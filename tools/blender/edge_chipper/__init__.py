# SPDX-License-Identifier: GPL-3.0-or-later
"""Edge Chipper - auto-detect hard edges and add chipped / broken edge damage.

Two procedural parts that share the same masks:

* A Geometry Nodes modifier ("EC_EdgeChip") that finds hard edges by face
  angle, builds a damage zone around them (biased towards the outer parts of
  the object), breaks the silhouette by pushing chips into the surface and
  stores the masks as attributes: ec_chip, ec_wear, ec_cavity.
* A shader node group ("EC_EdgeDecal") inserted in front of the Principled
  BSDF that reads those attributes and paints the chip decal: exposed
  material inside the chip, worn rim around it, dirt in concave edges, bump.

Everything stays live: tweak the sliders, then apply / bake when happy.
"""

import math

import bpy
from bpy.props import EnumProperty, BoolProperty

GN_NAME = "EC_EdgeChip"
SH_NAME = "EC_EdgeDecal"
TREE_VERSION = 1  # bump when node graphs change so old files get rebuilt

ATTR_CHIP = "ec_chip"
ATTR_WEAR = "ec_wear"
ATTR_CAVITY = "ec_cavity"


# --------------------------------------------------------------------------
# Small node-building helper
# --------------------------------------------------------------------------

class Builder:
    def __init__(self, tree):
        self.tree = tree
        self.nodes = tree.nodes
        self.links = tree.links

    def node(self, bl_idname, label=None, **props):
        n = self.nodes.new(bl_idname)
        for k, v in props.items():
            setattr(n, k, v)
        if label:
            n.label = label
        return n

    @staticmethod
    def sock(collection, ident):
        """Look a socket up by identifier (names are not unique on Mix nodes)."""
        return next(x for x in collection if x.identifier == ident)

    def link(self, src, dst):
        self.links.new(src, dst)

    def feed(self, sock, value):
        """Link a socket or set a constant on an input socket."""
        if isinstance(value, bpy.types.NodeSocket):
            self.link(value, sock)
        elif value is not None:
            sock.default_value = value

    def math(self, op, a, b=None, c=None, clamp=False, label=None):
        n = self.node("ShaderNodeMath", label, operation=op, use_clamp=clamp)
        for i, v in enumerate((a, b, c)):
            if v is not None:
                self.feed(n.inputs[i], v)
        return n.outputs[0]

    def vmath(self, op, a, b=None, scale=None):
        n = self.node("ShaderNodeVectorMath", operation=op)
        self.feed(n.inputs[0], a)
        if b is not None:
            self.feed(n.inputs[1], b)
        if scale is not None:
            self.feed(n.inputs["Scale"], scale)
        if op in {"LENGTH", "DOT_PRODUCT", "DISTANCE"}:
            return n.outputs["Value"]
        return n.outputs["Vector"]

    def separate(self, vec):
        n = self.node("ShaderNodeSeparateXYZ")
        self.feed(n.inputs[0], vec)
        return n.outputs

    def map_range(self, value, fmin, fmax, tmin=0.0, tmax=1.0, smooth=False, label=None):
        n = self.node("ShaderNodeMapRange", label, data_type="FLOAT", clamp=True,
                      interpolation_type="SMOOTHSTEP" if smooth else "LINEAR")
        self.feed(n.inputs["Value"], value)
        self.feed(n.inputs["From Min"], fmin)
        self.feed(n.inputs["From Max"], fmax)
        self.feed(n.inputs["To Min"], tmin)
        self.feed(n.inputs["To Max"], tmax)
        return n.outputs["Result"]

    def mix_float(self, fac, a, b):
        n = self.node("ShaderNodeMix", data_type="FLOAT", clamp_factor=True)
        self.feed(self.sock(n.inputs, "Factor_Float"), fac)
        self.feed(self.sock(n.inputs, "A_Float"), a)
        self.feed(self.sock(n.inputs, "B_Float"), b)
        return self.sock(n.outputs, "Result_Float")

    def mix_color(self, fac, a, b, blend="MIX"):
        n = self.node("ShaderNodeMix", data_type="RGBA", blend_type=blend, clamp_factor=True)
        self.feed(self.sock(n.inputs, "Factor_Float"), fac)
        self.feed(self.sock(n.inputs, "A_Color"), a)
        self.feed(self.sock(n.inputs, "B_Color"), b)
        return self.sock(n.outputs, "Result_Color")

    def layout(self):
        """Column layout by link depth so the graph is readable when opened."""
        depth = {}
        incoming = {n: [] for n in self.nodes}
        for l in self.links:
            incoming[l.to_node].append(l.from_node)

        def d(n, stack=()):
            if n in depth:
                return depth[n]
            if n in stack:
                return 0
            depth[n] = 0 if not incoming[n] else 1 + max(d(p, stack + (n,)) for p in incoming[n])
            return depth[n]

        cols = {}
        for n in self.nodes:
            cols.setdefault(d(n), []).append(n)
        for c, ns in cols.items():
            for i, n in enumerate(ns):
                n.location = (c * 220.0, -i * 190.0)


