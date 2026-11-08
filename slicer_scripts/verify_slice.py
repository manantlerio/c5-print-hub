# Compare a CLI-sliced G-code against a real Flash Studio GUI export from the same printer.
# Usage: python verify_slice.py <gui-export.3mf> <candidate.gcode>
# Prints: config diff, start/end sequence diff, temperatures, XYZ bounds, extrusion per mm, and the
# area each tool prints (to prove the colour-to-tool mapping). Read the output before sending a file to the printer.
import math, re, statistics, sys, zipfile

ref_txt = zipfile.ZipFile(sys.argv[1]).read("Metadata/plate_1.gcode").decode(errors="ignore")
new_txt = open(sys.argv[2], encoding="utf-8", errors="ignore").read()
REF, NEW = ref_txt.splitlines(), new_txt.splitlines()
EXPECTED = {"layer_height", "filament_colour", "filament_multi_colour", "filament_self_index", "filament_map",
            "flush_volumes_matrix", "flush_volumes_vector", "flush_multiplier", "extruder_colour", "print_settings_id",
            "enable_prime_tower", "independent_support_layer_height", "extruder_ams_count", "inherits_group",
            "different_settings_to_system", "default_filament_colour", "filament_notes", "volumetric_speed_coefficients",
            "filament_change_extrusion_role_gcode"}


def cfg(lines):
    d, on = {}, False
    for l in lines:
        if l.startswith("; CONFIG_BLOCK_START"): on = True; continue
        if l.startswith("; CONFIG_BLOCK_END"): break
        if on and " = " in l:
            k, v = l[2:].split(" = ", 1); d[k] = v
    return d


R, N = cfg(REF), cfg(NEW)
print("== CONFIG")
bad = 0
for k in sorted(set(R) | set(N)):
    a, b = R.get(k), N.get(k)
    if a == b: continue
    if a is not None and b is not None and any(b == ";".join([a] * n) or b == ",".join([a] * n) for n in range(2, 9)):
        continue  # per-filament value repeated once per colour
    tag = "expected" if k in EXPECTED else "CHECK"
    bad += tag == "CHECK"
    print(f"  [{tag}] {k}: gui={str(a)[:80]} | new={str(b)[:80]}")
print(f"  {len(set(R) | set(N))} keys compared, {bad} unexpected differences")


def cmds(lines):
    return [l.split(";")[0].strip() for l in lines if l.split(";")[0].strip()]


def start(lines):
    i = next(i for i, l in enumerate(lines) if l.startswith(";LAYER_CHANGE"))
    return cmds(lines[:i])


print("== START SEQUENCE (gui | new)")
for a, b in zip(start(REF), start(NEW)):
    print(("   same  " if a == b else "   DIFF  ") + f"{a:40s} | {b}")
print("== END SEQUENCE, last 8 commands (gui | new)")
for a, b in zip(cmds(REF)[-8:], cmds(NEW)[-8:]):
    print(("   same  " if a == b else "   DIFF  ") + f"{a:40s} | {b}")
for c in ("M104", "M109", "M140", "M190", "M141", "M191"):
    f = lambda t: sorted(set(re.findall(rf"^{c} S(\d+)", t, re.M)))
    print(f"== {c}: gui={f(ref_txt)} new={f(new_txt)}")
xs, ys, zs = [], [], []
for l in cmds(NEW):
    if l.startswith(("G0", "G1")):
        for ax, arr in (("X", xs), ("Y", ys), ("Z", zs)):
            m = re.search(rf"{ax}(-?[\d.]+)", l)
            if m: arr.append(float(m.group(1)))
print(f"== BOUNDS new: X {min(xs):.1f}..{max(xs):.1f} Y {min(ys):.1f}..{max(ys):.1f} Z {min(zs):.2f}..{max(zs):.2f} (bed 0..256)")


def eratio(lines):
    x = y = None; vals = []
    for c in cmds(lines):
        if not c.startswith("G1"): continue
        mx, my, me = re.search(r"X(-?[\d.]+)", c), re.search(r"Y(-?[\d.]+)", c), re.search(r"E(-?[\d.]+)", c)
        nx = float(mx.group(1)) if mx else x; ny = float(my.group(1)) if my else y
        if me and None not in (x, y, nx, ny):
            d = math.hypot(nx - x, ny - y); e = float(me.group(1))
            if d > 2 and e > 0: vals.append(e / d)
        x, y = nx, ny
    return statistics.median(vals)


