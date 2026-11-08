# Slice STL parts with the Flash Studio CLI (bundled slicer_scripts), gate the result against your own Flash Studio
# export, and read stats and a top-view preview out of G-code. Stdlib only.
import json, math, os, re, struct, subprocess, sys, zipfile, zlib
import detail

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "slicer_scripts")
# Your own Flash Studio export (a plate you sliced in the Flash Studio app on your PC). The setup page copies it here.
# Every slice the hub makes takes its settings from this file and is checked against it.
TEMPLATE = os.path.join(HERE, "template", "template.3mf")
FLASH_STUDIO = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Flashforge", "Flash Studio Desktop",
                            "flash studio.exe")
# ForkaSlicer (an OrcaSlicer fork) run in WSL. Off by default; set "engine": "forkaslicer" and "forkaslicer_path"
# (the orca-slicer binary inside WSL) in config.json to try it. It falls back to Flash Studio if it fails.
FORKA = ""


def configure(cfg):
    """Paths from config.json, when set there."""
    global TEMPLATE, FLASH_STUDIO, FORKA
    TEMPLATE = cfg.get("template") or TEMPLATE
    FLASH_STUDIO = cfg.get("flash_studio") or FLASH_STUDIO
    FORKA = cfg.get("forkaslicer_path") or FORKA


def template_ready():
    return os.path.isfile(TEMPLATE)