def _socket(tree, name, stype, parent=None, default=None, vmin=None, vmax=None,
            subtype=None, desc="", in_out="INPUT", hide_value=False):
    s = tree.interface.new_socket(name, description=desc, in_out=in_out,
                                  socket_type=stype, parent=parent)
    if subtype and hasattr(s, "subtype"):
        try:
            s.subtype = subtype
        except TypeError:
            pass
    if vmin is not None:
        s.min_value = vmin
    if vmax is not None:
        s.max_value = vmax
    if default is not None:
        s.default_value = default
    if hide_value:
        s.hide_value = True
    return s


# --------------------------------------------------------------------------
# Geometry Nodes: detection + damage zone + broken edge displacement
# --------------------------------------------------------------------------

def build_gn_tree():
    tree = bpy.data.node_groups.new(GN_NAME, "GeometryNodeTree")
    tree.is_modifier = True
    tree["ec_version"] = TREE_VERSION
    tree.description = "Auto-detect hard edges and add chips / broken edges"

    S = lambda *a, **k: _socket(tree, *a, **k)
    S("Geometry", "NodeSocketGeometry")
    S("Geometry", "NodeSocketGeometry", in_out="OUTPUT")

    p = tree.interface.new_panel("Edge Detection")
    S("Angle Threshold", "NodeSocketFloat", p, math.radians(30), 0.0, math.pi, "ANGLE",
      "Edges sharper than this face angle are treated as hard edges")
    S("Use Marked Sharp", "NodeSocketBool", p, False,
      desc="Also treat edges marked Sharp as hard edges (convex ones only)")
    S("Concave Chips", "NodeSocketFloat", p, 0.0, 0.0, 1.0, "FACTOR",
      "How much inner (concave) edges chip. Concave edges always feed the cavity/dirt mask")

    p = tree.interface.new_panel("Damage Zone")
    S("Edge Width", "NodeSocketFloat", p, 0.05, 0.0, 100.0, "DISTANCE",
      "How far from a hard edge damage can reach")
    S("Outer Bias", "NodeSocketFloat", p, 0.6, 0.0, 1.0, "FACTOR",
      "0 = every edge damaged evenly, 1 = only the outer extremities of the object get damaged")
    S("Outer Falloff", "NodeSocketFloat", p, 2.0, 0.1, 10.0, None,
      "Higher = damage concentrates harder on the outermost corners")
    S("Height Bias", "NodeSocketFloat", p, 0.0, -1.0, 1.0, "FACTOR",
      "+ = more damage on top edges, - = more damage at the bottom (scuffed feet)")

    p = tree.interface.new_panel("Chips")
    S("Amount", "NodeSocketFloat", p, 0.5, 0.0, 1.0, "FACTOR", "Overall amount of chipping")
    S("Breakup", "NodeSocketFloat", p, 0.8, 0.0, 2.0, "FACTOR",
      "0 = continuous worn strip, 1 = separate chips with gaps, >1 = extra ragged borders")
    S("Softness", "NodeSocketFloat", p, 0.3, 0.0, 1.0, "FACTOR",
      "Slope of the carved chip walls (the painted border stays crisp in the shader)")
    S("Chip Scale", "NodeSocketFloat", p, 8.0, 0.0, 1000.0, None, "Noise scale of the chips")
    S("Chip Detail", "NodeSocketFloat", p, 4.0, 0.0, 15.0, None, "Noise detail of the chips")
    S("Distortion", "NodeSocketFloat", p, 0.4, 0.0, 10.0, None, "Noise distortion (chunkier chips)")
    S("Seed", "NodeSocketInt", p, 0, 0, 100000, None, "Random variation")

    p = tree.interface.new_panel("Broken Edge Geometry")
    S("Subdivisions", "NodeSocketInt", p, 3, 0, 7, None,
      "Mesh density used to carve the chips. Keep low on heavy meshes")
    S("Displace", "NodeSocketBool", p, True, desc="Carve chips into the geometry")
    S("Chip Depth", "NodeSocketFloat", p, 0.015, 0.0, 10.0, "DISTANCE",
      "How deep chips are pushed into the surface")

    b = Builder(tree)
    gin = b.node("NodeGroupInput")
    gout = b.node("NodeGroupOutput")
    I = gin.outputs

    # --- detect hard edges on the ORIGINAL mesh ---------------------------
    angle = b.node("GeometryNodeInputMeshEdgeAngle").outputs["Signed Angle"]
    cmp_cvx = b.node("FunctionNodeCompare", "Convex", data_type="FLOAT", operation="GREATER_THAN")
    b.feed(cmp_cvx.inputs["A"], angle)
    b.feed(cmp_cvx.inputs["B"], I["Angle Threshold"])
    neg_thr = b.math("MULTIPLY", I["Angle Threshold"], -1.0)
    cmp_ccv = b.node("FunctionNodeCompare", "Concave", data_type="FLOAT", operation="LESS_THAN")
    b.feed(cmp_ccv.inputs["A"], angle)
    b.feed(cmp_ccv.inputs["B"], neg_thr)

    sharp = b.node("GeometryNodeInputNamedAttribute", "sharp_edge", data_type="BOOLEAN")
    sharp.inputs["Name"].default_value = "sharp_edge"
    is_pos = b.node("FunctionNodeCompare", data_type="FLOAT", operation="GREATER_THAN")
    b.feed(is_pos.inputs["A"], angle)
    is_pos.inputs["B"].default_value = 0.001
    and1 = b.node("FunctionNodeBooleanMath", operation="AND")
    b.link(sharp.outputs["Attribute"], and1.inputs[0])
    b.link(I["Use Marked Sharp"], and1.inputs[1])
    and2 = b.node("FunctionNodeBooleanMath", operation="AND")
    b.link(and1.outputs[0], and2.inputs[0])
    b.link(is_pos.outputs["Result"], and2.inputs[1])
    cvx_sel = b.node("FunctionNodeBooleanMath", operation="OR")
    b.link(cmp_cvx.outputs["Result"], cvx_sel.inputs[0])
    b.link(and2.outputs[0], cvx_sel.inputs[1])

    def edge_target(selection):
        m2c = b.node("GeometryNodeMeshToCurve")
        b.link(I["Geometry"], m2c.inputs["Mesh"])
        b.link(selection, m2c.inputs["Selection"])
        c2m = b.node("GeometryNodeCurveToMesh")
        b.link(m2c.outputs["Curve"], c2m.inputs["Curve"])
        return c2m.outputs["Mesh"]

    tgt_cvx = edge_target(cvx_sel.outputs[0])
    tgt_ccv = edge_target(cmp_ccv.outputs["Result"])

    # --- densify for carving -------------------------------------------------
    subd = b.node("GeometryNodeSubdivideMesh")
    b.link(I["Geometry"], subd.inputs["Mesh"])
    b.link(I["Subdivisions"], subd.inputs["Level"])

    # --- distance to hard edges (topology independent) ---------------------
    def edge_mask(target):
        prox = b.node("GeometryNodeProximity", target_element="EDGES")
        b.link(target, prox.inputs[0])
        m = b.map_range(prox.outputs["Distance"], 0.0, b.math("MAXIMUM", I["Edge Width"], 1e-5),
                        1.0, 0.0, smooth=True)
        # empty target (no hard edges found) -> Is Valid is False -> no damage
        return b.math("MULTIPLY", m, prox.outputs["Is Valid"])

    m_cvx = edge_mask(tgt_cvx)
    m_ccv = edge_mask(tgt_ccv)

    # --- outer / height weighting -------------------------------------------
    bbox = b.node("GeometryNodeBoundBox")
    b.link(I["Geometry"], bbox.inputs["Geometry"])
    center = b.vmath("SCALE", b.vmath("ADD", bbox.outputs["Min"], bbox.outputs["Max"]), scale=0.5)
    half = b.vmath("SCALE", b.vmath("SUBTRACT", bbox.outputs["Max"], bbox.outputs["Min"]), scale=0.5)
    half = b.vmath("MAXIMUM", half, (1e-4, 1e-4, 1e-4))
    pos = b.node("GeometryNodeInputPosition").outputs["Position"]
    rel = b.vmath("DIVIDE", b.vmath("SUBTRACT", pos, center), half)  # -1..1 box space
    outer = b.math("DIVIDE", b.vmath("LENGTH", rel), math.sqrt(3.0), clamp=True)
    outer = b.math("POWER", outer, I["Outer Falloff"])
    outer_w = b.mix_float(I["Outer Bias"], 1.0, outer)
    rel_z = b.separate(rel)["Z"]
    height_w = b.math("MULTIPLY_ADD", rel_z, I["Height Bias"], 1.0)
    height_w = b.math("MAXIMUM", height_w, 0.0)
    weight = b.math("MULTIPLY", outer_w, height_w, label="Damage Weight")

    wear = b.math("MULTIPLY", m_cvx, weight, clamp=True, label="Wear")
    ccv_chip = b.math("MULTIPLY", b.math("MULTIPLY", m_ccv, I["Concave Chips"]), weight)
    zone = b.math("MAXIMUM", wear, ccv_chip)

    # --- chip noise ------------------------------------------------------------
    seed_f = b.math("MULTIPLY", I["Seed"], 1.0)
    seed_v = b.node("ShaderNodeCombineXYZ")
    b.link(b.math("MULTIPLY", seed_f, 17.137), seed_v.inputs[0])
    b.link(b.math("MULTIPLY", seed_f, 9.731), seed_v.inputs[1])
    b.link(b.math("MULTIPLY", seed_f, 5.419), seed_v.inputs[2])
    noise = b.node("ShaderNodeTexNoise", noise_dimensions="3D")
    b.link(b.vmath("ADD", pos, seed_v.outputs[0]), noise.inputs["Vector"])
    b.link(I["Chip Scale"], noise.inputs["Scale"])
    b.link(I["Chip Detail"], noise.inputs["Detail"])
    noise.inputs["Roughness"].default_value = 0.6
    b.link(I["Distortion"], noise.inputs["Distortion"])
    # high-contrast noise gates WHERE along the edge chips happen (gaps in
    # between), and also varies how far each chip bites into the face.
    n_c = b.map_range(noise.outputs["Factor"], 0.38, 0.62, smooth=True, label="Chip Gate")
    gate = b.mix_float(b.math("MINIMUM", I["Breakup"], 1.0), 1.0, n_c)
    value = b.math("MULTIPLY", zone, gate)
    over = b.math("MAXIMUM", b.math("SUBTRACT", I["Breakup"], 1.0), 0.0)  # >1: ragged border
    value = b.math("MULTIPLY_ADD", b.math("SUBTRACT", noise.outputs["Factor"], 0.5), over, value)

    lo = b.math("SUBTRACT", 1.0, I["Amount"])
    hi = b.math("ADD", lo, b.math("MAXIMUM", I["Softness"], 0.002))
    chip = b.map_range(value, lo, hi, 0.0, 1.0, smooth=True)
    chip = b.math("MULTIPLY", chip, b.map_range(zone, 0.0, 0.15), label="Chip")

    # --- store masks, then carve -------------------------------------------
    geo = subd.outputs["Mesh"]
    for name, field in ((ATTR_CHIP, chip), (ATTR_WEAR, wear), (ATTR_CAVITY, m_ccv)):
        st = b.node("GeometryNodeStoreNamedAttribute", name, data_type="FLOAT", domain="POINT")
        st.inputs["Name"].default_value = name
        b.link(geo, st.inputs["Geometry"])
        b.link(field, st.inputs["Value"])
        geo = st.outputs["Geometry"]

    rd = b.node("GeometryNodeInputNamedAttribute", data_type="FLOAT")
    rd.inputs["Name"].default_value = ATTR_CHIP
    amt = b.math("MULTIPLY", rd.outputs["Attribute"], I["Chip Depth"])
    amt = b.math("MULTIPLY", amt, I["Displace"])
    nrm = b.node("GeometryNodeInputNormal").outputs["Normal"]
    offset = b.vmath("SCALE", nrm, scale=b.math("MULTIPLY", amt, -1.0))
    setp = b.node("GeometryNodeSetPosition")
    b.link(geo, setp.inputs["Geometry"])
    b.link(offset, setp.inputs["Offset"])
    b.link(setp.outputs["Geometry"], gout.inputs[0])

    b.layout()
    return tree


