# Design check on a sliced file: reads the real toolpath layer by layer and flags shapes known to fail on this printer.
# Checks (all from the G-code, so they work on Flash Studio, ForkaSlicer and hand-sliced files alike):
#   footprint - a narrow first-layer strip that a big part is later built on (lid 2c4d: groove ring curled, 2026-10-29)
#   floating  - a region that starts in mid-air with nothing under it (latch V catch, 2026-10-28)
#   span      - a large patch printed over nothing (lid ef56: panel floor = 168 mm bridge, 2026-10-28)
#   fused     - two separate printed pieces closer than ~1 mm
# Also counts the separate pieces on the plate (info["pieces"]): pieces that touch print as one. The fit gauge
# drone frame tab touched the RC frame and welded (2026-10-27) - only a count against the expected number shows that.
# Usage: python -I design_check.py <file.gcode|.3mf> [--png out_dir]     exit 1 = something to fix
import math, os, re, struct, sys, zlib

try:
    import numpy as np
except ImportError:  # the hub runs isolated (-I); numpy + scipy live in the user's site-packages, added last
    import site
    sys.path.append(site.getusersitepackages())
    import numpy as np
from scipy import ndimage

RES = 0.2                 # mm per raster cell
SKIP = ("Prime tower", "Wipe tower", "Skirt", "Brim", "Custom")
FOOT_W, FOOT_L = 15.0, 60.0   # footprint: narrower than this, longer than this ...
FOOT_GROW = 3.0               # ... and later part of a region this many times its own area (within FOOT_Z)
FOOT_Z = 3.0
FLOAT_MIN = 2.0           # mm2: smaller islands are noise (gap fill, seam blobs)
SPAN_W = 12.0             # mm: widest circle that fits in an unsupported patch
SUPPORT_R = 1.0           # mm: material within this of the layer below counts as carried (overhang step, bead width)
FUSE_GAP = 1.0            # mm
EIGHT = np.ones((3, 3), bool)


def layers(text):
    """Yield (z, segments, brim_segments) per layer: extrusion moves as [[x0, y0, x1, y1, tool], ...]."""
    z, kind, tool, x, y, rel_e, e_abs = 0.0, "", 0, None, None, True, 0.0
    segs, brim = [], []
    pack = lambda s: np.array(s, float).reshape(-1, 5)
    for line in text.splitlines():
        if line.startswith(";"):
            if line.startswith(";LAYER_CHANGE"):
                if segs or brim:
                    yield z, pack(segs), pack(brim)
                segs, brim = [], []
            elif line.startswith(";Z:"):
                z = float(line[3:])
            elif line.startswith(";TYPE:"):
                kind = line[6:].strip()
            continue
        c = line.split(";")[0].strip()
        if not c:
            continue
        head = c.split()[0]
        if head[0] == "T" and head[1:].isdigit():
            tool = int(head[1:])
            continue
        if head == "M83":
            rel_e = True
        elif head == "M82":
            rel_e = False
        elif head == "G92":
            m = re.search(r"E(-?[\d.]+)", c)
            if m:
                e_abs = float(m.group(1))
        if head not in ("G1", "G2", "G3"):
            if head == "G0":
                v = dict((k, float(n)) for k, n in re.findall(r"([XY])(-?[\d.]+)", c))
                x, y = v.get("X", x), v.get("Y", y)
            continue
        v = dict((k, float(n)) for k, n in re.findall(r"([XYEIJ])(-?[\d.]+)", c))
        nx, ny = v.get("X", x), v.get("Y", y)
        de = 0.0
        if "E" in v:
            de = v["E"] if rel_e else v["E"] - e_abs
            if not rel_e:
                e_abs = v["E"]
        if de > 0 and None not in (x, y, nx, ny):
            out = brim if kind in ("Brim", "Skirt") else None if kind.startswith(SKIP) else segs
            if out is not None:
                if head != "G1" and ("I" in v or "J" in v):
                    out.extend((*s, tool) for s in arc(x, y, nx, ny, v.get("I", 0.0), v.get("J", 0.0), head == "G2"))
                else:
                    out.append((x, y, nx, ny, tool))
        x, y = nx, ny
    if segs or brim:
        yield z, pack(segs), pack(brim)


def arc(x0, y0, x1, y1, i, j, cw):
    cx, cy = x0 + i, y0 + j
    a0, a1, r = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx), math.hypot(i, j)
    sweep = ((a0 - a1) if cw else (a1 - a0)) % (2 * math.pi) or 2 * math.pi
    n = max(2, int(r * sweep / 0.5))
    pts = [(cx + r * math.cos(a0 + (-1 if cw else 1) * sweep * k / n),
            cy + r * math.sin(a0 + (-1 if cw else 1) * sweep * k / n)) for k in range(n + 1)]
    return [(*pts[k], *pts[k + 1]) for k in range(n)]


