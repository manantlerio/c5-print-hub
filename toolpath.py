# 3D toolpath for the phone's spin viewer: a sliced file's extrusion moves as line segments, coloured by tool.
# Infill and supports are left out (they hide the part). Big files keep every Nth layer, always the top one.
# Each vertex carries a surface normal so the viewer can light the lines like a solid: from a top-down height map
# where the line is on the upper skin (letters, chamfers, domes), flat on a flat bottom, else level and square to it.
# Output (cached next to the G-code, gzipped): b"TP2\0", uint32 header length, JSON header padded to 4 bytes,
# then per tool in header order: uint16 x0 y0 z0 x1 y1 z1 per segment, in 0.01 mm steps from header "origin",
# then int8 nx ny nz per vertex (127 = 1).
import array, gzip, io, json, math, os, struct, sys, zipfile

VERSION = 2
BUDGET = 300_000   # segments sent to the phone at most (about 3.6 MB raw, less gzipped)
MIN_LEN = 0.5      # mm: shorter extrusion moves are joined into the next one
ARC_STEP = 1.0     # mm per chord when drawing G2/G3 arcs
HIDE = ("sparse infill", "internal", "support", "custom")  # ;TYPE: names left out (lower case, substring)
CELL = 0.4         # mm: height map grid


def open_lines(path):
    """Lines of plain G-code or of the plate G-code inside a sliced .3mf, streamed, with the total size."""
    if path.lower().endswith(".3mf"):
        z = zipfile.ZipFile(path)
        name = next((n for n in z.namelist() if n.startswith("Metadata/plate_") and n.endswith(".gcode")), None)
        if not name:
            raise ValueError("no sliced plate in this 3MF")
        return io.TextIOWrapper(z.open(name), encoding="utf-8", errors="ignore"), z.getinfo(name).file_size
    return open(path, encoding="utf-8", errors="ignore"), os.path.getsize(path)