# --------------------------------------------------------------------------
# Shader: chip decal
# --------------------------------------------------------------------------

def build_shader_tree():
    tree = bpy.data.node_groups.new(SH_NAME, "ShaderNodeTree")
    tree["ec_version"] = TREE_VERSION
    tree.description = "Paints chips / worn edges / cavity dirt from the Edge Chipper masks"
    S = lambda *a, **k: _socket(tree, *a, **k)

    S("Base Color", "NodeSocketColor", default=(0.35, 0.05, 0.03, 1.0))
    S("Roughness", "NodeSocketFloat", default=0.5, vmin=0.0, vmax=1.0, subtype="FACTOR")
    S("Metallic", "NodeSocketFloat", default=0.0, vmin=0.0, vmax=1.0, subtype="FACTOR")
    S("Normal", "NodeSocketVector", hide_value=True)

    p = tree.interface.new_panel("Chip Decal")
    S("Chip Color", "NodeSocketColor", p, (0.45, 0.45, 0.46, 1.0),
      desc="Exposed material under the paint")
    S("Chip Roughness", "NodeSocketFloat", p, 0.35, 0.0, 1.0, "FACTOR")
    S("Chip Metallic", "NodeSocketFloat", p, 1.0, 0.0, 1.0, "FACTOR")
    S("Chip Size", "NodeSocketFloat", p, 0.0, -1.0, 1.0, "FACTOR",
      desc="Grow / shrink the painted chips relative to the carved geometry")
    S("Chip Sharpness", "NodeSocketFloat", p, 0.85, 0.0, 1.0, "FACTOR")
    S("Fine Scale", "NodeSocketFloat", p, 60.0, 0.0, 5000.0, desc="Scale of the micro chip noise")
    S("Fine Breakup", "NodeSocketFloat", p, 0.7, 0.0, 2.0, "FACTOR",
      desc="Pixel-level breakup of the chip border")
    S("Rim Width", "NodeSocketFloat", p, 0.12, 0.0, 1.0, "FACTOR",
      desc="Light worn paint rim around every chip")

    p = tree.interface.new_panel("Wear & Dirt")
    S("Wear Color", "NodeSocketColor", p, (0.75, 0.6, 0.5, 1.0), desc="Rubbed paint on edges / chip rims")
    S("Wear Amount", "NodeSocketFloat", p, 0.2, 0.0, 1.0, "FACTOR")
    S("Dirt Color", "NodeSocketColor", p, (0.05, 0.04, 0.03, 1.0), desc="Grime in concave edges")
    S("Dirt Amount", "NodeSocketFloat", p, 0.5, 0.0, 1.0, "FACTOR")

    p = tree.interface.new_panel("Surface")
    S("Bump Strength", "NodeSocketFloat", p, 0.6, 0.0, 1.0, "FACTOR")
    S("Bump Distance", "NodeSocketFloat", p, 0.004, 0.0, 1.0, "DISTANCE")
    S("Show Mask", "NodeSocketFloat", p, 0.0, 0.0, 1.0, "FACTOR",
      desc="Debug view: R = chips, G = edge wear, B = cavity")

    S("Color", "NodeSocketColor", in_out="OUTPUT")
    S("Roughness", "NodeSocketFloat", in_out="OUTPUT")
    S("Metallic", "NodeSocketFloat", in_out="OUTPUT")
    S("Normal", "NodeSocketVector", in_out="OUTPUT")
    S("Chip Mask", "NodeSocketFloat", in_out="OUTPUT")

    b = Builder(tree)
    gin = b.node("NodeGroupInput")
    gout = b.node("NodeGroupOutput")
    I = gin.outputs

    def attr(name):
        n = b.node("ShaderNodeAttribute", name, attribute_type="GEOMETRY", attribute_name=name)
        return n.outputs["Fac"] if "Fac" in n.outputs else n.outputs["Factor"]

    a_chip, a_wear, a_cav = attr(ATTR_CHIP), attr(ATTR_WEAR), attr(ATTR_CAVITY)

    tc = b.node("ShaderNodeTexCoord").outputs["Object"]
    fine = b.node("ShaderNodeTexNoise", noise_dimensions="3D")
    b.link(tc, fine.inputs["Vector"])
    b.link(I["Fine Scale"], fine.inputs["Scale"])
    fine.inputs["Detail"].default_value = 8.0
    fine.inputs["Roughness"].default_value = 0.65
    fine.inputs["Distortion"].default_value = 0.2

    v = b.math("MULTIPLY_ADD", b.math("SUBTRACT", fine.outputs["Factor"], 0.5), I["Fine Breakup"], a_chip)
    v = b.math("ADD", v, b.math("MULTIPLY", I["Chip Size"], 0.4))
    soft = b.math("MAXIMUM", b.math("MULTIPLY", b.math("SUBTRACT", 1.0, I["Chip Sharpness"]), 0.3), 0.001)
    lo = b.math("SUBTRACT", 0.5, soft)
    hi = b.math("ADD", 0.5, soft)
    chip = b.map_range(v, lo, hi, smooth=True, label="Chip")
    # rim: band just outside the chip border
    rim_lo = b.math("SUBTRACT", lo, b.math("MULTIPLY", I["Rim Width"], 0.5))
    rim = b.math("SUBTRACT", b.map_range(v, rim_lo, lo, smooth=True), chip, clamp=True, label="Rim")

    wear_edge = b.map_range(a_wear, 0.55, 1.0, smooth=True)  # keep wear tight to the edge
    wear_f = b.math("MULTIPLY_ADD", wear_edge, I["Wear Amount"], rim, clamp=True)
    dirt_f = b.math("MULTIPLY", a_cav, I["Dirt Amount"], clamp=True)

    col = b.mix_color(wear_f, I["Base Color"], I["Wear Color"])
    col = b.mix_color(dirt_f, col, I["Dirt Color"], blend="MULTIPLY")
    col = b.mix_color(chip, col, I["Chip Color"])
    dbg = b.node("ShaderNodeCombineXYZ")
    b.link(chip, dbg.inputs[0])
    b.link(a_wear, dbg.inputs[1])
    b.link(a_cav, dbg.inputs[2])
    col = b.mix_color(I["Show Mask"], col, dbg.outputs[0])

    rough = b.mix_float(dirt_f, I["Roughness"], b.math("ADD", I["Roughness"], 0.25, clamp=True))
    rough = b.mix_float(chip, rough, I["Chip Roughness"])
    metal = b.mix_float(chip, I["Metallic"], I["Chip Metallic"])

    bump = b.node("ShaderNodeBump")
    b.link(I["Bump Strength"], bump.inputs["Strength"])
    b.link(I["Bump Distance"], bump.inputs["Distance"])
    b.link(b.math("SUBTRACT", 1.0, chip), bump.inputs["Height"])
    b.link(I["Normal"], bump.inputs["Normal"])

    b.link(col, gout.inputs["Color"])
    b.link(rough, gout.inputs["Roughness"])
    b.link(metal, gout.inputs["Metallic"])
    b.link(bump.outputs["Normal"], gout.inputs["Normal"])
    b.link(chip, gout.inputs["Chip Mask"])

    b.layout()
    return tree