def check_template(path):
    """Problems with a Flash Studio export as a template (empty list = good)."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "Metadata/project_settings.config" not in names:
                return ["no Flash Studio settings in it (Metadata/project_settings.config)"]
            if "Metadata/plate_1.gcode" not in names:
                return ["no sliced plate in it - slice the plate in Flash Studio first"]
            cfg = json.loads(z.read("Metadata/project_settings.config"))
            head = z.read("Metadata/plate_1.gcode")[:4000].decode(errors="ignore")
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        return [f"not a readable 3MF: {e}"]
    out = []
    if "Flash Studio" not in head and "Orca-Flashforge" not in head and "OrcaSlicer" not in head:
        out.append("not sliced by Flash Studio")
    if "Creator 5" not in json.dumps(cfg.get("printer_settings_id", "")) + json.dumps(cfg.get("printer_model", "")):
        out.append(f"printer profile is {cfg.get('printer_settings_id')}, not a Creator 5 Pro")
    n = len(cfg.get("filament_settings_id") or [])
    if n != 4:
        out.append(f"{n} filament(s) in it - set up all 4 filaments in Flash Studio before you slice")
    return out


NO_WINDOW = 0x08000000
# Settings Flash Studio 1.7.19 writes that upstream OrcaSlicer does not have, with Flash Studio's values (all off).
# A ForkaSlicer file may lack these; anything else missing, or Flash Studio's value changing, still fails the gate.
FS_ONLY = {"dithering_local_z_direct_multicolor": "0", "dithering_local_z_mode": "0",
           "dithering_local_z_whole_objects": "0", "dithering_step_painted_zones_only": "1",
           "dithering_z_step_size": "0", "enable_infill_filament_override": "0",
           "infill_filament_use_base_first_layers": "0", "infill_filament_use_base_last_layers": "0",
           "local_z_wipe_tower_purge_lines": "3", "mixed_color_layer_height_a": "0",
           "mixed_color_layer_height_b": "0", "mixed_filament_advanced_dithering": "0",
           "mixed_filament_component_bias_enabled": "0", "mixed_filament_definitions": "",
           "mixed_filament_gradient_mode": "0", "mixed_filament_height_lower_bound": "0.04",
           "mixed_filament_height_upper_bound": "0.16", "mixed_filament_pointillism_line_gap": "0",
           "mixed_filament_pointillism_pixel_size": "0", "mixed_filament_region_collapse": "1",
           "mixed_filament_surface_indentation": "0", "solid_infill_filament": "0", "sparse_infill_filament": "0",
           "wall_filament": "0"}


def run(args, log, timeout=900, cwd=None):
    p = subprocess.run(args, capture_output=True, text=True, errors="ignore", timeout=timeout,
                       creationflags=NO_WINDOW, cwd=cwd)
    log.append(f"$ {' '.join(os.path.basename(a) if i == 0 else a for i, a in enumerate(args))}\n"
               f"{p.stdout[-4000:]}{p.stderr[-2000:]}exit {p.returncode}")
    return p


def to_ascii_stl(src, dst):
    """make_3mf.py reads ASCII STL; most downloaded STLs are binary."""
    data = open(src, "rb").read()
    n = struct.unpack("<I", data[80:84])[0] if len(data) >= 84 else -1
    if len(data) == 84 + 50 * n:
        with open(dst, "w", encoding="ascii") as f:
            f.write("solid part\n")
            for i in range(n):
                v = struct.unpack("<12f", data[84 + 50 * i:84 + 50 * i + 48])
                f.write(f"facet normal {v[0]:e} {v[1]:e} {v[2]:e}\nouter loop\n")
                for k in (3, 6, 9):
                    f.write(f"vertex {v[k]:.5f} {v[k + 1]:.5f} {v[k + 2]:.5f}\n")
                f.write("endloop\nendfacet\n")
            f.write("endsolid part\n")
    else:
        open(dst, "wb").write(data)


def wsl_path(p):
    """C:\\Users\\x -> /mnt/c/Users/x"""
    p = os.path.abspath(p)
    return f"/mnt/{p[0].lower()}{p[2:].replace(os.sep, '/')}"


def slice_with(engine, inp, out, log):
    """Run one slicer on the prepared 3MF. Returns the exit code."""
    if engine == "forkaslicer":
        if not FORKA:
            log.append("forkaslicer_path is not set in config.json")
            return 1
        # cwd = out: the CLI writes result.json in its working folder.
        return run(["wsl.exe", "-e", FORKA, "--slice", "0", "--outputdir", wsl_path(out), wsl_path(inp)], log,
                   cwd=out).returncode
    return run([FLASH_STUDIO, "--slice", "0", "--outputdir", out, inp], log).returncode


def slice_parts(work, parts, colours, layer="0.2", engine="flashstudio", fine=False):
    """parts: [(name, stl_path, tool 1-4)]. Returns dict with gcode path, verify text, gate result and the engine used.
    engine 'forkaslicer' falls back to Flash Studio if it fails; the result says so.
    fine = Detail mode: thinner layers where slopes would show stair-steps (detail.py)."""
    log = []
    if not template_ready():
        return {"ok": False, "error": "no slicing template yet: open Setup, step 4 (your Flash Studio export)", "log": log}
    if not os.path.isfile(FLASH_STUDIO):
        return {"ok": False, "error": f"Flash Studio not found at {FLASH_STUDIO} (set flash_studio in config.json)",
                "log": log}
    os.makedirs(work, exist_ok=True)
    args = []
    for i, (name, path, tool) in enumerate(parts):
        a = os.path.join(work, f"part{i}.stl")
        to_ascii_stl(path, a)
        args.append(f"{re.sub(r'[^A-Za-z0-9_-]', '_', name) or 'part'}:{a}:{tool}")
    model = os.path.join(work, "model.3mf")
    out = os.path.join(work, "out")
    py = sys.executable
    # One object per separate piece (cancel one piece mid-print) when the template has exclude_object on, except
    # Detail: the slicer refuses a purge tower when objects of different heights carry a variable layer profile.
    with zipfile.ZipFile(TEMPLATE) as z:
        excl = json.loads(z.read("Metadata/project_settings.config")).get("exclude_object") == "1"
    if run([py, "-I", os.path.join(SCRIPTS, "make_3mf.py"), model, *(["--one-object"] if fine or not excl else []),
            *args], log).returncode:
        return {"ok": False, "error": "could not build the model file", "log": log}
    size = re.search(r"size mm: \[([^\]]+)\]", log[-1])
    dims = [float(x) for x in size.group(1).split(",")] if size else []
    if dims and (dims[0] > 250 or dims[1] > 250 or dims[2] > 250):
        return {"ok": False, "error": f"model is {dims} mm, bigger than the 256 mm bed", "log": log}
    # The template has 4 filaments (check_template); the slicer rejects a colour count that does not match it.
    if run([py, "-I", os.path.join(SCRIPTS, "build_from_gui.py"), TEMPLATE, model, out, layer, *colours[:4]],
           log).returncode:
        return {"ok": False, "error": "could not build slicer settings", "log": log}
    gcode = os.path.join(out, "plate_1.gcode")
    inp = os.path.join(out, "slice-input.3mf")
    fine_info = None
    if fine:
        pts, fine_info = detail.profile([p for _, p, _ in parts], float(layer))
        if fine_info["fine_mm"]:
            # A standard slice of the same input first: Detail shows its gain and cost against it.
            plain = os.path.join(work, "plain")
            os.makedirs(plain, exist_ok=True)
            if not slice_with("flashstudio", inp, plain, log) and os.path.exists(os.path.join(plain, "plate_1.gcode")):
                g = read_gcode(os.path.join(plain, "plate_1.gcode"))
                fine_info.update(plain_time=stats(g)["time"], plain_step=detail.stair_step([p for _, p, _ in parts], g))
            detail.write_profile(inp, pts)
    fallback = None
    for eng in ([engine, "flashstudio"] if engine == "forkaslicer" else ["flashstudio"]):
        if os.path.exists(gcode):
            os.remove(gcode)
        code = slice_with(eng, inp, out, log)
        if not code and os.path.exists(gcode):
            break
        if eng != "flashstudio":
            fallback = f"{eng} failed (exit {code}), sliced with Flash Studio"
    else:
        return {"ok": False, "error": f"slicer failed (exit {code})", "log": log}
    if fine_info and fine_info["fine_mm"] and eng == "flashstudio":
        err = fit_tower(inp, out, gcode, log)
        if err:
            return {"ok": False, "error": err, "log": log}
    v = run([py, "-I", os.path.join(SCRIPTS, "verify_slice.py"), TEMPLATE, gcode], log, timeout=300)
    passed, reasons = gate(v.stdout, open(gcode, encoding="utf-8", errors="ignore").read())
    if fine_info and fine_info["fine_mm"]:
        g = read_gcode(gcode)
        fine_info.update(time=stats(g)["time"], step=detail.stair_step([p for _, p, _ in parts], g),
                         applied=variable_layers(g))
    return {"ok": True, "gcode": gcode, "verify": v.stdout, "passed": passed, "reasons": reasons,
            "dims": dims, "engine": eng, "fallback": fallback, "fine": fine_info, "log": log}


def tower_box(gcode):
    """(tower box, model box, max Y of any move) from the G-code; a box is (x0, y0, x1, y1), None when absent.
    Tower = moves inside the WIPE_TOWER and CP blocks (the ;TYPE: label lags behind them)."""
    t, m, ymax = [], [], -1e9
    feat = x = y = None
    inside = set()  # the CP blocks sit inside the WIPE_TOWER ones
    for l in gcode.splitlines():
        if l.startswith(";"):
            if l.startswith(";TYPE:"):
                feat = l[6:].strip()
            elif l.startswith(("; WIPE_TOWER_START", "; CP TOOLCHANGE START", "; CP EMPTY GRID START")):
                inside.add("WT" if "WIPE" in l else "CP")
            elif l.startswith(("; WIPE_TOWER_END", "; CP TOOLCHANGE END", "; CP EMPTY GRID END")):
                inside.discard("WT" if "WIPE" in l else "CP")
            continue
        if not l.startswith(("G0", "G1", "G2", "G3")):
            continue
        mx, my = re.search(r"X(-?[\d.]+)", l), re.search(r"Y(-?[\d.]+)", l)
        x = float(mx.group(1)) if mx else x
        y = float(my.group(1)) if my else y
        if my:
            ymax = max(ymax, y)
        if x is None or y is None or not (mx or my) or not re.search(r"E[\d.]", l) or feat == "Custom":
            continue
        (t if inside else m).append((x, y))
    box = lambda p: (min(a for a, _ in p), min(b for _, b in p), max(a for a, _ in p), max(b for _, b in p)) if p else None
    return box(t), box(m), ymax


def fit_tower(inp, out, gcode, log, bed=256.0):
    """Detail mode only. The slicer sizes the purge tower from the base layer height, so thinner layers make it deeper
    than it planned and it can run off the back of the bed. Move it forward by the overflow and slice once more.
    Returns an error text when it would then sit on the model, else None (the gate still checks the bounds)."""
    tower, model, ymax = tower_box(read_gcode(gcode))
    if not tower or ymax <= bed - 1:
        return None
    with zipfile.ZipFile(inp) as z:
        files = {n: z.read(n) for n in z.namelist()}
    cfg = json.loads(files["Metadata/project_settings.config"])
    cfg["wipe_tower_y"] = [f"{float(cfg['wipe_tower_y'][0]) - (ymax - (bed - 1)) - 1:.3f}"]
    files["Metadata/project_settings.config"] = json.dumps(cfg, indent=4).encode()
    with zipfile.ZipFile(inp, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)
    log.append(f"Detail: purge tower reached Y {ymax:.1f}, moved to wipe_tower_y {cfg['wipe_tower_y'][0]}")
    os.remove(gcode)
    if slice_with("flashstudio", inp, out, log) or not os.path.exists(gcode):
        return "slicer failed after moving the purge tower for Detail"
    tower, model, ymax = tower_box(read_gcode(gcode))
    if tower and model and tower[0] < model[2] + 2 and model[0] < tower[2] + 2 \
            and tower[1] < model[3] + 2 and model[1] < tower[3] + 2:
        return "Detail makes the purge tower too deep to fit beside this model - slice without Detail"
    return None


def machine_cmds(text):
    """Non-move commands before the first layer and the last few of the file (velocity limits vary per model)."""
    # OrcaSlicer writes its settings block after the G-code, so the tail is taken from the executable block only.
    lines = text.split("; EXECUTABLE_BLOCK_END")[0].splitlines()
    first = next((i for i, l in enumerate(lines) if l.startswith(";LAYER_CHANGE")), len(lines))
    # EXCLUDE_OBJECT_* lines name each model's objects (exclude_object 1); gate() checks them on their own.
    keep = lambda ls: [c for c in (l.split(";")[0].strip() for l in ls)
                       if c and not re.match(r"G[0-3](\s|$)", c) and not c.startswith(("SET_VELOCITY_LIMIT", "EXCLUDE_OBJECT"))]
    # A tool change near the end (its block, then one SET_PRESSURE_ADVANCE when PA is on) is not the end routine;
    # gate() checks tool change blocks on their own (toolchange_cmds).
    tc = max((i for i, l in enumerate(lines) if l.startswith("; CP TOOLCHANGE END")), default=-1)
    skip = next((i for i in range(tc + 1, len(lines)) if keep([lines[i]])), None) if tc >= 0 else None
    if skip is not None and not lines[skip].startswith("SET_PRESSURE_ADVANCE"):
        skip = None
    start = max(tc + 1, len(lines) - 400)
    tail = keep([l for i, l in enumerate(lines[start:], start) if i != skip])
    return set(keep(lines[:first])), tail[-4:]


def toolchange_cmds(text):
    """Non-move commands inside every tool change block. Values are masked only on temps (checked elsewhere),
    acceleration (varies per model) and the tool number; anything else must match Flash Studio exactly."""
    cmds, inside = set(), False
    for l in text.split("; EXECUTABLE_BLOCK_END")[0].splitlines():
        if l.startswith(("; CP TOOLCHANGE START", "; CP TOOLCHANGE END")):  # WIPE sits inside the block
            inside = l.startswith("; CP TOOLCHANGE START")
            continue
        c = l.split(";")[0].strip()
        if inside and c and not re.match(r"G[0-3](\s|$)", c) and not c.startswith("EXCLUDE_OBJECT"):
            head, *args = c.split()
            if head in ("M104", "M109", "SET_VELOCITY_LIMIT"):
                args = [re.sub(r"^([A-Z]+=?)-?[\d.]+$", r"\1#", a) for a in args]
            cmds.add(" ".join([re.sub(r"^T\d$", "T#", head)] + args))
    return cmds


def exclude_object_problems(gcode):
    """exclude_object 1: every object is defined before the first layer, and its START / END lines alternate.
    Off (no EXCLUDE_OBJECT lines at all) is fine too."""
    body = gcode.split("; EXECUTABLE_BLOCK_END")[0]
    cmds = re.findall(r"^(EXCLUDE_OBJECT_\w+)(?:\s+NAME=(\S+))?", body, re.M)
    if not cmds:
        return []
    first = body.find(";LAYER_CHANGE")
    defined = set(re.findall(r"^EXCLUDE_OBJECT_DEFINE NAME=(\S+)", body[:first if first >= 0 else len(body)], re.M))
    problems, open_obj = [], None
    for cmd, name in cmds:
        if cmd == "EXCLUDE_OBJECT_DEFINE":
            continue
        if cmd not in ("EXCLUDE_OBJECT_START", "EXCLUDE_OBJECT_END") or name not in defined:
            problems.append(f"{cmd} {name or ''}".strip())
        elif (cmd == "EXCLUDE_OBJECT_START") != (open_obj is None) or (cmd == "EXCLUDE_OBJECT_END" and name != open_obj):
            problems.append(f"{cmd} {name} out of order")
        else:
            open_obj = name if cmd == "EXCLUDE_OBJECT_START" else None
    if open_obj:
        problems.append(f"EXCLUDE_OBJECT_START {open_obj} never ended")
    if not defined:
        problems.append("EXCLUDE_OBJECT lines with no object defined before the first layer")
    return [f"exclude_object lines wrong: {sorted(set(problems))[:4]}"] if problems else []


def gate(text, gcode):
    """Pass/fail on verify_slice.py output plus the machine commands, (any difference from your own Flash Studio export fails).
    Any reason = do not send. Coordinates in the last moves differ for every model, so moves are not compared."""
    reasons = []
    m = re.search(r"(\d+) unexpected differences", text)
    # The slicer re-places the purge tower to fit each plate; the bounds check below keeps it on the bed.
    tower = len(re.findall(r"\[CHECK\] wipe_tower_[xy]:", text))
    fs_only = sum(FS_ONLY.get(k) == v for k, v in re.findall(r"\[CHECK\] (\w+): gui=(.*?) \| new=None$", text, re.M))
    if not m or int(m.group(1)) - tower - fs_only:
        reasons.append(f"{int(m.group(1)) - tower - fs_only if m else '?'} unexpected settings differences")
    ref = machine_cmds(read_gcode(TEMPLATE))
    new = machine_cmds(gcode)
    # The slicer heats and selects whichever tool prints first (T1 when a tool-2 inlay sits on the bed).
    # Accept that only as the template's T0 swapped for a tool the file really uses; anything else still fails.
    used = set(re.findall(r"^T([0-3])\s*$", gcode, re.M))
    first = {re.sub(r"T\d", "T0", c) for c in new[0]}
    picked = {t[-1] for t in new[0] if re.fullmatch(r"T\d", t)}
    if ref[0] != new[0] and not (first == ref[0] and len(picked) == 1 and picked <= used):
        reasons.append(f"start commands differ from Flash Studio's: {sorted(ref[0] ^ new[0])[:6]}")
    if ref[1] != new[1]:
        reasons.append(f"end commands differ from Flash Studio's: {new[1]}")
    extra = toolchange_cmds(gcode) - toolchange_cmds(read_gcode(TEMPLATE))
    if extra:
        reasons.append(f"tool change commands not in Flash Studio's: {sorted(extra)[:6]}")
    reasons += exclude_object_problems(gcode)
    # Flash Studio waits for the new tool's temperature (M109) in every tool change; without it a tool can
    # print cold. Missing commands are not caught by the extra-commands check above.
    blocks = re.findall(r"^; CP TOOLCHANGE START(.*?)^; CP TOOLCHANGE END", gcode, re.M | re.S)
    nowait = sum(not re.search(r"^M109 ", b, re.M) for b in blocks)
    if nowait:
        reasons.append(f"{nowait} of {len(blocks)} tool changes have no temperature wait (M109)")
    nozzle = set()
    for c in ("M104", "M109"):
        m = re.search(rf"== {c}: gui=\[([^\]]*)\]", text)
        nozzle |= {x.strip(" '") for x in m.group(1).split(",") if x.strip()} if m else set()
    for c in ("M104", "M109", "M140", "M190"):
        m = re.search(rf"== {c}: gui=\[([^\]]*)\] new=\[([^\]]*)\]", text)
        if not m:
            continue
        gui = {s.strip(" '") for s in m.group(1).split(",") if s.strip()}
        new = {s.strip(" '") for s in m.group(2).split(",") if s.strip()}
        gui = nozzle if c in ("M104", "M109") else gui
        allowed = gui | {str(int(g) - 100) for g in gui if g.isdigit() and int(g) > 150}  # idle tools (ooze_prevention)
        if not new <= allowed:
            reasons.append(f"{c} temperatures {sorted(new)} not in Flash Studio's {sorted(gui)}")
    m = re.search(r"BOUNDS new: X (-?[\d.]+)\.\.(-?[\d.]+) Y (-?[\d.]+)\.\.(-?[\d.]+)", text)
    if not m or float(m.group(1)) < -1 or float(m.group(3)) < -1 or float(m.group(2)) > 257 or float(m.group(4)) > 257:
        reasons.append("moves outside the bed")
    m = re.search(r"EXTRUSION new: median E/mm ([\d.]+), formula for [\d.]+ x [\d.]+ mm bead ([\d.]+), "
                  r"filament_diameter ([\d.,]+)", text)
    bands = extrusion_bands(gcode) if variable_layers(gcode) else None
    if bands is not None:
        # Detail mode: the one-height median above means nothing; check every height band line by line instead.
        bad = [f"{h:.2f} mm x{r:.2f}" for h, (n, r) in sorted(bands.items()) if n >= 50 and abs(r - 1) > 0.10]
        if not m or not bands or bad or set(m.group(3).split(",")) != {"1.75"}:
            reasons.append(f"extrusion amount does not match the bead formula {bad or ''}".strip())
    # Flash Studio's own slices sit 5-6% over the formula (wider inner lines); a wrong filament diameter is ~2.7x.
    elif not m or abs(float(m.group(1)) / float(m.group(2)) - 1) > 0.10 or set(m.group(3).split(",")) != {"1.75"}:
        reasons.append("extrusion amount does not match the bead formula")
    return not reasons, reasons


def variable_layers(gcode):
    """True when the layers above the first are not all one height (Detail mode or a painted layer profile)."""
    hs = re.findall(r"^;LAYER_CHANGE\n;Z:[\d.]+\n;HEIGHT:([\d.]+)", gcode, re.M)
    return len({round(float(h), 3) for h in hs[1:]}) > 1


def extrusion_bands(gcode, dia=1.75):
    """{height band mm: (moves, median of E / formula E / that tool's filament_flow_ratio)} using each move's own
    ;WIDTH and ;HEIGHT, so a right slice sits at 1.00 whatever its layer heights. First layer (first layer flow),
    bridges and moves under 2 mm left out."""
    area = math.pi * (dia / 2) ** 2
    m = re.search(r"^; filament_flow_ratio = ([\d.,]+)", gcode, re.M)
    flow = [float(v) for v in m.group(1).split(",")] if m else []
    h = w = x = y = tool = None
    layer, bridge = 0, False
    vals = {}
    for l in gcode.splitlines():
        if l.startswith(";"):
            if l.startswith(";HEIGHT:"):
                h = float(l[8:])
            elif l.startswith(";WIDTH:"):
                w = float(l[7:])
            elif l.startswith(";LAYER_CHANGE"):
                layer += 1
            elif l.startswith(";TYPE:"):
                bridge = "ridge" in l
            continue
        t = re.match(r"T(\d)\s*$", l)
        if t:
            tool = int(t.group(1))
            continue
        if not l.startswith("G1"):
            continue
        mx, my, me = re.search(r"X(-?[\d.]+)", l), re.search(r"Y(-?[\d.]+)", l), re.search(r"E(-?[\d.]+)", l)
        nx = float(mx.group(1)) if mx else x
        ny = float(my.group(1)) if my else y
        if me and h and w and layer > 1 and not bridge and tool is not None and tool < len(flow)                 and None not in (x, y, nx, ny):
            d, e = math.hypot(nx - x, ny - y), float(me.group(1))
            if d > 2 and e > 0:
                vals.setdefault(round(round(h / 0.02) * 0.02, 2), []).append(
                    e / d / (((w - h) * h + math.pi * h * h / 4) / area) / flow[tool])
        x, y = nx, ny
    return {k: (len(v), sorted(v)[len(v) // 2]) for k, v in vals.items()}


def read_gcode(path):
    """Plain G-code, or a sliced .3mf (Flash Studio 'Export plate sliced file')."""
    if path.lower().endswith(".3mf"):
        z = zipfile.ZipFile(path)
        name = next((n for n in z.namelist() if re.match(r"Metadata/plate_\d+\.gcode$", n)), None)
        return z.read(name).decode(errors="ignore") if name else None
    return open(path, encoding="utf-8", errors="ignore").read()


def stats(text):
    # Flash Studio writes the totals near the top; OrcaSlicer (ForkaSlicer) writes them after the G-code.
    head, tail = text[:20000], text.split("; CONFIG_BLOCK_START")[0][-20000:]
    g = lambda pat: (re.search(pat, head, re.M) or re.search(pat, tail, re.M) or [None, None])[1]
    per = g(r"^; filament used \[g\] = (.+)$")
    grams = [float(x) for x in per.split(",")] if per else []
    colours = re.findall(r'^; <filament id="(\d)"[^>]*color="(#[0-9A-Fa-f]{6})"', head, re.M)
    if not colours:  # no per-filament lines: used tools from the grams, colours from the settings
        cols = [c.strip() for c in (header(text, "filament_colour") or "").split(";")]
        colours = [(str(i + 1), cols[i]) for i, w in enumerate(grams) if w > 0 and i < len(cols)]
    return {"time": g(r"^; estimated printing time \(normal mode\) = (.+)$"),
            "grams": g(r"^; total filament used \[g\] = (.+)$"),
            "grams_per_tool": grams, "layers": g(r"^; total layers count = (\d+)"),
            "changes": g(r"^; total filament change = (\d+)"),
            "colours": colours, "slicer": g(r"^; generated by (.+?) on"), "materials": materials(text)}


def header(text, key):
    """A '; key = a,b,c' settings line (Flash Studio writes them near the top, OrcaSlicer at the end)."""
    m = re.search(rf"^; {key} = (.+)$", text[:300000], re.M) or re.search(rf"^; {key} = (.+)$", text[-300000:], re.M)
    return m.group(1).strip() if m else None


def config(text):
    """Every '; key = value' line of the settings block the slicer wrote into the file (Flash Studio near the top,
    OrcaSlicer at the end): what the printer was really given, not the preset as it is now."""
    m = re.search(r"^; CONFIG_BLOCK_START\s*$(.*?)^; CONFIG_BLOCK_END", text, re.M | re.S)
    return dict(re.findall(r"^; ([A-Za-z0-9_]+) = (.*)$", m.group(1), re.M)) if m else {}


PART_KEYS_SKIP = {"name", "extruder", "matrix", "source_file", "source_object_id", "source_volume_id",
                  "source_offset_x", "source_offset_y", "source_offset_z", "source_in_inches", "source_in_meters",
                  "mesh_stat", "label_object_enabled"}


def part_settings(path):
    """Per-object setting changes in a 3MF (model_settings.config): [(part name, [(key, value)])]. The G-code
    settings block only holds the plate-wide values, so these are the ones it does not show."""
    out = []
    try:
        z = zipfile.ZipFile(path)
        xml = z.read("Metadata/model_settings.config").decode(errors="ignore")
    except (OSError, KeyError, zipfile.BadZipFile):
        return out
    for obj in re.findall(r"<object\b.*?</object>", xml, re.S):
        meta = re.findall(r'<metadata key="([^"]+)" value="([^"]*)"', re.sub(r"<part\b.*?</part>", "", obj, flags=re.S))
        name = dict(meta).get("name", "part")
        sets = [(k, v) for k, v in meta if k not in PART_KEYS_SKIP]
        for part in re.findall(r"<part\b.*?</part>", obj, re.S):
            pm = re.findall(r'<metadata key="([^"]+)" value="([^"]*)"', part)
            sets += [(f"{dict(pm).get('name', 'part')}: {k}", v) for k, v in pm if k not in PART_KEYS_SKIP]
        if sets:
            out.append((name, sets))
    return out


def materials(text):
    """Material each filament (tool) was sliced for, e.g. ['PLA', 'PETG', 'PLA', 'PLA']."""
    return [x.strip() for x in re.split(r"[;,]", header(text, "filament_type") or "") if x.strip()]


def family(name):
    """'PLA+', 'PLA PRO', 'Generic PLA High Speed' -> 'PLA'; 'PETG HF' -> 'PETG'."""
    for f in ("PETG", "PLA", "TPU", "ABS", "ASA", "PA", "PC"):
        if re.search(rf"(^|[^A-Z]){f}([^A-Z]|$)", (name or "").upper()):
            return f
    return (name or "").upper().strip()


def preset_check(text, house):
    """Every tool that prints must be on one of your own dialed-in presets (config house_presets; empty = no check).
    A stock Generic preset left in a used slot prints at its own temperatures, not yours. Returns reasons."""
    if not house:
        return []
    names = [x.strip().strip('"') for x in (header(text, "filament_settings_id") or "").split(";")]
    per = header(text, r"filament used \[g\]")
    grams = [float(g) for g in per.split(",")] if per else []
    used = [i for i, g in enumerate(grams) if g > 0]
    if not names or not names[0] or not used:
        return ["filament presets not stated in the file"]
    return [f"T{i + 1} on preset '{names[i] if i < len(names) else '?'}', not one of ours ({', '.join(house)})"
            for i in used if i >= len(names) or names[i] not in house]


def sanity(text, ranges):
    """Machine checks for a sliced file from anywhere (the full gate only runs on files this hub slices):
    1.75 mm filament, extrusion on the bed, and every used tool's nozzle and the bed inside the label range of the
    material it was sliced for. ranges: {"PLA": {"nozzle": [lo, hi], "bed": [lo, hi]}, ...}. Returns reasons."""
    reasons = []
    dia = header(text, "filament_diameter")
    if not dia or {x.strip() for x in dia.split(",") if x.strip()} != {"1.75"}:
        reasons.append(f"filament diameter {dia or 'not stated'}, not 1.75 mm")
    mats = materials(text)
    hot, bed, t = {}, [], 0
    x = y = None
    x0 = y0 = 1e9
    x1 = y1 = -1e9
    for line in text.splitlines():
        if line[:1] == "T" and line[1:2].isdigit():
            t = int(line[1])
            continue
        c = line.split(";")[0]
        if c.startswith(("M104", "M109")):
            m, mt = re.search(r"S(\d+)", c), re.search(r"T(\d)", c)
            if m:
                k = int(mt.group(1)) if mt else t
                hot[k] = max(hot.get(k, 0), int(m.group(1)))
        elif c.startswith(("M140", "M190")):
            m = re.search(r"S(\d+)", c)
            if m:
                bed.append(int(m.group(1)))
        elif c.startswith("G1 "):
            mx, my, me = re.search(r"X(-?[\d.]+)", c), re.search(r"Y(-?[\d.]+)", c), re.search(r"E(-?[\d.]+)", c)
            nx = float(mx.group(1)) if mx else x
            ny = float(my.group(1)) if my else y
            if me and float(me.group(1)) > 0 and None not in (nx, ny):
                x0, x1, y0, y1 = min(x0, nx), max(x1, nx), min(y0, ny), max(y1, ny)
            x, y = nx, ny
    if x1 > -1e9 and (x0 < -1 or y0 < -1 or x1 > 257 or y1 > 257):
        reasons.append(f"extrudes outside the bed (X {x0:.0f}..{x1:.0f}, Y {y0:.0f}..{y1:.0f})")
    per = header(text, r"filament used \[g\]")
    grams = [float(g) for g in per.split(",")] if per else []
    used = [i for i, g in enumerate(grams) if g > 0] or sorted(hot)
    bed_lo, bed_hi = 0, 999
    for i in used:
        mat = mats[i] if i < len(mats) else "?"
        r = ranges.get(family(mat))
        if not r:
            reasons.append(f"T{i + 1} sliced for {mat}: no temperature range on file for it")
            continue
        lo, hi = r["nozzle"]
        if not lo <= hot.get(i, 0) <= hi:
            reasons.append(f"T{i + 1} {mat} nozzle up to {hot.get(i, 0)}C, label range {lo}-{hi}C")
        bed_lo, bed_hi = max(bed_lo, r["bed"][0]), min(bed_hi, r["bed"][1])
    if bed and used and not bed_lo <= max(bed) <= bed_hi:
        reasons.append(f"bed up to {max(bed)}C, range for these materials {bed_lo}-{bed_hi}C")
    return reasons


def preview_png(text, colours, out, size=480):
    """Top view of every extrusion move, later layers on top, coloured by tool."""
    segs, x = [], None
    t, y, xs, ys = 0, None, [], []
    for line in text.splitlines():
        if line[:1] == "T" and line[1:2].isdigit():
            t = int(line[1])
            continue
        if not line.startswith("G1 "):
            continue
        c = line.split(";")[0]
        mx, my, me = re.search(r"X(-?[\d.]+)", c), re.search(r"Y(-?[\d.]+)", c), re.search(r"E(-?[\d.]+)", c)
        nx = float(mx.group(1)) if mx else x
        ny = float(my.group(1)) if my else y
        if me and float(me.group(1)) > 0 and None not in (x, y, nx, ny) and ny > 1:
            segs.append((x, y, nx, ny, t))
            xs += (x, nx); ys += (y, ny)
        x, y = nx, ny
    if not segs:
        return False
    x0, x1, y0, y1 = min(xs) - 5, max(xs) + 5, min(ys) - 5, max(ys) + 5
    k = (size - 1) / max(x1 - x0, y1 - y0)
    w, h = int((x1 - x0) * k) + 1, int((y1 - y0) * k) + 1
    px = bytearray(b"\x1c\x1e\x22" * (w * h))
    rgb = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in colours] or [(255, 255, 255)]
    for ax, ay, bx, by, tool in segs:
        col = bytes(rgb[tool % len(rgb)])
        steps = int(max(abs(bx - ax), abs(by - ay)) * k) + 1
        for s in range(steps + 1):
            f = s / steps
            i = int((ax + (bx - ax) * f - x0) * k)
            j = h - 1 - int((ay + (by - ay) * f - y0) * k)
            p = (j * w + i) * 3
            px[p:p + 3] = col
    raw = b"".join(b"\x00" + bytes(px[r * w * 3:(r + 1) * w * 3]) for r in range(h))
    chunk = lambda tag, d: struct.pack(">I", len(d)) + tag + d + struct.pack(">I", zlib.crc32(tag + d))
    open(out, "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                          + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    return True


def duration(s):  # "6h 56m 15s" -> seconds
    return sum(int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u] for n, u in re.findall(r"(\d+)\s*([dhms])", s))


ARG = re.compile(r"([XYZEFIJRPST])(-?[\d.]+)")


def timeline(text):
    """Layer start times, temperature and tool events, and the total, all in print-time seconds."""
    x = y = z = 0.0
    feed, t, tool = 3000.0, 0.0, 0
    layers, events = [], []
    marker = ";LAYER_CHANGE" if ";LAYER_CHANGE" in text else ";Z:"
    for line in text.splitlines():
        if line.startswith(";"):
            if line.startswith(marker):
                layers.append(t)
            continue
        code = line.split(";", 1)[0].strip()
        if not code:
            continue
        cmd, _, rest = code.partition(" ")
        p = {k: float(v) for k, v in ARG.findall(rest)}
        if cmd in ("G0", "G1", "G2", "G3"):
            feed = p.get("F", feed)
            nx, ny, nz = p.get("X", x), p.get("Y", y), p.get("Z", z)
            if cmd in ("G2", "G3") and ("I" in p or "J" in p):
                cx, cy = x + p.get("I", 0), y + p.get("J", 0)
                a = math.atan2(y - cy, x - cx)
                b = math.atan2(ny - cy, nx - cx)
                sweep = (a - b) if cmd == "G2" else (b - a)
                sweep = sweep % (2 * math.pi) or 2 * math.pi
                d = math.hypot(sweep * math.hypot(cx - x, cy - y), nz - z)
            else:
                d = math.dist((x, y, z), (nx, ny, nz)) or abs(p.get("E", 0))
            if feed > 0:
                t += d / (feed / 60)
            x, y, z = nx, ny, nz
        elif cmd == "G4":
            t += p.get("S", 0) + p.get("P", 0) / 1000
        elif re.fullmatch(r"T[0-3]", cmd):
            tool = int(cmd[1])
            events.append((t, "tool", tool))
        elif cmd in ("M104", "M109") and "S" in p:
            events.append((t, "nozzle", (int(p.get("T", tool)), p["S"])))
        elif cmd in ("M140", "M190") and "S" in p:
            events.append((t, "bed", p["S"]))
        elif cmd == "M220" and "S" in p:  # speed back to 100% at each tool change
            events.append((t, "speed", p["S"]))
    m = re.search(r"estimated printing time \(normal mode\) = ([^\n]+)", text)
    total = duration(m.group(1)) if m else t
    k = total / t if t > 0 else 1.0  # moves alone miss accel and tool changes; the slicer's own total is better
    thumb = None
    tm = re.search(r"; thumbnail begin \d+x\d+ \d+\n(.*?)\n; thumbnail end", text, re.S)
    if tm:
        import base64
        thumb = base64.b64decode("".join(l.lstrip("; ") for l in tm.group(1).splitlines()))
    return {"total": float(total), "layers": [s * k for s in layers] or [0.0],
            "events": [(s * k, kind, v) for s, kind, v in events], "thumb": thumb}


def eta_model(text):
    """Every move of the file, for the time-left model. The printer's printProgress is the share of the FILE READ
    (bytes), not of filament: on the two-tool lid 62f9 bytes kept real/model time at 0.98-1.18 the whole way while
    filament share swung 0.3-7x (2026-10-29). So each move keeps its byte position, layer and height.
    Returns arrays for eta_times plus "b" (byte share at the end of each move), "layer" (file layer, 1 = first),
    "zs" (height of each layer) and "slicer" (the slicer's own total, seconds)."""
    from array import array
    d, f, a, jc, extra, b, tcs = (array("d") for _ in range(7))
    lay = array("i")
    zs = []
    x = y = z = 0.0
    feed, acc, stop, pu, pend, pos, layer, swaps = 50.0, 10000.0, True, None, 0.0, 0, 0, 0
    for line in text.split("\n"):
        pos += len(line) + 1
        if not line:
            continue
        if line[0] == ";":
            if line.startswith(";LAYER_CHANGE"):
                layer += 1
            elif line.startswith(";Z:") and layer and len(zs) < layer:
                zs.append(float(line[3:]))
            continue
        code = line.split(";", 1)[0].strip()
        cmd, _, rest = code.partition(" ")
        if cmd == "SET_VELOCITY_LIMIT":
            m = re.search(r"ACCEL=([\d.]+)", rest)
            acc = float(m.group(1)) if m else acc
            continue
        if cmd in ("G0", "G1", "G2", "G3"):
            p = {k: float(v) for k, v in ARG.findall(rest)}
            feed = p.get("F", feed * 60) / 60
            e = p.get("E", 0.0)
            nx, ny, nz = p.get("X", x), p.get("Y", y), p.get("Z", z)
            dx, dy, dz = nx - x, ny - y, nz - z
            L = math.sqrt(dx * dx + dy * dy + dz * dz)
            x, y, z = nx, ny, nz
            if L < 1e-9:
                if e:
                    pend += abs(e) / max(1e-3, min(feed, 30.0))
                    stop = True
                continue
            u = (dx / L, dy / L, dz / L)
            v, ac = feed, acc
            if dx == 0 and dy == 0:
                v, ac = min(v, 20.0), min(ac, 300.0)
            jc.append(2.0 if (stop or pu is None) else -(pu[0] * u[0] + pu[1] * u[1] + pu[2] * u[2]))
            d.append(L); f.append(min(v, 600.0)); a.append(ac); extra.append(pend); b.append(pos)
            lay.append(layer); tcs.append(swaps)
            stop, pu, pend = False, u, 0.0
        elif cmd == "G4":
            p = {k: float(v) for k, v in ARG.findall(rest)}
            pend += p.get("S", 0) + p.get("P", 0) / 1000
            stop = True
        elif re.fullmatch(r"T[0-3]", cmd):
            swaps += layer > 0  # the start-up tool select is not a swap
            stop = True
    if not d:
        return None
    for i in range(len(b)):
        b[i] /= pos
    mt = re.search(r"estimated printing time \(normal mode\) = ([^\n]+)", text)
    return {"d": d, "f": f, "a": a, "jc": jc, "extra": extra, "tc": tcs, "b": b, "layer": lay, "zs": zs,
            "slicer": float(duration(mt.group(1))) if mt else None}


def eta_times(m, speed=1.0, scv=5.0, mcr=0.0, amax=1e9, swap_s=0.0):
    """Seconds done at the end of each move, from a Klipper-style planner: trapezoids, junction deviation from
    square corner velocity SCV, full stop at retracts and tool changes, accelerations capped at AMAX, cruise speed
    capped by Klipper's minimum_cruise_ratio MCR, SWAP_S per tool change. SPEED is the printer's speed % / 100: it
    scales feeds but not accelerations or corner speeds, so 140% is nowhere near 1.4x on small parts."""
    from array import array
    d, f, jc, extra, tc = m["d"], m["f"], m["jc"], m["extra"], m["tc"]
    n, k = len(d), scv * scv * (math.sqrt(2) - 1)
    a = [min(x, amax) for x in m["a"]]
    v2 = [min(fi * speed, 600.0) ** 2 for fi in f]
    jv2 = [0.0] * n
    for i in range(1, n):
        c = jc[i]
        if c < 0.999999:
            sn = math.sqrt(0.5 * (1.0 - max(c, -0.999999)))
            jv2[i] = min(sn / (1.0 - sn) * k, v2[i], v2[i - 1])
    sm, nxt = [0.0] * n, 0.0
    for i in range(n - 1, -1, -1):
        cap = min(jv2[i + 1] if i + 1 < n else 0.0, nxt)
        nxt = sm[i] = min(jv2[i], v2[i], cap + 2 * a[i] * d[i])
    cum, t, prev, last = array("d", [0.0] * n), 0.0, 0.0, 0
    for i in range(n):
        s2 = min(sm[i], prev, jv2[i])
        e2 = min(sm[i + 1] if i + 1 < n else 0.0, s2 + 2 * a[i] * d[i], v2[i])
        ac, L = a[i], d[i]
        vc2 = max(min(v2[i], (s2 + e2) / 2 + ac * (1 - mcr) * L), s2, e2) if mcr else v2[i]
        da, dd = (vc2 - s2) / (2 * ac), (vc2 - e2) / (2 * ac)
        v0, v1 = math.sqrt(s2), math.sqrt(e2)
        if da + dd <= L:
            vc = math.sqrt(vc2)
            t += (2 * vc - v0 - v1) / ac + (L - da - dd) / vc
        else:
            vp = math.sqrt(max((2 * ac * L + s2 + e2) / 2, s2, e2))
            t += (2 * vp - v0 - v1) / ac
        t += extra[i] + swap_s * (tc[i] - last)
        last = tc[i]
        cum[i] = t
        prev = e2
    return cum


def eta_profile(text, scv=5.0, points=4000):
    """Where the printer is and how long the rest takes. The printer's printProgress is the share of filament
    extruded so far (checked against a 6562 s print: elapsed tracks it all the way, while its layer number ended at
    132 of 165). This maps that share to real-ish seconds done, from a Klipper-style planner (trapezoids, junction
    deviation, full stop at retracts, a nominal tool swap), scaled to the slicer's own total.
    Returns {"e": [filament share], "t": [seconds done], "total": seconds} at about POINTS steps."""
    from array import array
    d, f, a, jc, extra, cume = array("d"), array("d"), array("d"), array("d"), array("d"), array("d")
    x = y = z = 0.0
    feed, acc, stop, pu, pend, etot = 50.0, 10000.0, True, None, 0.0, 0.0
    for line in text.splitlines():
        if not line or line[0] == ";":
            continue
        code = line.split(";", 1)[0].strip()
        cmd, _, rest = code.partition(" ")
        if cmd == "SET_VELOCITY_LIMIT":
            m = re.search(r"ACCEL=([\d.]+)", rest)
            acc = float(m.group(1)) if m else acc
            continue
        if cmd in ("G0", "G1", "G2", "G3"):
            p = {k: float(v) for k, v in ARG.findall(rest)}
            feed = p.get("F", feed * 60) / 60
            e = p.get("E", 0.0)
            if e > 0:
                etot += e
            nx, ny, nz = p.get("X", x), p.get("Y", y), p.get("Z", z)
            dx, dy, dz = nx - x, ny - y, nz - z
            L = math.sqrt(dx * dx + dy * dy + dz * dz)
            x, y, z = nx, ny, nz
            if L < 1e-9:
                if e:
                    pend += abs(e) / max(1e-3, min(feed, 30.0))
                    stop = True
                continue
            u = (dx / L, dy / L, dz / L)
            v, ac = feed, acc
            if dx == 0 and dy == 0:
                v, ac = min(v, 20.0), min(ac, 300.0)
            jc.append(2.0 if (stop or pu is None) else -(pu[0] * u[0] + pu[1] * u[1] + pu[2] * u[2]))
            d.append(L); f.append(min(v, 600.0)); a.append(ac); extra.append(pend); cume.append(etot)
            stop, pu, pend = False, u, 0.0
        elif cmd == "G4":
            p = {k: float(v) for k, v in ARG.findall(rest)}
            pend += p.get("S", 0) + p.get("P", 0) / 1000
            stop = True
        elif re.fullmatch(r"T[0-3]", cmd):
            pend += 15.0
            stop = True
    n, k = len(d), scv * scv * (math.sqrt(2) - 1)
    if not n or etot <= 0:
        return None
    v2 = [fi * fi for fi in f]
    jv2 = [0.0] * n
    for i in range(1, n):
        c = jc[i]
        if c < 0.999999:
            sn = math.sqrt(0.5 * (1.0 - max(c, -0.999999)))
            jv2[i] = min(sn / (1.0 - sn) * k, v2[i], v2[i - 1])
    sm, nxt = [0.0] * n, 0.0
    for i in range(n - 1, -1, -1):
        cap = min(jv2[i + 1] if i + 1 < n else 0.0, nxt)
        nxt = sm[i] = min(jv2[i], v2[i], cap + 2 * a[i] * d[i])
    cum, t, prev = array("d", [0.0] * n), 0.0, 0.0
    for i in range(n):
        s2 = min(sm[i], prev, jv2[i])
        e2 = min(sm[i + 1] if i + 1 < n else 0.0, s2 + 2 * a[i] * d[i], v2[i])
        ac, L, vc2 = a[i], d[i], v2[i]
        da, dd = (vc2 - s2) / (2 * ac), (vc2 - e2) / (2 * ac)
        v0, v1 = math.sqrt(s2), math.sqrt(e2)
        if da + dd <= L:
            vc = math.sqrt(vc2)
            t += (2 * vc - v0 - v1) / ac + (L - da - dd) / vc
        else:
            vp = math.sqrt(max((2 * ac * L + s2 + e2) / 2, s2, e2))
            t += (2 * vp - v0 - v1) / ac
        t += extra[i]
        cum[i] = t
        prev = e2
    mt = re.search(r"estimated printing time \(normal mode\) = ([^\n]+)", text)
    total = duration(mt.group(1)) if mt else t
    scale = total / t if t else 1.0
    step = max(1, n // points)
    es, ts = [0.0], [0.0]
    for i in list(range(0, n, step)) + [n - 1]:
        if cume[i] / etot >= es[-1]:
            es.append(cume[i] / etot); ts.append(cum[i] * scale)
    return {"e": es, "t": ts, "total": float(total)}


def speed_ratios(text, speeds, scv=5.0):
    """How much faster the printer runs the file at each speed factor (printer speed % / 100), as plan seconds per
    real second. Feeds scale with the speed setting but accelerations and corner speeds do not, so 140% is not 1.4x:
    a Klipper-style planner (trapezoids, junction deviation, full stop at retracts and tool changes) is run once per
    factor and compared with 100%."""
    from array import array
    d, f, a, jc = array("d"), array("d"), array("d"), array("d")
    fixed = 0.0
    x = y = z = 0.0
    feed, acc, stop, pu = 50.0, 10000.0, True, None
    for line in text.splitlines():
        if not line or line[0] == ";":
            continue
        code = line.split(";", 1)[0].strip()
        cmd, _, rest = code.partition(" ")
        if cmd == "SET_VELOCITY_LIMIT":
            m = re.search(r"ACCEL=([\d.]+)", rest)
            acc = float(m.group(1)) if m else acc
            continue
        if cmd in ("G0", "G1", "G2", "G3"):
            p = {k: float(v) for k, v in ARG.findall(rest)}
            feed = p.get("F", feed * 60) / 60
            nx, ny, nz = p.get("X", x), p.get("Y", y), p.get("Z", z)
            dx, dy, dz = nx - x, ny - y, nz - z
            L = math.sqrt(dx * dx + dy * dy + dz * dz)
            x, y, z = nx, ny, nz
            if L < 1e-9:
                if p.get("E"):
                    fixed += abs(p["E"]) / max(1e-3, min(feed, 30.0))
                    stop = True
                continue
            u = (dx / L, dy / L, dz / L)
            v, ac = feed, acc
            if dx == 0 and dy == 0:
                v, ac = min(v, 20.0), min(ac, 300.0)
            jc.append(2.0 if (stop or pu is None) else -(pu[0] * u[0] + pu[1] * u[1] + pu[2] * u[2]))
            d.append(L); f.append(v); a.append(ac)
            stop, pu = False, u
        elif cmd == "G4" or re.fullmatch(r"T[0-3]", cmd):
            stop = True
    n, k = len(d), scv * scv * (math.sqrt(2) - 1)

    def total(s):
        v2 = [min(fi * s, 600.0) ** 2 for fi in f]
        jv2 = [0.0] * n
        for i in range(1, n):
            c = jc[i]
            if c < 0.999999:
                sn = math.sqrt(0.5 * (1.0 - max(c, -0.999999)))
                jv2[i] = min(sn / (1.0 - sn) * k, v2[i], v2[i - 1])
        sm, nxt = [0.0] * n, 0.0
        for i in range(n - 1, -1, -1):
            cap = min(jv2[i + 1] if i + 1 < n else 0.0, nxt)
            nxt = sm[i] = min(jv2[i], v2[i], cap + 2 * a[i] * d[i])
        t, prev = 0.0, 0.0
        for i in range(n):
            s2 = min(sm[i], prev, jv2[i])
            e2 = min(sm[i + 1] if i + 1 < n else 0.0, s2 + 2 * a[i] * d[i], v2[i])
            ac, L, vc2 = a[i], d[i], v2[i]
            da, dd = (vc2 - s2) / (2 * ac), (vc2 - e2) / (2 * ac)
            v0, v1 = math.sqrt(s2), math.sqrt(e2)
            if da + dd <= L:
                vc = math.sqrt(vc2)
                t += (2 * vc - v0 - v1) / ac + (L - da - dd) / vc
            else:
                vp = math.sqrt(max((2 * ac * L + s2 + e2) / 2, s2, e2))
                t += (2 * vp - v0 - v1) / ac
            prev = e2
        return t + fixed

    base = total(1.0)
    return {s: (base / total(s) if base else 1.0) for s in speeds}


def scene(text):
    """What should be on the plate at each layer, for the watcher: top Z, the part's and the prime tower's printed
    footprints (x0, x1, y0, y1), and the tools that printed the part on each layer."""
    x = y = z = 0.0
    tool, feature, tower = 0, "", False
    zs, tools, part, prime = [], [], None, None
    grow = lambda b, x, y: [min(b[0], x), max(b[1], x), min(b[2], y), max(b[3], y)] if b else [x, x, y, y]
    for line in text.splitlines():
        if line.startswith(";"):
            if line.startswith(";LAYER_CHANGE"):
                zs.append(z)
                tools.append(set())
            elif line.startswith(";Z:") and zs:
                zs[-1] = float(line[3:])
            elif line.startswith(";TYPE:"):
                feature = line[6:]
            continue
        code = line.split(";", 1)[0].strip()
        if not code:
            continue
        cmd, _, rest = code.partition(" ")
        if re.fullmatch(r"T[0-3]", cmd):
            tool = int(cmd[1])
            continue
        if cmd not in ("G1", "G2", "G3"):
            continue
        p = {k: float(v) for k, v in ARG.findall(rest)}
        nx, ny, z = p.get("X", x), p.get("Y", y), p.get("Z", z)
        if p.get("E", 0) > 0 and zs and ("X" in p or "Y" in p) and feature != "Custom":
            if feature == "Prime tower":
                prime = grow(grow(prime, x, y), nx, ny)
            else:
                part = grow(grow(part, x, y), nx, ny)
                tools[-1].add(tool)
        x, y = nx, ny
    return {"z": zs, "tools": [sorted(t) for t in tools], "part": part, "tower": prime}


# Creator 5 material-station palette: slots report one of these 24 colours. Values from ff-5mp-api-py
# (github.com/GhostTypes/ff-5mp-api-py, flashforge/api/controls/creator5_palette.py, MIT). Nearest by CIE76 here.
C5_PALETTE = [("white", "#FFFFFF"), ("yellow", "#FFF245"), ("light green", "#DEF578"), ("green", "#21CC3D"),
              ("dark green", "#167A4B"), ("teal", "#156682"), ("cyan", "#24E4A0"), ("light blue", "#7BD9F0"),
              ("blue", "#4CAAF8"), ("dark blue", "#2E54DD"), ("purple", "#48358C"), ("violet", "#A341F7"),
              ("magenta", "#F435F6"), ("pink", "#D5B4DE"), ("coral", "#FA6173"), ("red", "#F82D29"),
              ("brown", "#805003"), ("orange", "#F9903B"), ("cream", "#FCEBD7"), ("tan", "#D5C5A1"),
              ("dark brown", "#B17C38"), ("gray", "#8C8C89"), ("light gray", "#BEBEBE"), ("black", "#1B1B1B")]


def _lab(hexc):
    rgb = [int(hexc.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    xyz = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b,         (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = [t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116 for t in xyz]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def colour_name(hexc):
    """'#F5D000' -> 'yellow': the palette colour the printer would show for it."""
    try:
        lab = _lab(hexc)
    except (ValueError, AttributeError):
        return None
    return min(C5_PALETTE, key=lambda p: sum((a - b) ** 2 for a, b in zip(lab, _lab(p[1]))))[0]


def z_shift(text, dz):
    """Raise the whole print by dz mm, like a Z offset on the printer: every absolute Z from the first layer change
    on. The start G-code (purge line) is left as it is. Afterwards every extrusion must sit at its layer's ;Z: + dz.
    Returns (text, reasons); any reason = do not use the result."""
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if l.startswith(";LAYER_CHANGE")), None)
    if start is None:
        return text, ["no layer markers in the file"]
    reasons, out, absolute, moved = [], list(lines), True, 0
    for i, l in enumerate(lines):
        c = l.split(";")[0].strip()
        if c.startswith("G90"):
            absolute = True
        elif c.startswith("G91"):
            absolute = False
        if i < start:
            continue
        if re.match(r"G(28|92)\b", c) and re.search(r"\bZ", c):
            reasons.append(f"line {i + 1}: {c} after the first layer")
        elif re.match(r"G[0-3] ", c) and re.search(r"\sZ-?[\d.]", c):
            if not absolute:
                reasons.append(f"line {i + 1}: relative Z move after the first layer")
                continue
            code, rest = l[:len(l.split(";")[0])], l[len(l.split(";")[0]):]
            out[i] = re.sub(r"(\s)Z(-?[\d.]+)", lambda m: f"{m.group(1)}Z{float(m.group(2)) + dz:.3f}", code) + rest
            moved += 1
    # A spiral vase climbs within each layer, so there every extrusion must sit at its own unshifted height + dz
    spiral = (header(text, "spiral_mode") or "0").strip() == "1"
    z = z0 = layer = None
    bad = n = 0
    for l, l0 in zip(out[start:], lines[start:]):
        if l.startswith(";Z:"):
            layer = float(l[3:])
            continue
        c = l.split(";")[0]
        if not re.match(r"G[0-3] ", c):
            continue
        mz, me = re.search(r"\sZ(-?[\d.]+)", c), re.search(r"\sE(-?[\d.]+)", c)
        m0 = re.search(r"\sZ(-?[\d.]+)", l0.split(";")[0])
        if mz:
            z = float(mz.group(1))
        if m0:
            z0 = float(m0.group(1))
        if me and float(me.group(1)) > 0 and re.search(r"\s[XYIJ]-?[\d.]", c):
            n += 1
            want = (z0 + dz if z0 is not None else None) if spiral else (layer + dz if layer is not None else None)
            if want is None or z is None or abs(z - want) > 1e-4:
                bad += 1
    if not moved or not n:
        reasons.append("no Z moves or extrusions found after the first layer change")
    if bad:
        reasons.append(f"{bad} of {n} extrusions not at their layer height + {dz} after the shift")
    return "\n".join(out), reasons


def write_shifted(src, dst, dz):
    """z_shift a .gcode or a sliced .3mf (the plate G-code and its .md5 replaced, every other entry copied).
    Returns reasons; empty = dst written."""
    text = read_gcode(src)
    if not text:
        return ["no G-code in the file"]
    new, reasons = z_shift(text, dz)
    if reasons:
        return reasons
    data = new.encode("utf-8")
    if not src.lower().endswith(".3mf"):
        open(dst, "wb").write(data)
        return []
    import hashlib
    zin = zipfile.ZipFile(src)
    gname = next(n for n in zin.namelist() if re.match(r"Metadata/plate_\d+\.gcode$", n))
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == gname:
                zout.writestr(item, data)
            elif item.filename == gname + ".md5":
                zout.writestr(item, hashlib.md5(data).hexdigest().upper())
            else:
                zout.writestr(item, zin.read(item.filename))
    return []