def expected_e(w, h, dia=1.75):  # Orca's rounded-rectangle bead area / filament area
    return ((w - h) * h + math.pi * (h / 2) ** 2) / (math.pi * (dia / 2) ** 2)


for name, lines, c in (("gui", REF, R), ("new", NEW, N)):
    h, w = float(c["layer_height"]), float(c.get("outer_wall_line_width", "0.42").rstrip("%") or 0.42)
    print(f"== EXTRUSION {name}: median E/mm {eratio(lines):.5f}, formula for {w} x {h} mm bead {expected_e(w, h):.5f}, "
          f"filament_diameter {c.get('filament_diameter')}")
print("== AREA PRINTED PER TOOL (new), prime tower and purge line excluded")
t = None; x = y = None; box = {}
for c in cmds(NEW):
    m = re.match(r"^T(\d)$", c)
    if m: t = int(m.group(1)); continue
    if not c.startswith("G1"): continue
    mx, my, me = re.search(r"X(-?[\d.]+)", c), re.search(r"Y(-?[\d.]+)", c), re.search(r"E(-?[\d.]+)", c)
    x = float(mx.group(1)) if mx else x; y = float(my.group(1)) if my else y
    if me and float(me.group(1)) > 0 and t is not None and x is not None and y is not None and y > 1:
        tower_x = float(N.get("wipe_tower_x", "165").split(",")[0]); tower_y = float(N.get("wipe_tower_y", "218").split(",")[0])
        if tower_x - 5 < x < tower_x + 45 and tower_y - 25 < y < 256: continue
        b = box.setdefault(t, [999, 999, -999, -999])
        b[:] = [min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y)]
for k, b in sorted(box.items()):
    print(f"  T{k}: X {b[0]:.1f}..{b[2]:.1f}  Y {b[1]:.1f}..{b[3]:.1f}")
print("== SEAMS on colour tools (base tool skipped): wall loop starts that stack up or double up")
# Caddy badge 2026-10-27: the yellow rim's outer loop started at the same corner on all 3 inlay layers (and the
# inner loop too on the first), so the seam gaps stacked into a hole down to the black. Any hit here on a show face
# needs the seam moved (per-object seam_gap 0, staggered_inner_seams, or a deliberate notch to take it).
t = None; z = 0.0; kind = ""; x = y = None; run = False; starts = []; used = {}
for l in NEW:
    if l.startswith(";TYPE:"): kind = l[6:].strip(); run = False; continue
    c = l.split(";")[0].strip()
    m = re.match(r"^T(\d)$", c)
    if m: t = int(m.group(1)); run = False; continue
    if not c.startswith(("G1", "G0")): continue
    mx, my, mz, me = (re.search(p + r"(-?[\d.]+)", c) for p in ("X", "Y", "Z", "E"))
    nx = float(mx.group(1)) if mx else x; ny = float(my.group(1)) if my else y
    z = float(mz.group(1)) if mz else z
    ext = bool(me) and float(me.group(1)) > 0 and (nx, ny) != (x, y)
    if ext and t is not None:
        used[t] = used.get(t, 0) + float(me.group(1))
        if not run and kind in ("Outer wall", "Inner wall"):
            starts.append((t, round(z, 2), kind, x, y))
    run = ext
    x, y = nx, ny
base = max(used, key=used.get) if used else None
hits = 0
for tool in sorted(set(s[0] for s in starts) - {base}):
    pts = [s for s in starts if s[0] == tool]
    done = set()
    for i, (_, z0, k0, x0, y0) in enumerate(pts):
        if i in done: continue
        near = [j for j, s in enumerate(pts) if math.hypot(s[3] - x0, s[4] - y0) < 0.8]
        done.update(near)
        layers = sorted({pts[j][1] for j in near})
        double = len(near) > len(layers)
        if len(layers) >= 2 or double:
            hits += 1
            print(f"  WARN T{tool}: {len(near)} loop starts at X {x0:.1f} Y {y0:.1f} on {len(layers)} layers "
                  f"(Z {layers[0]}..{layers[-1]}){', 2 loops on one layer' if double else ''}")
print(f"  {hits} stacked seam spots" + (" - check each is on a hidden face" if hits else ""))