def _get_tree(name, builder, tree_type):
    t = bpy.data.node_groups.get(name)
    if t and t.bl_idname == tree_type and t.get("ec_version") == TREE_VERSION:
        return t
    if t:  # outdated or wrong type: rename so existing users keep working
        t.name = name + "_old"
    return builder()


def get_gn_tree():
    return _get_tree(GN_NAME, build_gn_tree, "GeometryNodeTree")


def get_shader_tree():
    return _get_tree(SH_NAME, build_shader_tree, "ShaderNodeTree")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def find_modifier(obj):
    if obj is None or obj.type != "MESH":
        return None
    for m in obj.modifiers:
        if m.type == "NODES" and m.node_group and m.node_group.name.startswith(GN_NAME):
            return m
    return None


def input_ids(group):
    return {i.name: i.identifier for i in group.interface.items_tree
            if i.item_type == "SOCKET" and i.in_out == "INPUT"}


def input_struct(mod, ident):
    """Blender 5.x keeps modifier inputs at mod.properties.inputs.<id>.value;
    older versions used ID properties (mod[id])."""
    props = getattr(mod, "properties", None)
    if props is not None and hasattr(props, "inputs"):
        return getattr(props.inputs, ident, None)
    return None


def set_input(mod, name, value):
    ident = input_ids(mod.node_group).get(name)
    if ident is None:
        return
    st = input_struct(mod, ident)
    if st is not None:
        st.value = value
    else:
        mod[ident] = value
    mod.id_data.update_tag()


