# Pack per-colour ASCII STLs (OpenSCAD output) into one 3MF, each part preassigned to an extruder,
# centred on the 256 x 256 bed, with plate 1 defined. Parts whose boxes touch or overlap form one object
# (a multi-colour piece stays together); each separate piece is its own object, so with exclude_object on
# it can be cancelled on its own mid-print (2026-11-05). --one-object keeps every part in a single object
# (needed for set_part_type.py --object, and for modifier volumes that do not touch their part).
# Usage: python make_3mf.py <out.3mf> [--one-object | --separate] <name>:<file.stl>:<extruder> [...]
#   e.g. python make_3mf.py tag.3mf base:tag-base.stl:1 text:tag-text.stl:4
import sys, zipfile

OUT = sys.argv[1]
ONE = "--one-object" in sys.argv[2:]
SEP = "--separate" in sys.argv[2:]   # every part its own object (caller knows the pieces do not touch), 2026-11-07
PARTS = []
for a in (x for x in sys.argv[2:] if x not in ("--one-object", "--separate")):
    name, rest = a.split(":", 1)
    path, ext = rest.rsplit(":", 1)  # path may hold a drive colon
    PARTS.append((name, path, int(ext)))


def read_ascii_stl(path):
    verts, index, tris, cur = [], {}, [], []
    for line in open(path, encoding="ascii"):
        w = line.split()
        if w and w[0] == "vertex":
            v = tuple(round(float(x), 5) for x in w[1:4])
            if v not in index:
                index[v] = len(verts)
                verts.append(v)
            cur.append(index[v])
            if len(cur) == 3:
                if len(set(cur)) == 3:
                    tris.append(tuple(cur))
                cur = []
    return verts, tris


objs, boxes, lo, hi = [], [], [1e9] * 3, [-1e9] * 3
for oid, (name, path, _) in enumerate(PARTS, 1):
    verts, tris = read_ascii_stl(path)
    blo = [min(v[i] for v in verts) for i in range(3)]
    bhi = [max(v[i] for v in verts) for i in range(3)]
    boxes.append((blo, bhi))
    lo = [min(a, b) for a, b in zip(lo, blo)]
    hi = [max(a, b) for a, b in zip(hi, bhi)]
    vx = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
    tx = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    objs.append(f'<object id="{oid}" name="{name}" type="model"><mesh><vertices>{vx}</vertices><triangles>{tx}</triangles></mesh></object>')
    print(name, len(verts), "verts", len(tris), "tris")

# Group parts into objects: boxes that touch or overlap (0.01 mm) join, transitively. Boxes are coarse, so two
# pieces with overlapping boxes but a real gap stay one object; that only means they cancel together.
group = list(range(len(PARTS)))
def root(i):
    while group[i] != i:
        group[i] = group[group[i]]
        i = group[i]
    return i
for i in range(len(PARTS)):
    for j in range(i + 1, len(PARTS)):
        (alo, ahi), (blo, bhi) = boxes[i], boxes[j]
        if not SEP and (ONE or all(alo[k] <= bhi[k] + 0.01 and blo[k] <= ahi[k] + 0.01 for k in range(3))):
            group[root(j)] = root(i)
GROUPS = []
for i in range(len(PARTS)):
    r = root(i)
    g = next((g for g in GROUPS if root(g[0]) == r), None)
    g.append(i) if g else GROUPS.append([i])
# The slicer drops every object onto the bed. A piece that does not reach the bed (a stack with an air gap,
# a part held up by another across a gap) would move, so then keep the whole plate one object as before.
if len(GROUPS) > 1 and any(min(boxes[i][0][2] for i in g) > lo[2] + 0.01 for g in GROUPS):
    print("a piece does not reach the bed: one object")
    GROUPS = [list(range(len(PARTS)))]

dx, dy, dz = 128 - (lo[0] + hi[0]) / 2, 128 - (lo[1] + hi[1]) / 2, -lo[2]
ASM = [len(PARTS) + 1 + k for k in range(len(GROUPS))]
gname = lambda g: "-".join(PARTS[i][0] for i in g)[:60]
# Application tag makes Flash Studio read model_settings.config (extruder per part). Keep the version
# <= 02.04.02.00: the CLI (2.4.2) rejects newer ones with "File Version ... not supported".
# 02.04.02.00 made the GUI warn "3MF file version 2.4.2.0 is newer than Flash Studio's version 2.4.0.2"
# (2026-10-21), so use the GUI's own 02.04.00.02.
model = ('<?xml version="1.0" encoding="UTF-8"?>'
         '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021">'
         '<metadata name="Application">BambuStudio-02.04.00.02</metadata><metadata name="BambuStudio:3mfVersion">1</metadata>'
         '<resources>' + "".join(objs) + "".join(
             f'<object id="{a}" type="model"><components>'
             + "".join(f'<component objectid="{i + 1}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>' for i in g)
             + '</components></object>' for a, g in zip(ASM, GROUPS)) + '</resources><build>'
         + "".join(f'<item objectid="{a}" transform="1 0 0 0 1 0 0 0 1 {dx:.3f} {dy:.3f} {dz:.3f}" printable="1"/>'
                   for a in ASM) + '</build></model>')
def parts_cfg(g):
    return "".join(f'<part id="{i + 1}" subtype="normal_part"><metadata key="name" value="{PARTS[i][0]}"/>'
                   f'<metadata key="extruder" value="{PARTS[i][2]}"/></part>' for i in g)
settings = ('<?xml version="1.0" encoding="UTF-8"?><config>'
            # With a single part the slicer uses the object's extruder, not the part's, so it must match the first part.
            + "".join(f'<object id="{a}"><metadata key="name" value="{"model" if len(GROUPS) == 1 else gname(g)}"/>'
                      f'<metadata key="extruder" value="{PARTS[g[0]][2]}"/>{parts_cfg(g)}</object>'
                      for a, g in zip(ASM, GROUPS))
            # The CLI segfaults on a project with no plate.
            + '<plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value=""/><metadata key="locked" value="false"/>'
            + "".join(f'<model_instance><metadata key="object_id" value="{a}"/><metadata key="instance_id" value="0"/>'
                      f'<metadata key="identify_id" value="{100 + k}"/></model_instance>' for k, a in enumerate(ASM))
            + '</plate></config>')
ctypes = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("[Content_Types].xml", ctypes)
    z.writestr("_rels/.rels", rels)
    z.writestr("3D/3dmodel.model", model)
    z.writestr("Metadata/model_settings.config", settings)
print("size mm:", [round(h - l, 1) for l, h in zip(lo, hi)])
print("objects:", len(GROUPS), [gname(g) for g in GROUPS])
print("saved", OUT)