def build(path, progress=None):
    """Parse the G-code at path; returns the gzipped toolpath bytes. progress(fraction) is called now and then."""
    f, total = open_lines(path)
    layers = []          # per layer: {tool: array('f') of x0 y0 z0 x1 y1 z1}
    cur = {}
    markers = None       # True once a ;LAYER_CHANGE line is seen; else layers follow Z
    last_z = None
    x = y = z = e = 0.0
    rel_xyz, rel_e, tool, hidden = False, False, 0, False
    cs = None            # start of the current joined run (x, y, z) or None
    cl = None            # last point of the current run
    seen = 0

    def flush():
        nonlocal cs
        if cs is not None and cl != cs:
            cur.setdefault(tool, array.array("f")).extend((cs[0], cs[1], cs[2], cl[0], cl[1], cl[2]))
        cs = None

    def new_layer():
        nonlocal cur
        flush()
        if cur:
            layers.append(cur)
        cur = {}

    def extrude(ax, ay, az, bx, by, bz):
        nonlocal cs, cl
        if cs is None or cl != (ax, ay, az):
            flush()
            cs = (ax, ay, az)
        cl = (bx, by, bz)
        if (bx - cs[0]) ** 2 + (by - cs[1]) ** 2 + (bz - cs[2]) ** 2 >= MIN_LEN * MIN_LEN:
            cur.setdefault(tool, array.array("f")).extend((cs[0], cs[1], cs[2], bx, by, bz))
            cs = cl

    with f:
        for n, line in enumerate(f):
            seen += len(line)
            if progress and n % 200_000 == 0:
                progress(min(0.8, 0.8 * seen / max(1, total)))
            c = line[:1]
            if c == ";":
                if line.startswith(";LAYER_CHANGE"):
                    markers = True
                    new_layer()
                elif line.startswith(";TYPE:") or line.startswith("; FEATURE:"):
                    flush()
                    kind = line.split(":", 1)[1].strip().lower()
                    hidden = any(h in kind for h in HIDE)
                continue
            if c == "T" and line[1:2].isdigit():
                flush()
                tool = int(line[1:].split(";")[0].split()[0])
                continue
            if c == "M":
                cmd = line.split(";")[0].split()
                if cmd and cmd[0] == "M83":
                    rel_e = True
                elif cmd and cmd[0] == "M82":
                    rel_e = False
                continue
            if c != "G":
                continue
            parts = line.split(";")[0].split()
            if not parts:
                continue
            g = parts[0]
            if g == "G90":
                rel_xyz = False
                continue
            if g == "G91":
                rel_xyz = True
                continue
            if g == "G92":
                for p in parts[1:]:
                    if p[:1] == "E":
                        e = float(p[1:] or 0)
                continue
            if g not in ("G1", "G0", "G2", "G3", "G01", "G00", "G02", "G03"):
                continue
            a = {}
            for p in parts[1:]:
                try:
                    a[p[0]] = float(p[1:])
                except (ValueError, IndexError):
                    pass
            if rel_xyz:
                nx, ny, nz = x + a.get("X", 0.0), y + a.get("Y", 0.0), z + a.get("Z", 0.0)
            else:
                nx, ny, nz = a.get("X", x), a.get("Y", y), a.get("Z", z)
            de = 0.0
            if "E" in a:
                de = a["E"] if rel_e else a["E"] - e
                e = e + a["E"] if rel_e else a["E"]
            if de > 0 and g not in ("G0", "G00") and (nx, ny) != (x, y):
                if not markers and (last_z is None or nz > last_z + 0.005):
                    new_layer()
                    last_z = nz
                if not hidden:
                    if g in ("G2", "G3", "G02", "G03") and ("I" in a or "J" in a):
                        cx, cy = x + a.get("I", 0.0), y + a.get("J", 0.0)
                        r = math.hypot(x - cx, y - cy)
                        a0, a1 = math.atan2(y - cy, x - cx), math.atan2(ny - cy, nx - cx)
                        sweep = a1 - a0
                        if g in ("G2", "G02"):  # clockwise
                            sweep = sweep if sweep < 0 else sweep - 2 * math.pi
                        else:
                            sweep = sweep if sweep > 0 else sweep + 2 * math.pi
                        k = max(1, min(64, int(abs(sweep) * r / ARC_STEP) + 1))
                        px, py, pz = x, y, z
                        for i in range(1, k + 1):
                            t = a0 + sweep * i / k
                            qx, qy = (cx + r * math.cos(t), cy + r * math.sin(t)) if i < k else (nx, ny)
                            qz = z + (nz - z) * i / k
                            extrude(px, py, pz, qx, qy, qz)
                            px, py, pz = qx, qy, qz
                    else:
                        extrude(x, y, z, nx, ny, nz)
            elif (nx, ny, nz) != (x, y, z):
                flush()
            x, y, z = nx, ny, nz
    new_layer()

    count = sum(len(v) // 6 for L in layers for v in L.values())
    stride = max(1, math.ceil(count / BUDGET))
    keep = [L for i, L in enumerate(layers) if (len(layers) - 1 - i) % stride == 0]
    shade = Shade(layers)
    per_tool, per_norm = {}, {}
    for n, L in enumerate(keep):
        if progress and n % 20 == 0:
            progress(0.8 + 0.19 * n / len(keep))
        for t, v in L.items():
            per_tool.setdefault(t, []).append(v)
            per_norm.setdefault(t, array.array("b")).extend(shade.normals(v))
    lo = [math.inf] * 3
    for vs in per_tool.values():
        for v in vs:
            for k in range(3):
                lo[k] = min(lo[k], min(v[k::3]))
    lo = [0.0, 0.0, 0.0] if lo[0] == math.inf else [math.floor(q) for q in lo]
    tools, body = [], []
    for t in sorted(per_tool):
        q = array.array("H")
        for v in per_tool[t]:
            q.extend(min(65535, max(0, int(round((v[i] - lo[i % 3]) * 100)))) for i in range(len(v)))
        if sys.byteorder == "big":
            q.byteswap()
        tools.append([t + 1, len(q) // 6])
        body.append(q.tobytes() + per_norm[t].tobytes())
    head = json.dumps({"v": VERSION, "scale": 0.01, "origin": lo, "tools": tools, "layers": len(layers),
                       "stride": stride, "segments": sum(n for _, n in tools), "normals": "int8",
                       "hidden": "infill and supports"}).encode()
    head += b" " * (-len(head) % 4)
    raw = b"TP2\0" + struct.pack("<I", len(head)) + head + b"".join(body)
    if progress:
        progress(1.0)
    return gzip.compress(raw, 6)


class Shade:
    """Top-down height maps of every shown line (highest and lowest Z per CELL), used to pick vertex normals."""

    def __init__(self, layers):
        xs, ys, zs = [], [], []
        for L in layers:
            for v in L.values():
                xs += (min(v[0::3]), max(v[0::3]))
                ys += (min(v[1::3]), max(v[1::3]))
                zs += (min(v[2::3]),)
        self.ok = bool(xs)
        if not self.ok:
            return
        self.x0, self.y0, self.bed = min(xs) - 3 * CELL, min(ys) - 3 * CELL, min(zs)
        self.w = int((max(xs) - self.x0) / CELL) + 4
        self.h = int((max(ys) - self.y0) / CELL) + 4
        lows = sorted({round(z, 3) for z in zs})
        steps = sorted(b - a for a, b in zip(lows, lows[1:]) if b - a > 0.01)
        lh = steps[len(steps) // 2] if steps else 0.2   # typical layer height
        self.tol = 1.5 * lh + CELL                       # this close under the map's top (or over its bottom) = skin
        none = -1e9
        top = array.array("f", [none]) * (self.w * self.h)
        bot = array.array("f", [-none]) * (self.w * self.h)
        x0, y0, w, step = self.x0, self.y0, self.w, CELL / 2
        for L in layers:
            for v in L.values():
                for k in range(0, len(v), 6):
                    ax, ay, az, bx, by, bz = v[k:k + 6]
                    m = int(math.hypot(bx - ax, by - ay) / step) + 1
                    for i in range(m + 1):
                        f = i / m
                        c = int((ay + (by - ay) * f - y0) / CELL) * w + int((ax + (bx - ax) * f - x0) / CELL)
                        z = az + (bz - az) * f
                        if z > top[c]:
                            top[c] = z
                        if z < bot[c]:
                            bot[c] = z
        # one pass closes the odd cell missed between neighbouring lines, then empty = bed (top) / unknown (bottom)
        for grid, empty, pick in ((top, none, max), (bot, -none, min)):
            src = grid[:]
            for c in range(w + 1, len(grid) - w - 1):
                if src[c] == empty:
                    near = [src[c + d] for d in (1, -1, w, -w) if src[c + d] != empty]
                    if near:
                        grid[c] = pick(near)
        self.top = array.array("f", (self.bed if z == none else z for z in top))
        self.bot = bot
        self.none = -none

    def normal(self, x, y, z, px, py):
        """Unit normal at a vertex; (px, py) = the level normal square to its line, used off the skins."""
        w = self.w
        i = min(self.w - 3, max(2, int((x - self.x0) / CELL)))
        j = min(self.h - 3, max(2, int((y - self.y0) / CELL)))
        c, top = j * w + i, self.top
        if z >= top[c] - self.tol:  # upper skin: slope of the height map over +-2 cells, 3 rows wide
            gx = sum(top[c + r + 2] - top[c + r - 2] for r in (-w, 0, w)) / (12 * CELL)
            gy = sum(top[c + 2 * w + r] - top[c - 2 * w + r] for r in (-1, 0, 1)) / (12 * CELL)
            return -gx, -gy, 1.0
        b = self.bot
        if z <= b[c] + self.tol and all(abs(b[c + d] - b[c]) <= self.tol for d in (2, -2, 2 * w, -2 * w)):
            return 0.0, 0.0, 1.0   # flat bottom skin (the viewer lights both faces alike)
        return px, py, 0.0

    def normals(self, v):
        """int8 normals for a flat x0 y0 z0 x1 y1 z1 segment array, one per vertex."""
        out = []
        for k in range(0, len(v), 6):
            ax, ay, az, bx, by, bz = v[k:k + 6]
            d = math.hypot(bx - ax, by - ay)
            px, py = ((by - ay) / d, (ax - bx) / d) if d > 1e-6 else (0.0, 0.0)
            for x, y, z in ((ax, ay, az), (bx, by, bz)):
                nx, ny, nz = self.normal(x, y, z, px, py) if self.ok else (px, py, 0.0)
                m = math.sqrt(nx * nx + ny * ny + nz * nz)
                if m < 1e-6:
                    nx, ny, nz, m = 0.0, 0.0, 1.0, 1.0
                out += (int(round(127 * nx / m)), int(round(127 * ny / m)), int(round(127 * nz / m)))
        return out