def selected_meshes(context):
    objs = [o for o in context.selected_objects if o.type == "MESH"]
    if not objs and context.active_object and context.active_object.type == "MESH":
        objs = [context.active_object]
    return objs


def object_size(obj):
    xs = [v[0] for v in obj.bound_box]
    ys = [v[1] for v in obj.bound_box]
    zs = [v[2] for v in obj.bound_box]
    return max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 1e-4)


# width / depth as a fraction of the object size, so presets work at any scale
PRESETS = {
    "SUBTLE": dict(width=0.025, depth=0.006, scale=10.0, amount=0.4,  breakup=0.95, outer=0.7),
    "WORN":   dict(width=0.04,  depth=0.012, scale=8.0,  amount=0.55, breakup=0.9, outer=0.6),
    "HEAVY":  dict(width=0.06,  depth=0.02,  scale=6.0,  amount=0.68, breakup=0.85, outer=0.45),
    "BROKEN": dict(width=0.09,  depth=0.035, scale=4.0,  amount=0.78, breakup=1.25, outer=0.35),
}
PRESET_ITEMS = [
    ("SUBTLE", "Subtle", "Light chipping on outer corners"),
    ("WORN", "Worn", "Typical used prop"),
    ("HEAVY", "Heavy", "Well beaten object"),
    ("BROKEN", "Broken", "Large broken chunks along edges"),
]