def raster(segs, shape, origin):
    """Cells under each bead (line widened by one cell, ~0.6 mm for a 0.45 mm bead), and the tool (1-4) per cell."""
    g, t = np.zeros(shape, bool), np.zeros(shape, np.int8)
    if not len(segs):
        return g, t
    d = np.hypot(segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1])
    n = np.maximum(1, np.ceil(d / (RES / 2))).astype(int) + 1
    idx = np.repeat(np.arange(len(segs)), n)
    f = np.arange(len(idx)) - np.repeat(np.cumsum(n) - n, n)
    tt = f / np.repeat(n - 1, n).clip(1)
    ix = np.clip(((segs[idx, 0] + (segs[idx, 2] - segs[idx, 0]) * tt - origin[0]) / RES).astype(int), 0, shape[1] - 1)
    iy = np.clip(((segs[idx, 1] + (segs[idx, 3] - segs[idx, 1]) * tt - origin[1]) / RES).astype(int), 0, shape[0] - 1)
    g[iy, ix] = True
    t[iy, ix] = segs[idx, 4] + 1
    g = ndimage.binary_dilation(g, structure=EIGHT)
    return g, ndimage.grey_dilation(t, size=3) * g


def grow(mask, r_mm):
    """Mask widened by about r_mm each way (square reach, fast)."""
    r = max(1, int(round(r_mm / RES)))
    return ndimage.maximum_filter(mask.view(np.uint8), size=2 * r + 1).view(bool)


def width_mm(mask):
    """Diameter of the widest circle that fits inside the region."""
    return 2 * ndimage.distance_transform_edt(mask).max() * RES


