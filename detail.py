# Detail mode: a variable layer height profile built from the model's slopes. Fine layers only where a sloped or
# curved surface would show stair-steps; vertical walls and flat tops keep the standard layer (no gain there).
# The profile goes into the slice input as Metadata/layer_heights_profile.txt, which Orca/Flash Studio read
# per object ("object_id=1|z;h;z;h;..."), and the slicer interpolates between points.
import bisect, json, math, re, struct, zipfile

FINE = 0.1     # thinnest layer; the printer profile allows 0.08
CUSP = 0.1     # allowed stair-step depth: what 0.2 layers leave on a 60 deg wall (0.2 x cos 60)
FLAT = 0.995   # |nz| above this is a flat face: no stair-step, layers do not help
STEP = 0.05    # profile sampling, mm
RAMP = 0.1     # max change in layer height per mm of Z, so the switch is gradual, not a visible band
MIN_AREA = 4.0 # mm2 of sloped surface in a 0.5 mm band before it earns fine layers (skips tessellation noise)


def triangles(path):
    """Yield (v0, v1, v2) from an ASCII or binary STL."""
    data = open(path, "rb").read()
    n = struct.unpack("<I", data[80:84])[0] if len(data) >= 84 else -1
    if len(data) == 84 + 50 * n:
        for i in range(n):
            v = struct.unpack("<12f", data[84 + 50 * i:84 + 50 * i + 48])
            yield v[3:6], v[6:9], v[9:12]
        return
    vs = [tuple(map(float, l.split()[1:4])) for l in data.decode("ascii", "ignore").splitlines()
          if l.strip().startswith("vertex")]
    for i in range(0, len(vs) - 2, 3):
        yield vs[i], vs[i + 1], vs[i + 2]


def profile(paths, base=0.2):
    """Return ([(z, h)...], info) for the parts as one object, z from the object's bottom. base = the job's layer,
    used as the ceiling: Detail only ever adds layers, it never coarsens what the standard slice would print."""
    tris = [t for p in paths for t in triangles(p)]
    z0 = min(v[2] for t in tris for v in t)
    top = max(v[2] for t in tris for v in t) - z0
    n = int(top / STEP) + 2
    need = [base] * n
    area = [0.0] * n
    for a, b, c in tris:
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        s = math.sqrt(nx * nx + ny * ny + nz * nz)
        if s < 1e-9:
            continue
        cz = abs(nz) / s
        if cz > FLAT:
            continue
        h = max(FINE, min(base, CUSP / cz)) if cz > 1e-6 else base
        if h >= base:
            continue
        lo, hi = min(a[2], b[2], c[2]) - z0, max(a[2], b[2], c[2]) - z0
        i0, i1 = int(lo / STEP), min(n - 1, int(hi / STEP) + 1)
        share = 0.5 * s / max(1, i1 - i0)
        for i in range(i0, i1 + 1):
            need[i] = min(need[i], h)
            area[i] += share
    # A band earns fine layers only if enough sloped surface sits in it (0.5 mm window).
    w = int(0.5 / STEP)
    req = []
    for i in range(n):
        a = sum(area[max(0, i - w):i + w + 1])
        req.append(need[i] if a >= MIN_AREA else base)
    # Start fine layers a little early and ramp out gradually, both directions.
    h = req[:]
    for i in range(1, n):
        h[i] = min(h[i], h[i - 1] + RAMP * STEP)
    for i in range(n - 2, -1, -1):
        h[i] = min(h[i], h[i + 1] + RAMP * STEP)
    pts = []
    for i, v in enumerate(h):
        z, v = round(i * STEP, 3), round(v, 3)
        if z >= top:
            break
        if len(pts) >= 2 and pts[-1][1] == v == pts[-2][1]:
            pts[-1] = (z, v)
        else:
            pts.append((z, v))
    # The slicer drops a profile whose last point is not exactly the object's top.
    pts.append((top, pts[-1][1]))
    fine = sum(STEP for v in h if v < base - 1e-6)
    return pts, {"height": round(top, 2), "fine_mm": round(fine, 2), "min": round(min(h), 3),
                 "layers_est": round(sum(STEP / v for v in h)), "layers_plain": round(top / base)}


def write_profile(slice_3mf, pts):
    """Add the profile for object 1 (make_3mf.py builds one object) to the prepared slice input. The slicer also
    drops a profile whose first height is not the first layer height, so that comes from the file's own settings."""
    with zipfile.ZipFile(slice_3mf) as z:
        first = json.loads(z.read("Metadata/project_settings.config"))["initial_layer_print_height"]
    first = float(first)
    # The slicer interpolates between points, so pin the height just above the first layer as well.
    k = next(i for i, p in enumerate(pts) if p[0] > first)
    (z1, h1), (z2, h2) = pts[k - 1], pts[k]
    pts = [(0.0, first), (first, h1 + (h2 - h1) * (first - z1) / (z2 - z1))] + pts[k:]
    text = "object_id=1|" + ";".join(f"{z:f};{h:f}" for z, h in pts) + "\n"
    with zipfile.ZipFile(slice_3mf, "a", zipfile.ZIP_DEFLATED) as z:
        if "Metadata/layer_heights_profile.txt" in z.namelist():
            raise ValueError("slice input already has a layer profile")
        z.writestr("Metadata/layer_heights_profile.txt", text)


def stair_step(paths, gcode):
    """Area-weighted mean stair-step depth (mm) on the sloped faces, from the layers the G-code really prints:
    layer height x |nz| per face. Lower is smoother. Flat and vertical faces are left out (no stair-step)."""
    layers = [(float(z), float(h)) for z, h in re.findall(r"^;LAYER_CHANGE\n;Z:([\d.]+)\n;HEIGHT:([\d.]+)",
                                                                    gcode, re.M)]
    if not layers:
        return None
    tops = [z for z, _ in layers]
    tris = [t for p in paths for t in triangles(p)]
    z0 = min(v[2] for t in tris for v in t)
    num = den = 0.0
    for a, b, c in tris:
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        s = math.sqrt(nx * nx + ny * ny + nz * nz)
        if s < 1e-9:
            continue
        cz = abs(nz) / s
        if cz > FLAT or cz < 0.05:
            continue
        zc = (a[2] + b[2] + c[2]) / 3 - z0
        h = layers[min(bisect.bisect_left(tops, zc), len(layers) - 1)][1]
        num += 0.5 * s * h * cz
        den += 0.5 * s
    return round(num / den, 3) if den else None


if __name__ == "__main__":
    import sys
    pts, info = profile(sys.argv[1:])
    print(info)
    for z, h in pts:
        print(f"{z:7.2f} {h:.3f}")