def apply_preset(obj, mod, key):
    p = PRESETS[key]
    size = object_size(obj)
    set_input(mod, "Edge Width", size * p["width"])
    set_input(mod, "Chip Depth", size * p["depth"])
    set_input(mod, "Chip Scale", p["scale"] / size)
    set_input(mod, "Amount", p["amount"])
    set_input(mod, "Breakup", p["breakup"])
    set_input(mod, "Outer Bias", p["outer"])
    obj.update_tag()


def add_decal_to_material(mat):
    """Insert the decal group in front of the material's Principled BSDF."""
    if not mat.use_nodes:
        mat.use_nodes = True
    nt = mat.node_tree
    group = get_shader_tree()
    for n in nt.nodes:
        if n.type == "GROUP" and n.node_tree and n.node_tree.name.startswith(SH_NAME):
            return False
    bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
        if out is None:
            out = nt.nodes.new("ShaderNodeOutputMaterial")
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (out.location.x - 300, out.location.y)
        nt.links.new(bsdf.outputs[0], out.inputs["Surface"])

    g = nt.nodes.new("ShaderNodeGroup")
    g.node_tree = group
    g.label = "Edge Chip Decal"
    g.location = (bsdf.location.x - 260, bsdf.location.y)
    for name in ("Base Color", "Roughness", "Metallic", "Normal"):
        dst = bsdf.inputs[name]
        if dst.is_linked:
            nt.links.new(dst.links[0].from_socket, g.inputs[name])
        elif name != "Normal":
            g.inputs[name].default_value = dst.default_value
        out_name = "Color" if name == "Base Color" else name
        nt.links.new(g.outputs[out_name], dst)
    return True


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