class Bodies:
    """Union-find over (layer, region): regions that touch the region under them are one printed body."""
    def __init__(self):
        self.up = {}

    def find(self, a):
        while self.up.get(a, a) != a:
            self.up[a] = self.up.get(self.up[a], self.up[a])
            a = self.up[a]
        return a

    def join(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.up[ra] = rb


def check(text, png_dir=None, info=None):
    """Returns a list of findings: {"check", "layer", "z", "where", "message", "layers"}. Empty = nothing found.
    info (a dict) gets "pieces": separate printed bodies of at least FLOAT_MIN on some layer."""
    data = list(layers(text))
    allsegs = np.concatenate([s for _, s, _ in data if len(s)] or [np.zeros((0, 5))])
    if not len(allsegs):
        return []
    xs, ys = np.r_[allsegs[:, 0], allsegs[:, 2]], np.r_[allsegs[:, 1], allsegs[:, 3]]
    origin = (xs.min() - 5, ys.min() - 5)
    shape = (int((ys.max() - origin[1] + 5) / RES) + 1, int((xs.max() - origin[0] + 5) / RES) + 1)
    cell = RES * RES
    out, near, pics = [], [], {}
    fuse = (FUSE_GAP - 0.15) / 2  # each raster bead is ~0.15 mm wider than the real one
    bodies, prev, prev_lab, foot, real = Bodies(), None, None, [], []

    def where(mask):
        yy, xx = np.nonzero(mask)
        return f"X {origin[0] + xx.mean() * RES:.0f} Y {origin[1] + yy.mean() * RES:.0f}" if len(xx) else "?"

    def add(name, n, z, mask, msg, grid):
        out.append({"check": name, "layer": n, "z": round(z, 2), "where": where(mask), "message": msg})
        if png_dir and (name, where(mask)) not in pics:
            pics[(name, where(mask))] = 1
            save_png(os.path.join(png_dir, f"design-{name}-layer{n}.png"), grid, mask)

    for n, (z, segs, brim) in enumerate(data, 1):
        g, tg = raster(segs, shape, origin)
        lab, count = ndimage.label(g, structure=EIGHT)
        idx = np.arange(1, count + 1)
        sizes = np.bincount(lab.ravel(), minlength=count + 1) * cell
        real += [(n, int(k)) for k in idx[sizes[1:] >= FLOAT_MIN]]
        if prev_lab is not None and count:
            both = (lab > 0) & (prev_lab > 0)
            for a, b in set(zip(prev_lab[both].tolist(), lab[both].tolist())):
                bodies.join((n - 1, a), (n, b))
        if n == 1:
            b = grow(raster(brim, shape, origin)[0], 1.0)
            for k, sl in enumerate(ndimage.find_objects(lab), 1):
                length = max(sl[0].stop - sl[0].start, sl[1].stop - sl[1].start) * RES
                if length > FOOT_L:
                    m = lab == k
                    if width_mm(m) < FOOT_W and not (b & m).any():
                        foot.append([m, sizes[k], width_mm(m), length, 0.0])
        elif foot and z <= FOOT_Z:
            for f in foot:
                ks = np.unique(lab[f[0]])
                ks = ks[ks > 0]
                if len(ks):
                    f[4] = max(f[4], sizes[ks].max())
        if prev is not None and count:
            below = grow(prev, SUPPORT_R)
            held = np.bincount(lab.ravel(), weights=below.ravel(), minlength=count + 1)[1:]
            for k in idx[(sizes[1:] >= FLOAT_MIN) & (held == 0)]:
                add("floating", n, z, lab == k, f"{sizes[k]:.0f} mm2 starts in mid-air at Z {z:.2f} "
                                                f"(nothing printed under it)", g)
            free = g & ~below
            fl, fc = ndimage.label(free)
            if fc:
                fsz = np.bincount(fl.ravel()) * cell
                for k in np.nonzero(fsz > SPAN_W * SPAN_W * 0.7)[0]:
                    if k:
                        m = fl == k
                        w = width_mm(m)
                        if w > SPAN_W:
                            add("span", n, z, m, f"{w:.0f} mm wide patch printed over nothing at Z {z:.2f} "
                                                 f"({fsz[k]:.0f} mm2)", g)
        if count > 1:
            grown, gc = ndimage.label(grow(g, fuse), structure=EIGHT)
            if gc < count:
                owner = np.zeros(count + 1, int)
                owner[lab.ravel()] = grown.ravel()  # grown region of each region
                owner = owner[1:]
                tools = np.stack([np.bincount(lab.ravel(), weights=(tg.ravel() == t), minlength=count + 1)[1:]
                                  for t in range(1, 5)]) > 0
                one = np.where(tools.sum(0) == 1, tools.argmax(0), -1)  # the tool of a one-tool region
                for gk in np.nonzero(np.bincount(owner, minlength=gc + 1) > 1)[0]:
                    ks = idx[(owner == gk) & (sizes[1:] >= FLOAT_MIN)]
                    for i, a in enumerate(ks):
                        for b2 in ks[i + 1:]:
                            # different tools side by side = a designed multi-material contact (filler, inlay)
                            if one[a - 1] >= 0 and one[a - 1] == one[b2 - 1]:
                                near.append((n, z, int(a), int(b2)))
        prev, prev_lab = g, lab
    redo = {}
    for n, z, a, b2 in near:
        if bodies.find((n, a)) != bodies.find((n, b2)):
            if n not in redo:  # re-raster only the layers that need a picture (labels come out the same)
                g = raster(data[n - 1][1], shape, origin)[0]
                redo = {n: (g, ndimage.label(g, structure=EIGHT)[0])}
            g, lab = redo[n]
            touch = grow(lab == a, fuse) & grow(lab == b2, fuse)
            if not touch.any():  # a and b2 only share a grown region through a third piece
                continue
            add("fused", n, z, touch, f"two separate pieces under {FUSE_GAP:.1f} mm apart at Z {z:.2f} "
                                      f"- they may weld together", g)
    for m, a, w, length, grew in foot:
        if grew >= FOOT_GROW * a:
            add("footprint", 1, data[0][0], m, f"first-layer strip {w:.0f} mm wide x {length:.0f} mm long carries a "
                                               f"{grew:.0f} mm2 region by Z {FOOT_Z:.0f} ({grew / a:.0f}x its own "
                                               f"area) - it can curl off the plate", raster(data[0][1], shape, origin)[0])
    if info is not None:
        info["pieces"] = len({bodies.find(r) for r in real})
    return merge(out)


def merge(found):
    """One finding per check and place: the same problem repeats on many layers."""
    seen, keep = {}, []
    for f in found:
        key = (f["check"], f["where"])
        if key in seen:
            seen[key]["layers"] += 1
            continue
        f["layers"] = 1
        seen[key] = f
        keep.append(f)
    return keep


def save_png(path, grid, mark):
    """Grey = that layer, red = the problem. Flipped so +Y is up, like the slicer."""
    img = np.full(grid.shape + (3,), 255, np.uint8)
    img[grid] = (150, 150, 150)
    img[ndimage.binary_dilation(mark, iterations=3)] = (220, 30, 30)
    img = np.ascontiguousarray(img[::-1])
    raw = b"".join(b"\x00" + row.tobytes() for row in img)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    head = struct.pack(">IIBBBBB", img.shape[1], img.shape[0], 8, 2, 0, 0, 0)
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(raw, 6))
                           + chunk(b"IEND", b""))


def reasons(found):
    return [f"design check {f['check']}: {f['message']} ({f['where']}, layer {f['layer']}"
            + (f", {f['layers']} layers" if f["layers"] > 1 else "") + ")" for f in found]


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import slicer
    png = sys.argv[sys.argv.index("--png") + 1] if "--png" in sys.argv else None
    info = {}
    found = check(slicer.read_gcode(sys.argv[1]), png, info)
    print("\n".join(reasons(found)) or "design check: nothing found")
    print(f"prints as {info.get('pieces', 0)} separate pieces")
    sys.exit(1 if found else 0)