class EC_OT_add(bpy.types.Operator):
    bl_idname = "edge_chipper.add"
    bl_label = "Add Edge Chips"
    bl_description = "Detect hard edges and add procedural chips to the selected meshes"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(name="Preset", items=PRESET_ITEMS, default="WORN")
    add_shader: BoolProperty(name="Add Chip Decal Shader", default=True,
                             description="Insert the chip decal group into the object's materials")

    def execute(self, context):
        objs = selected_meshes(context)
        if not objs:
            self.report({"WARNING"}, "Select at least one mesh object")
            return {"CANCELLED"}
        tree = get_gn_tree()
        for obj in objs:
            mod = find_modifier(obj)
            if mod is None:
                mod = obj.modifiers.new("Edge Chipper", "NODES")
                mod.node_group = tree
            apply_preset(obj, mod, self.preset)
            set_input(mod, "Seed", hash(obj.name) % 1000)
            if self.add_shader:
                add_shader_to_object(obj)
        return {"FINISHED"}


def add_shader_to_object(obj):
    if not obj.data.materials or all(m is None for m in obj.data.materials):
        mat = bpy.data.materials.new(obj.name + "_EdgeChip")
        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)
    count = 0
    for mat in obj.data.materials:
        if mat and add_decal_to_material(mat):
            count += 1
    return count


class EC_OT_add_shader(bpy.types.Operator):
    bl_idname = "edge_chipper.add_shader"
    bl_label = "Add Decal Shader"
    bl_description = "Insert the chip decal node group into the materials of the selected meshes"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        n = sum(add_shader_to_object(o) for o in selected_meshes(context))
        self.report({"INFO"}, f"Chip decal added to {n} material(s)")
        return {"FINISHED"}


class EC_OT_preset(bpy.types.Operator):
    bl_idname = "edge_chipper.preset"
    bl_label = "Apply Preset"
    bl_description = "Apply a damage preset scaled to each object's size"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(name="Preset", items=PRESET_ITEMS, default="WORN")

    def execute(self, context):
        for obj in selected_meshes(context):
            mod = find_modifier(obj)
            if mod:
                apply_preset(obj, mod, self.preset)
        return {"FINISHED"}


class EC_OT_seed(bpy.types.Operator):
    bl_idname = "edge_chipper.randomize"
    bl_label = "Randomize"
    bl_description = "New random chip pattern"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        import random
        for obj in selected_meshes(context):
            mod = find_modifier(obj)
            if mod:
                set_input(mod, "Seed", random.randint(0, 99999))
                obj.update_tag()
        return {"FINISHED"}


class EC_OT_apply(bpy.types.Operator):
    bl_idname = "edge_chipper.apply"
    bl_label = "Apply"
    bl_description = ("Apply the Edge Chipper modifier (bakes geometry; the ec_* attributes stay "
                      "on the mesh so the decal shader keeps working)")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        for obj in selected_meshes(context):
            mod = find_modifier(obj)
            if mod:
                with context.temp_override(object=obj, active_object=obj):
                    bpy.ops.object.modifier_apply(modifier=mod.name)
        return {"FINISHED"}


class EC_OT_remove(bpy.types.Operator):
    bl_idname = "edge_chipper.remove"
    bl_label = "Remove"
    bl_description = "Remove the Edge Chipper modifier"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        for obj in selected_meshes(context):
            mod = find_modifier(obj)
            if mod:
                obj.modifiers.remove(mod)
        return {"FINISHED"}


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

PANEL_LAYOUT = (
    ("Edge Detection", ("Angle Threshold", "Use Marked Sharp", "Concave Chips")),
    ("Damage Zone (outer vs inner)", ("Edge Width", "Outer Bias", "Outer Falloff", "Height Bias")),
    ("Chips", ("Amount", "Breakup", "Softness", "Chip Scale", "Chip Detail", "Distortion", "Seed")),
    ("Broken Edge Geometry", ("Displace", "Subdivisions", "Chip Depth")),
)


class EC_PT_main(bpy.types.Panel):
    bl_label = "Edge Chipper"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Edge Chipper"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        mod = find_modifier(obj)
        if mod is None:
            layout.operator(EC_OT_add.bl_idname, icon="MOD_BEVEL")
            if obj is None or obj.type != "MESH":
                layout.label(text="Select a mesh", icon="INFO")
            return

        row = layout.row(align=True)
        row.operator_menu_enum(EC_OT_preset.bl_idname, "preset", text="Preset", icon="PRESET")
        row.operator(EC_OT_seed.bl_idname, icon="FILE_REFRESH")
        row = layout.row(align=True)
        row.prop(mod, "show_viewport", text="Viewport")
        row.operator(EC_OT_add_shader.bl_idname, text="Decal Shader", icon="MATERIAL")

        ids = input_ids(mod.node_group)
        for title, names in PANEL_LAYOUT:
            box = layout.box()
            box.label(text=title)
            col = box.column(align=True)
            for n in names:
                if n not in ids:
                    continue
                st = input_struct(mod, ids[n])
                if st is not None:
                    col.prop(st, "value", text=n)
                else:
                    col.prop(mod, f'["{ids[n]}"]', text=n)

        row = layout.row(align=True)
        row.operator(EC_OT_apply.bl_idname, icon="CHECKMARK")
        row.operator(EC_OT_remove.bl_idname, icon="X")


classes = (EC_OT_add, EC_OT_add_shader, EC_OT_preset, EC_OT_seed, EC_OT_apply,
           EC_OT_remove, EC_PT_main)


def menu_func(self, context):
    self.layout.operator(EC_OT_add.bl_idname, icon="MOD_BEVEL")


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.VIEW3D_MT_object.append(menu_func)


def unregister():
    bpy.types.VIEW3D_MT_object.remove(menu_func)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
