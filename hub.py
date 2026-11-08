# C5 Print Hub: phone page for the Flashforge Creator 5 Pro (unofficial). Upload, slice, send, watch, pause/cancel.
# Run: python -I hub.py   (config.json next to this file; jobs/ holds every upload and slice)
# First run: open http://127.0.0.1:8765/setup on this PC.
import bisect, glob, gzip, hashlib, hmac, ipaddress, json, os, re, secrets, shutil, socket, sys, threading, time, \
    traceback, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dryer, printer, slicer, toolpath, watcher

CFG_PATH = os.path.join(HERE, "config.json")
JOBS = os.path.join(HERE, "jobs")
NAME = "C5 Print Hub"
# host 0.0.0.0: phones on your own Wi-Fi can open the page (LAN only). Never forward this port on your router.
# For a hub reachable only through `tailscale serve`, set host to 127.0.0.1 (setup.ps1 -Tailscale).
DEFAULTS = {"setup_done": False, "printer_ip": "", "serial": "", "check_code": "", "port": 8765, "host": "0.0.0.0",
            "colours": ["#FFFFFF", "#1E5BD8", "#F5D000", "#FF69B4"], "leveling": False,
            # Phone webcam (an IP camera app), e.g. "http://192.168.1.50:8080"; "" = printer camera only.
            "webcam_url": "", "webcam_fps": 8,
            # Failure watcher: off until set up. watch_backend "api" (your own Anthropic API key) or "cli" (Claude Code).
            "watch": False, "watch_backend": "", "anthropic_api_key": "", "watch_every_min": 2, "watch_action": "pause",
            "ntfy_topic": "",
            # Slicer: "flashstudio", or "forkaslicer" (an OrcaSlicer fork in WSL, falls back to Flash Studio if it fails).
            "engine": "flashstudio",
            # Nozzle and bed ranges for the upload check, from common spool labels. Change them to your own spools.
            "material_ranges": {"PLA": {"nozzle": [190, 230], "bed": [0, 70]},
                                "PETG": {"nozzle": [230, 270], "bed": [60, 80]},
                                "TPU": {"nozzle": [210, 240], "bed": [0, 60]}},
            # Z offset per material, applied by the hub at send (mm, + = nozzle higher), e.g. {"PETG": 0.05}.
            # Flash Studio has no Z offset per filament.
            "z_offset_by_material": {},
            # Your own dialed-in Flash Studio filament presets. When set, a tool that prints on any other preset blocks
            # the send (Send anyway still overrides). Empty = no preset check.
            "house_presets": []}
ACTIVE = ("printing", "pause", "pausing", "paused", "heating", "busy", "downloading")
PAUSED = ("pause", "pausing", "paused")
ERRORS = {"E0162": "filament run-out or empty slot"}  # seen with an empty slot on a run-out pause


CFG_LOCK = threading.Lock()


def load_cfg():
    cfg = dict(DEFAULTS)
    if os.path.exists(CFG_PATH):
        cfg.update(json.load(open(CFG_PATH, encoding="utf-8")))
    if not cfg.get("password"):
        cfg["password"] = f"{secrets.randbelow(10**6):06d}"
    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_hex(16)
    json.dump(cfg, open(CFG_PATH, "w", encoding="utf-8"), indent=1)
    return cfg


def save_cfg(**changes):
    with CFG_LOCK:
        CFG.update(changes)
        json.dump(CFG, open(CFG_PATH + ".tmp", "w", encoding="utf-8"), indent=1)
        os.replace(CFG_PATH + ".tmp", CFG_PATH)


def webcam_stream(url):
    """The MJPEG address of a phone camera app: the address as given, or <address>/video when it has no path
    (the IP Webcam app's stream)."""
    url = (url or "").strip()
    u = urlparse(url)
    return url.rstrip("/") + "/video" if url and u.path in ("", "/") and not u.query else url


CFG = load_cfg()
slicer.configure(CFG)
PR = printer.Printer(CFG["printer_ip"], CFG["serial"], CFG["check_code"])
CAM = printer.Camera(CFG["printer_ip"] or "0.0.0.0")
WEBCAM = (printer.Camera(url=webcam_stream(CFG["webcam_url"]), fps=CFG["webcam_fps"], wake=8)
          if CFG["webcam_url"] else None)
STATE = {"detail": None, "error": "starting", "time": 0}
LAST_CONTROL = {}  # action -> time, so a pause or cancel you sent yourself is not pushed back to you
STARTED = time.time()
SLICE_LOCK = threading.Lock()
JOB_LOCK = threading.Lock()
DESIGN_LOCK = threading.Lock()  # one design check at a time (each holds a few hundred MB)
os.makedirs(JOBS, exist_ok=True)


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(os.path.join(HERE, "hub.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- printer status ----------

class Eta:
    """Time left from a planner model of the whole file (slicer.eta_model / eta_times) and the pace really seen.
    The printer's printProgress is the share of the file read, so it pins the exact move, layer and height. Fitted
    2026-10-29 on 6 logged prints: at 100% the model alone matched real printing time within 2.5% (SCV 5, no
    accel cap, no cruise ratio). A speed change did not always give what the model says (lid 62f9 at 120% ran
    1.12x slower than modelled), so the measured pace at the current speed corrects it, weighed against
    PRIOR_S seconds of on-model printing. The printer's own clock and estimatedTime leave out the start
    (heating + leveling), so the hub counts that separately (START_S)."""
    def __init__(self, scv=5.0, k=1.0, window=21600, min_span=120, prior_s=1800, clamp=1.5):
        # window: seconds of printing the pace is measured over (6 h: on lid 62f9 a 30 min window kept trusting the
        # 120% model for hours, mean error 7.9 vs 5.7 min), min_span: least of it before it counts;
        # clamp: the correction stays within 1/clamp..clamp (0 = no limit)
        self.scv, self.k, self.window, self.min_span, self.prior_s, self.clamp = scv, k, window, min_span, prior_s, clamp
        self.lock = threading.Lock()
        self.reset(None)

    def reset(self, name):
        self.file, self.model, self.cum, self.active, self.last = name, None, {}, 0.0, None
        self.layer = None  # the printer's own layer number (it bounces at tool changes): only paces the trace
        self.first = None  # first measured estimate this print: (wall time, progress, hub left s, printer left s)
        self.speed, self.prog, self.samples = None, 0.0, []  # samples: (printing seconds, progress) at this speed
        self.sent = None  # (send time, leveling) of the hub job that started this file

    def load(self, name):
        path = job_file_for(name)
        job = job_for_file(name) or {}
        model = cum = None
        try:
            model = slicer.eta_model(slicer.read_gcode(path)) if path else None
            cum = slicer.eta_times(model, 1.0, self.scv) if model else None
        except Exception as e:
            log(f"eta model for {name} failed: {e}")
        with self.lock:
            if self.file == name:
                self.sent = (job["sent_ts"], bool(job.get("leveling"))) if job.get("sent_ts") else None
            if self.file == name and model:
                self.model, self.cum[1.0] = model, cum
        log(f"eta model for {name}: {f'{cum[-1]:.0f} s at 100%' if cum else 'none'} "
            f"(job file {'found' if path else 'not found'})")

    def load_speed(self, name, model, f):
        try:
            c = slicer.eta_times(model, f, self.scv)
        except Exception as e:
            c = None
            log(f"eta model for {name} at {f:.2f} failed: {e}")
        with self.lock:
            if self.file == name:
                self.cum[f] = c

    def update(self, d):
        now, name = time.time(), d.get("printFileName") or None
        st = (d.get("status") or "").lower()
        with self.lock:
            if name != self.file:
                self.reset(name)
                if name:
                    threading.Thread(target=self.load, args=(name,), daemon=True).start()
            if st == "printing" and self.last:
                self.active += min(now - self.last, 30)  # a stalled hub is not printing time
            self.last = now
            layer = d.get("printLayer") or 0
            if st != "printing" or not layer:
                return
            if self.layer is None or layer > self.layer:  # forward only: it bounces back around tool changes
                self.layer = layer
            speed = d.get("printSpeedAdjust") or 100
            prog = float(d.get("printProgress") or 0)
            prog = prog if prog <= 1 else prog / 100
            if prog < self.prog - 0.05:  # the same file started again
                self.samples, self.layer = [], layer
            if speed != self.speed:
                self.samples = []  # pace from another speed no longer applies
            self.speed = speed
            if prog > 0 and d.get("printDuration"):  # its clock starts at the first move, after heating and probing
                self.prog = prog
                self.samples.append((self.active, prog))
                while self.samples and self.samples[0][0] < self.active - self.window:
                    self.samples.pop(0)

    def times(self):
        """(seconds done per move at the printer's speed, extra factor) - called under the lock. Until that speed is
        worked out (a few seconds of CPU), the 100% times with about half the speed gain."""
        f = round((self.speed or 100) / 100, 2)
        if f not in self.cum:
            self.cum[f] = None  # being worked out
            threading.Thread(target=self.load_speed, args=(self.file, self.model, f), daemon=True).start()
        return (self.cum[f], 1.0) if self.cum[f] else (self.cum[1.0], 1 / (1 + 0.5 * (f - 1)))

    def at(self, prog):
        b = self.model["b"]
        return min(bisect.bisect_left(b, prog), len(b) - 1)

    def where(self):
        """(file layer, height mm, top height mm, layers) or None."""
        with self.lock:
            if not self.model or self.prog <= 0:
                return None
            zs = self.model["zs"]
            n = max(1, self.model["layer"][self.at(self.prog)])
            return n, (zs[n - 1] if n <= len(zs) else None), (zs[-1] if zs else None), len(zs)

    def left(self):
        """(seconds left, percent done by time, how it was worked out) or None without a model of this file."""
        with self.lock:
            if not self.model or not self.cum.get(1.0) or self.prog <= 0:
                return None
            c, guess = self.times()
            i = self.at(self.prog)
            k, how, s = self.k, "plan", self.samples
            if len(s) >= 2 and s[-1][0] - s[0][0] >= self.min_span:
                dm = (c[self.at(s[-1][1])] - c[self.at(s[0][1])]) * guess
                if dm > 0:
                    k = (s[-1][0] - s[0][0] + self.k * self.prior_s) / (dm + self.prior_s)
                    k, how = min(self.clamp, max(1 / self.clamp, k)) if self.clamp else k, "pace"
            return (c[-1] - c[i]) * guess * k, 100 * c[i] / c[-1], how


ETA = Eta(CFG.get("eta_scv", 5.0), CFG.get("eta_k", 1.0), CFG.get("eta_window_s", 21600), CFG.get("eta_min_span_s", 120),
          CFG.get("eta_prior_s", 1800), CFG.get("eta_clamp", 1.5))
# Send to first move, which the printer's clock leaves out: heating + probing. Measured 2026-10-27..29 (hub.log
# start times against printDuration): 17.7-18.7 min with leveling (6 PETG prints, bed 70), 4.3 min without (1 PLA).
START_S = {True: CFG.get("eta_start_level_s", 1090), False: CFG.get("eta_start_s", 260)}


def trace_layer(d):
    """One line per layer change in eta-trace.jsonl: what the hub and the printer each said, to measure and tune the
    time-left estimate against what really happened."""
    try:
        est, w = ETA.left(), ETA.where()
        rec = {"wall": round(time.time(), 1), "file": d.get("printFileName"), "layer": ETA.layer,
               "layers": d.get("targetPrintLayer"), "file_layer": w[0] if w else None, "z": w[1] if w else None,
               "active_s": round(ETA.active, 1),
               "printer_elapsed_s": d.get("printDuration"), "printer_left_s": d.get("estimatedTime"),
               "printer_progress": d.get("printProgress"), "speed": d.get("printSpeedAdjust"),
               "hub_left_s": round(est[0]) if est else None, "hub_from": est[2] if est else None}
        with open(os.path.join(HERE, "eta-trace.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + chr(10))
    except Exception as e:
        log(f"eta trace failed: {e}")


def poll_status():
    while True:
        try:
            if not (PR.ip and PR.serial and PR.check):
                raise RuntimeError("printer not set up yet: open Setup")
            r = PR.detail()
            if r.get("code") == 0 and r.get("detail"):
                STATE.update(detail=r["detail"], error=None, time=time.time())
                prev = ETA.layer
                ETA.update(r["detail"])
                if ETA.layer and ETA.layer != prev:
                    trace_layer(r["detail"])
                keep_hold(r["detail"])
            else:
                STATE.update(detail=None, error=r.get("message") or f"printer replied {r}", time=time.time())
        except Exception as e:
            STATE.update(detail=None, error=str(e), time=time.time())
        time.sleep(3)


def summary():
    d, err = STATE["detail"], STATE["error"]
    if not d:
        hint = ("Turn on LAN Only on the printer (Settings - Network)" if err and "lan mode" in err.lower()
                else err)
        return {"online": False, "error": hint}
    tools = []
    temps, targets = d.get("nozzleTemps") or [], d.get("nozzleTargetTemps") or []
    for i in range(max(len(temps), len(targets))):
        tools.append({"t": i + 1, "temp": round(float(temps[i]), 0) if i < len(temps) else None,
                      "target": round(float(targets[i]), 0) if i < len(targets) else None})
    slots = []
    ms = d.get("matlStationInfo") or {}
    for s in ms.get("slotInfos") or []:
        slots.append({"slot": s.get("slotId"), "material": s.get("materialName"),
                      "colour": s.get("materialColor"), "loaded": s.get("hasFilament")})
    prog = d.get("printProgress") or 0
    prog = prog * 100 if prog <= 1 else prog
    left, eta_from, where = d.get("estimatedTime"), "printer", None
    mine = (d.get("status") or "").lower() in ACTIVE and d.get("printFileName") == ETA.file
    est = ETA.left() if mine else None
    if est:
        left, prog, eta_from = round(est[0]), est[1], est[2]
        where = ETA.where()
    elif mine and not d.get("printDuration") and ETA.cum.get(1.0) and ETA.sent:
        # heating and probing: the start the printer's clock leaves out, then the whole print
        sent, level = ETA.sent
        left = round(max(60, START_S[level] - (time.time() - sent)) + ETA.cum[1.0][-1] * ETA.k)
        eta_from = "start"
    return {"online": True, "status": d.get("status"), "file": d.get("printFileName"),
            "progress": round(prog, 1), "layer": d.get("printLayer"),
            "layers": d.get("targetPrintLayer"),
            # the file's own layer and height at the printer's place in the file (its printLayer bounces)
            "file_layer": where[0] if where else None, "z": where[1] if where else None,
            "z_top": where[2] if where else None, "file_layers": where[3] if where else None, "left_s": left, "eta_from": eta_from,
            "printer_left_s": d.get("estimatedTime"), "speed": d.get("printSpeedAdjust"), "elapsed_s": d.get("printDuration"),
            "bed": round(float(d.get("platTemp") or 0)), "bed_target": round(float(d.get("platTargetTemp") or 0)),
            "chamber_target": round(float(d.get("chamberTargetTemp") or 0)),
            # chamberTemp -108 means no chamber sensor (ff-5mp-api-py changelog)
            "chamber": round(float(d.get("chamberTemp") or 0)) if float(d.get("chamberTemp") or 0) > -50 else None,
            "tools": tools, "slots": slots,
            "light": d.get("lightStatus"), "door": d.get("doorStatus"), "error_code": d.get("errorCode"),
            "thumb": bool(d.get("printFileThumbUrl"))}


# ---------- temperature and speed ----------
# The file sets each tool back to its print temperature at every tool change (idle tools sit at print -100) and
# resets speed with M220 S100, so a change made here is held for the rest of the print: when the printer reads a
# value the file set, the hub sends the held one again. A value set on the printer's screen ends that hold.

HOLD = {"file": None, "items": {}}  # "t1".."t4" / "bed" / "speed" -> {"value", "base": values the file sets, "sent"}
HOLD_LOCK = threading.Lock()
IDLE_BELOW = 150  # tool targets under this are the file's idle or off temperatures, never held over


def adjust_value(d, item):
    if item == "speed":
        v = d.get("printSpeedAdjust")
    elif item == "bed":
        v = d.get("platTargetTemp")
    else:
        t = d.get("nozzleTargetTemps") or []
        i = int(item[1]) - 1
        v = t[i] if i < len(t) else None
    return None if v is None else round(float(v))


def adjust_send(item, value):
    if item == "speed":
        return PR.speed(value)
    if item == "bed":
        return PR.temperature(bed=value)
    return PR.temperature(tool=int(item[1]), temp=value)


def file_values(name, item):
    """The values the running file sets for this item (print and first-layer temperatures, speed 100)."""
    if item == "speed":
        return [100]
    path = job_file_for(name)
    text = slicer.read_gcode(path) if path else ""
    keys = (("hot_plate_temp", "hot_plate_temp_initial_layer", "textured_plate_temp",
             "textured_plate_temp_initial_layer") if item == "bed"
            else ("nozzle_temperature", "nozzle_temperature_initial_layer"))
    out = []
    for k in keys:
        vals = (slicer.header(text, k) or "").split(",")
        i = 0 if item == "bed" else int(item[1]) - 1
        if i < len(vals) and vals[i].strip().lstrip("-").isdigit():
            out.append(int(vals[i]))
    return out


def adjust_limits(item, s, printing):
    if item == "speed":
        return 50, 150
    if item == "bed":
        return (40 if printing else 0), 100
    slot = next((x for x in s["slots"] if x["slot"] == int(item[1])), {})
    rng = (CFG["material_ranges"].get(slicer.family(slot.get("material") or "")) or {}).get("nozzle") or [170, 280]
    return rng if printing else (0, rng[1])  # 0 (off) only when nothing is printing


def adjust(item, value):
    if item not in ("t1", "t2", "t3", "t4", "bed", "speed"):
        raise ValueError("unknown setting")
    if DRY.refuse():
        raise ValueError(DRY.refuse())
    s = summary()
    if not s["online"]:
        raise ValueError(f"printer not reachable: {s['error']}")
    st, value = (s["status"] or "").lower(), int(value)
    printing = st in ACTIVE
    if item == "speed" and not printing:
        raise ValueError("speed only changes during a print")
    lo, hi = adjust_limits(item, s, printing)
    if not (lo <= value <= hi or (value == 0 and lo == 0)):
        raise ValueError(f"{item.upper()} {value} is outside {lo}-{hi}")
    d = STATE["detail"] or {}
    before = adjust_value(d, item)
    tool = item.startswith("t")
    if printing and tool and (before or 0) < IDLE_BELOW:
        # A parked tool: heating it now only oozes. Hold the new value for when the file heats it again.
        base = sorted(set(file_values(s["file"], item)) - {value})
        if not base:
            raise ValueError(f"{item.upper()} is idle and this file was not sent by {NAME}, "
                             "so set it while that tool is printing")
        with HOLD_LOCK:
            if HOLD["file"] != s["file"]:
                HOLD.update(file=s["file"], items={})
            HOLD["items"][item] = {"value": value, "base": base, "sent": 0}
        log(f"adjust {item} idle at {before}: {value} held for its next tool change")
        return {"ok": True, "held": True, "value": value, "note": f"{item.upper()} is parked; it heats to {value} "
                "at its next tool change"}
    r = adjust_send(item, value)
    log(f"adjust {item} {before} -> {value}: {r}")
    if not PR.ok(r):
        raise ValueError(f"printer refused: {r}")
    applied = False
    for _ in range(8):  # the firmware ACKs commands it ignores, so read the value back
        time.sleep(1)
        rd = PR.detail()
        if rd.get("code") == 0 and adjust_value(rd.get("detail") or {}, item) == value:
            applied = True
            break
    held = False
    with HOLD_LOCK:
        if printing and applied:
            if HOLD["file"] != s["file"]:
                HOLD.update(file=s["file"], items={})
            prev = HOLD["items"].get(item)
            base = set(file_values(s["file"], item)) | set(prev["base"] if prev else [])
            if before is not None and not prev:
                base.add(before)  # a parked tool returned above, so this is a value the file set
            base.discard(value)
            HOLD["items"][item] = {"value": value, "base": sorted(base), "sent": time.time()}
            held = True
        elif applied:
            HOLD["items"].pop(item, None)
    return {"ok": applied, "held": held, "value": value,
            "note": None if applied else "the printer said OK but the value did not change"}


def keep_hold(d):
    st, name = (d.get("status") or "").lower(), d.get("printFileName") or None
    with HOLD_LOCK:
        if not HOLD["items"]:
            return
        if st not in ACTIVE or name != HOLD["file"]:
            log(f"hold ended ({st}): {HOLD['items']}")
            HOLD.update(file=None, items={})
            return
        if st != "printing":
            return
        for item, h in list(HOLD["items"].items()):
            now = adjust_value(d, item)
            if now is None or now == h["value"] or time.time() - h["sent"] < 6:
                continue
            if now in h["base"]:
                h["sent"] = time.time()
                r = adjust_send(item, h["value"])
                log(f"hold {item}: file set {now}, sent {h['value']} again: {r}")
            elif item in ("bed", "speed") or now >= IDLE_BELOW:
                log(f"hold {item} dropped: printer now {now} (changed on the screen?)")
                del HOLD["items"][item]


def hold_state():
    with HOLD_LOCK:
        return {k: v["value"] for k, v in HOLD["items"].items()}


def watch_scene(name):
    """What the running file should have put on the plate, layer by layer (None for a file the hub didn't send)."""
    path = job_file_for(name)
    return slicer.scene(slicer.read_gcode(path)) if path else None


WATCH = watcher.Watcher(CFG, PR, CAM, summary, log, watch_scene, slicer.colour_name, WEBCAM)


def dry_judge(img, prompt, view):
    """Drying bed check: Sonnet (not Haiku), since a wrong 'empty' lets a print home the bed into the spools.
    No AI set up: 'unsure', and you unlock by eye on the phone page. The simulator sets dry_judge to a fixed verdict
    so its tests never call Claude."""
    fixed = CFG.get("dry_judge")
    if fixed == "file":  # simulator: the test writes the verdict it wants into drying/sim-verdict.txt
        fixed = open(os.path.join(DRY.dir, "sim-verdict.txt")).read().strip()
    if fixed:
        return {"verdict": fixed, "reason": "simulator verdict"}
    if not watcher.backend(CFG):
        return {"verdict": "unsure", "reason": "no AI check set up - look at the bed yourself, then use the unlock"}
    return WATCH.ask(img, "", CFG.get("dry_check_model", "sonnet"), view, system=prompt)


DRY = dryer.Dryer(CFG, PR, summary, log, WATCH.notify,
                  ([("phone", WEBCAM, 8)] if WEBCAM else []) + [("printer", CAM, 20)], dry_judge,
                  os.path.join(HERE, "drying"))


# ---------- jobs ----------

def job_dir(jid):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}", jid or ""):
        raise ValueError("bad job id")
    return os.path.join(JOBS, jid)


def load_job(jid):
    return json.load(open(os.path.join(job_dir(jid), "job.json")))


def save_job(job):
    p = os.path.join(job_dir(job["id"]), "job.json")
    json.dump(job, open(p + ".tmp", "w"), indent=1)
    os.replace(p + ".tmp", p)


def new_job(name, source):
    jid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    os.makedirs(job_dir(jid))
    job = {"id": jid, "name": name, "source": source, "created": time.strftime("%Y-%m-%d %H:%M"),
           "state": "new", "parts": [], "layer": "0.2", "sliced_file": None}
    save_job(job)
    return job


def add_file(job, fname, path):
    low = fname.lower()
    if low.endswith(".stl"):
        if job.get("kind") == "sliced":
            raise ValueError("this job already holds a sliced file")
        job["kind"] = "model"
        used = {p["tool"] for p in job["parts"]}
        tool = next((t for t in (1, 2, 3, 4) if t not in used), 1)
        job["parts"].append({"file": os.path.basename(path), "name": os.path.splitext(fname)[0], "tool": tool})
        job["state"] = "new"
    elif low.endswith((".gcode", ".3mf")):
        text = slicer.read_gcode(path)
        if not text:
            raise ValueError("that 3MF has no sliced plate in it. Upload the STL instead, "
                             "or slice it in Flash Studio and use Export plate sliced file")
        if job["parts"]:
            raise ValueError("start a new print for a sliced file")
        job.update(kind="sliced", sliced_file=os.path.basename(path), state="ready", stats=slicer.stats(text))
        trusted = (job["stats"].get("slicer") or "").startswith("Flash Studio")
        job["sanity"] = slicer.sanity(text, CFG["material_ranges"]) + slicer.preset_check(text, CFG.get("house_presets"))
        reasons = ([] if trusted else [f"sliced by {job['stats'].get('slicer') or 'an unknown slicer'}, not Flash Studio"])
        reasons += job["sanity"]
        job["check"] = {"passed": not reasons, "reasons": reasons}
        slicer.preview_png(text, pad_colours(job["stats"]["colours"]), os.path.join(job_dir(job["id"]), "preview.png"))
        job["design"] = {"state": "checking"}
    else:
        raise ValueError("upload .stl, .gcode or a sliced .3mf")
    save_job(job)
    if job.get("design") == {"state": "checking"}:
        threading.Thread(target=design_job, args=(job["id"],), daemon=True).start()


def design_job(jid):
    """design_check.py on the sliced file: footprint, floating, span, fused. Findings block the send like any
    other failed check (Send anyway still overrides). A crash in the check only logs: it never blocks a print."""
    try:
        import design_check
        job = load_job(jid)
        d = job_dir(jid)
        with DESIGN_LOCK:
            info = {}
            found = design_check.check(slicer.read_gcode(os.path.join(d, job["sliced_file"])), d, info)
        reasons = design_check.reasons(found)
        with JOB_LOCK:
            job = load_job(jid)
            job["design"] = {"state": "done", "reasons": reasons, "pieces": info.get("pieces"),
                             "images": sorted(f for f in os.listdir(d) if f.startswith("design-"))}
            if reasons:
                c = job.get("check") or {"passed": True, "reasons": []}
                job["check"] = {"passed": False, "reasons": c.get("reasons", []) + reasons}
            save_job(job)
        log(f"design check {jid}: {len(reasons)} found, {info.get('pieces')} pieces" + "".join("; " + r for r in reasons))
        estimate_job(jid)
    except Exception as e:
        log(f"design check {jid} failed: {e}")
        log(traceback.format_exc())
        with JOB_LOCK:
            job = load_job(jid)
            job["design"] = {"state": "error", "error": str(e)}
            save_job(job)


def estimate_job(jid):
    """Real printing time at 100% from the hub's model of the file (fitted within 2.5%), and the start with and
    without leveling, for the Send sheet's done time. After the design check, so it never holds a send."""
    try:
        job = load_job(jid)
        model = slicer.eta_model(slicer.read_gcode(os.path.join(job_dir(jid), job["sliced_file"])))
        if not model:
            return
        est = {"print_s": round(slicer.eta_times(model, 1.0, ETA.scv)[-1] * ETA.k), "start_s": START_S[False],
               "start_level_s": START_S[True]}
        with JOB_LOCK:
            job = load_job(jid)
            job["est"] = est
            save_job(job)
    except Exception as e:
        log(f"time estimate {jid} failed: {e}")


def pad_colours(pairs):
    cols = list(CFG["colours"])
    for i, c in pairs:
        cols[int(i) - 1] = c
    return cols


def slice_job(jid):
    with SLICE_LOCK:
        with JOB_LOCK:
            job = load_job(jid)
            job["state"] = "slicing"
            save_job(job)
        d = job_dir(jid)
        try:
            parts = [(p["name"], os.path.join(d, p["file"]), int(p["tool"])) for p in job["parts"]]
            work = os.path.join(d, "slice")
            shutil.rmtree(work, ignore_errors=True)
            r = slicer.slice_parts(work, parts, CFG["colours"], job.get("layer", "0.2"),
                                   job.get("engine") or CFG["engine"], fine=bool(job.get("detail")))
            open(os.path.join(d, "slice.log"), "w", encoding="utf-8").write("\n\n".join(r["log"]))
            if not r["ok"]:
                job.update(state="failed", error=r["error"])
            else:
                open(os.path.join(d, "verify.txt"), "w", encoding="utf-8").write(r["verify"])
                text = slicer.read_gcode(r["gcode"])
                safe = re.sub(r"[^A-Za-z0-9_-]+", "_", job["name"])[:40] or "print"
                out = f"{safe}.gcode"
                shutil.copy(r["gcode"], os.path.join(d, out))
                slicer.preview_png(text, CFG["colours"], os.path.join(d, "preview.png"))
                reasons = r["reasons"] + slicer.preset_check(text, CFG.get("house_presets"))
                job.update(state="blocked" if reasons else "ready", sliced_file=out, stats=slicer.stats(text),
                           check={"passed": not reasons, "reasons": reasons}, dims=r["dims"], error=None,
                           engine=r["engine"], engine_note=r["fallback"], fine=r["fine"], design={"state": "checking"})
        except Exception as e:
            job.update(state="failed", error=str(e))
            log(traceback.format_exc())
        with JOB_LOCK:
            save_job(job)
        log(f"sliced {jid}: {job['state']} {job.get('error') or job.get('check')}")
        if job.get("design") == {"state": "checking"}:
            design_job(jid)


def used_tools(job):
    return [i for i, g in enumerate(job["stats"].get("grams_per_tool") or [1], 1) if g > 0] or [1]


def backfill(job):
    """Jobs from before the material and upload checks: add what they lack, once."""
    if "materials" in job["stats"] and not (job.get("kind") == "sliced" and "sanity" not in job):
        return job
    text = slicer.read_gcode(os.path.join(job_dir(job["id"]), job["sliced_file"]))
    with JOB_LOCK:
        job = load_job(job["id"])
        job["stats"]["materials"] = slicer.materials(text)
        if job.get("kind") == "sliced" and "sanity" not in job:
            job["sanity"] = slicer.sanity(text, CFG["material_ranges"])
            if job["sanity"]:
                job["check"] = {"passed": False, "reasons": (job.get("check") or {}).get("reasons", []) + job["sanity"]}
        save_job(job)
    return job


def material_problems(job, s):
    """Every tool the file uses needs its slot loaded with the material the file was sliced for
    (without a mapping, tool N prints from slot N)."""
    mats = job["stats"].get("materials") or []
    slots = {x["slot"]: x for x in s.get("slots") or []}
    out = []
    for t in used_tools(job):
        slot, want = slots.get(t) or {}, mats[t - 1] if t - 1 < len(mats) else None
        if slot.get("loaded") is False:
            out.append(f"T{t} needs slot {t}, which is empty")
        elif want and slot.get("material") and slicer.family(want) != slicer.family(slot["material"]):
            out.append(f"T{t} was sliced for {want}, slot {t} has {slot['material']}")
    return out


def colour_notes(job, s):
    """File colour against the colour loaded in each used slot, by palette name. A note, not a block."""
    cols = dict(job["stats"].get("colours") or [])
    slots = {x["slot"]: x for x in s.get("slots") or []}
    out = []
    for t in used_tools(job):
        want, have = slicer.colour_name(cols.get(str(t))), slicer.colour_name((slots.get(t) or {}).get("colour"))
        if want and have and want != have:
            out.append(f"T{t} file colour {want}, slot {t} has {have}")
    return out


def z_offset(job):
    """Hub Z offset for the job's material (config z_offset_by_material). Only when every used tool is that material:
    a mixed plate shares one first layer, and PLA is tuned at 0. Returns (dz, note)."""
    table = CFG.get("z_offset_by_material") or {}
    mats = job["stats"].get("materials") or []
    fams = {slicer.family(mats[t - 1]) if t - 1 < len(mats) else "?" for t in used_tools(job)}
    if not any(table.get(f) for f in fams):
        return 0, None
    if len(fams) > 1:
        return 0, f"mixed plate ({', '.join(sorted(fams))}): no Z offset"
    f = fams.pop()
    return table[f], f"{f}: printed {table[f]:+.2f} mm higher (hub Z offset)"


def shifted_file(job, dz):
    """The job's file raised by dz, built once per dz in the job folder. Raises ValueError if it cannot be done safely."""
    d = job_dir(job["id"])
    src = os.path.join(d, job["sliced_file"])
    out = os.path.join(d, f"z{dz:+.2f}")
    dst = os.path.join(out, job["sliced_file"])
    if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return dst
    os.makedirs(out, exist_ok=True)
    reasons = slicer.write_shifted(src, dst + ".part", dz)
    if reasons:
        raise ValueError(f"could not apply the {dz:+.2f} mm Z offset: " + "; ".join(reasons[:2]))
    os.replace(dst + ".part", dst)
    return dst


def send_job(jid, leveling, plate_clear, allow_unchecked=False):
    if DRY.refuse():
        raise ValueError(DRY.refuse())
    job = load_job(jid)
    if job.get("sliced_file") and job.get("stats"):
        job = backfill(job)
    if job.get("state") not in ("ready", "sent", "done") and not (job.get("state") == "blocked" and allow_unchecked):
        raise ValueError(f"job is {job.get('state')}, not ready")
    if (job.get("design") or {}).get("state") == "checking" and not allow_unchecked:
        raise ValueError("the design check is still reading this file, try again in a minute")
    if not job.get("check", {}).get("passed") and not allow_unchecked:
        raise ValueError("this file did not pass the check: " + "; ".join(job.get("check", {}).get("reasons", [])))
    if not plate_clear:
        raise ValueError("confirm the plate is clear first")
    s = summary()
    if not s["online"]:
        raise ValueError(f"printer not reachable: {s['error']}")
    problems = material_problems(job, s)
    if problems and not allow_unchecked:
        raise ValueError("; ".join(problems))
    status = (s["status"] or "").lower()
    if status in ("completed", "cancel"):
        PR.clear_platform()
        time.sleep(2)
    elif status not in ("ready",):
        raise ValueError(f"printer is {status}, not ready")
    path = os.path.join(job_dir(jid), job["sliced_file"])
    name = job["sliced_file"]
    dz, note = z_offset(job)
    if dz:
        try:
            path = shifted_file(job, dz)
        except ValueError:
            if not allow_unchecked:
                raise
            dz, note = 0, "Z offset skipped (Send anyway)"
    tools = used_tools(job)
    r = PR.upload(path, name, len(tools), leveling)
    log(f"upload {jid} {name} z_offset={dz}: {r}")
    if not PR.ok(r):
        raise ValueError(f"upload refused: {r}")
    mappings = None
    if name.lower().endswith(".3mf"):  # the firmware applies mappings to .3mf files only
        slots = {x["slot"]: x for x in s["slots"]}
        cols = dict(job["stats"]["colours"])
        mappings = [{"toolId": t - 1, "slotId": t, "materialName": (slots.get(t) or {}).get("material") or "PLA",
                     "toolMaterialColor": cols.get(str(t), CFG["colours"][t - 1]).upper(),
                     "slotMaterialColor": ((slots.get(t) or {}).get("colour") or CFG["colours"][t - 1]).upper()}
                    for t in tools]
    r = PR.start(name, leveling, mappings)
    log(f"start {jid} {name} leveling={leveling}: {r}")
    if not PR.ok(r):
        raise ValueError(f"start refused: {r}")
    with JOB_LOCK:
        job = load_job(jid)
        job.update(state="sent", sent=time.strftime("%Y-%m-%d %H:%M"), sent_ts=time.time(), leveling=bool(leveling),
                   z_offset=dz)
        save_job(job)
    return job


def history(s, result):
    """One line per finished or cancelled print: real time against the slicer, the printer and the hub estimates
    (idea from flashforge-c5-dashboard's job history). jobs/../print-history.jsonl"""
    job = job_for_file(s["file"]) or {}
    rec = {"end": time.strftime("%Y-%m-%d %H:%M"), "file": s["file"], "result": result, "layers": s["layers"],
           "slicer_estimate": (job.get("stats") or {}).get("time"), "printer_elapsed_s": s.get("elapsed_s"),
           "hub_printing_s": round(ETA.active)}
    if ETA.first:
        t0, prog, hub_left, printer_left = ETA.first
        rec.update(from_progress=prog, actual_left_s=round(time.time() - t0), hub_said_s=hub_left,
                   printer_said_s=printer_left)
    with open(os.path.join(HERE, "print-history.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + chr(10))
    log(f"history {rec}")


def push_events():
    """Phone push for what matters away from the printer: done, cancelled, paused by the printer, error codes,
    printer gone quiet mid-print, and hub started. State changes as in flashforge-c5-dashboard."""
    prev, quiet_since, quiet_sent, codes, started = None, None, False, set(), False
    mine = lambda a: time.time() - LAST_CONTROL.get(a, 0) < 90
    hms = lambda x: f"{int(x or 0) // 3600}h {int(x or 0) % 3600 // 60}m"
    while True:
        time.sleep(2)
        try:
            s = summary()
            if not started and (s["online"] or time.time() - STARTED > 60):
                started = True
                what = (f"printer {s['status']}" + (f", {s['file']} layer {s['layer']}/{s['layers']}"
                                                    if (s["status"] or "").lower() in ACTIVE else "")
                        if s["online"] else f"printer not reachable: {s['error']}")
                WATCH.notify(f"{NAME} started - {what}", "default")
            if not s["online"]:
                if prev and (prev["status"] or "").lower() in ACTIVE:
                    quiet_since = quiet_since or time.time()
                    if not quiet_sent and time.time() - quiet_since > 300:
                        WATCH.notify(f"Printer not answering for 5 min during {prev['file']}: {s['error']}")
                        quiet_sent = True
                continue  # prev stays the last state seen online
            if quiet_sent:
                WATCH.notify(f"Printer answering again: {s['status']}", "default")
            quiet_since, quiet_sent = None, False
            st, was = (s["status"] or "").lower(), (prev["status"] or "").lower() if prev else None
            code = str(s.get("error_code") or "")
            code = "" if code == "0" else code
            why = f" - {code} {ERRORS.get(code, '')}".rstrip() if code else ""
            ev = WATCH.state.get("event") or {}
            if prev and st != was:
                # A pause with an error code is the printer's own (run-out, clog), whoever paused last.
                if st in PAUSED and was not in PAUSED and (code or not mine("pause")) and time.time() - ev.get("at", 0) > 180:
                    WATCH.notify(f"Paused: {s['file']} at layer {s['layer']}/{s['layers']}{why}")
                    codes.add(code)
                elif st == "completed" and was in ACTIVE:
                    WATCH.notify(f"Done: {prev['file']} ({hms(prev.get('elapsed_s'))})", "default")
                if st in ("completed", "cancel") and was in ACTIVE:
                    history(prev, st)
                elif st == "cancel" and was in ACTIVE and not mine("cancel"):
                    WATCH.notify(f"Cancelled on the printer: {prev['file']} at layer {prev['layer']}/{prev['layers']}")
            if s.get("eta_from") == "pace" and not ETA.first and s["file"] == ETA.file:
                ETA.first = (time.time(), s["progress"], s["left_s"], s["printer_left_s"])
            if code and code not in codes:
                WATCH.notify(f"Printer error{why} ({st})")
                codes.add(code)
            elif not code:
                codes.clear()
            prev = s
        except Exception:
            log(traceback.format_exc())


# ---------- 3D viewer ----------
# The phone's spin viewer shows a job's STL parts, or for a sliced-only job its G-code toolpath, built once here
# (toolpath.py) and cached in the job folder.

TP = {}  # job id -> {"progress": 0-1} while its toolpath builds, {"error": ...} if it failed
TP_LOCK = threading.Lock()   # guards TP, so two requests never start the same build
TP_BUILD = threading.Lock()  # one build at a time: a 127 MB file takes ~20 s of CPU
STATIC_GZ = {}  # (path, mtime) -> gzipped script, so three.js crosses cellular at ~170 KB, not 690 KB


def job_file(jid, name):
    """A file directly inside this job's own folder, or ValueError for any name that could leave it."""
    d = job_dir(jid)
    if not name or ".." in name or any(c in name for c in "/\\:\0") or name != os.path.basename(name):
        raise ValueError("bad file name")
    f = os.path.join(d, name)
    if os.path.dirname(os.path.realpath(f)) != os.path.realpath(d):
        raise ValueError("bad file name")
    return f


def model_info(job):
    d = job_dir(job["id"])
    parts = [{"file": p["file"], "name": p["name"], "tool": int(p["tool"]),
              "size": os.path.getsize(os.path.join(d, p["file"]))}
             for p in job.get("parts") or [] if p.get("file", "").lower().endswith(".stl")
             and os.path.isfile(os.path.join(d, p["file"]))]
    if parts:
        return {"kind": "stl", "parts": parts}
    src = os.path.join(d, job["sliced_file"]) if job.get("sliced_file") else None
    if src and os.path.isfile(src):
        return {"kind": "toolpath", "file": job["sliced_file"], "size": os.path.getsize(src),
                "ready": toolpath_ready(job)}
    return {"kind": "none"}


def toolpath_out(jid):
    return os.path.join(job_dir(jid), f"toolpath-v{toolpath.VERSION}.bin.gz")


def toolpath_ready(job):
    out, src = toolpath_out(job["id"]), os.path.join(job_dir(job["id"]), job["sliced_file"])
    return os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src)


def build_toolpath(jid, src):
    with TP_BUILD:
        t = time.time()
        try:
            data = toolpath.build(src, lambda f: TP[jid].update(progress=round(f, 3)))
            out = toolpath_out(jid)
            open(out + ".tmp", "wb").write(data)
            os.replace(out + ".tmp", out)
            TP.pop(jid, None)
            log(f"toolpath {jid}: {len(data)} bytes in {time.time() - t:.1f} s")
        except Exception as e:
            TP[jid] = {"error": f"could not read the G-code: {e}"}
            log(f"toolpath {jid} failed: {traceback.format_exc()}")


def job_for_file(name):
    """The newest job whose sliced file the printer is running, searching every job (the page lists only 40)."""
    for jid in sorted(os.listdir(JOBS), reverse=True):
        try:
            j = load_job(jid)
        except Exception:
            continue
        if name and j.get("sliced_file") == name:
            return j
    return None


def job_file_for(name):
    j = job_for_file(name)
    return os.path.join(job_dir(j["id"]), j["sliced_file"]) if j else None


def job_settings(job):
    """The settings in the file this job sent to the printer, laid out for the phone: the main ones per group, what
    the hub changed at send and during the print, per-part changes, then every line of the settings block."""
    d = job_dir(job["id"])
    path = os.path.join(d, job["sliced_file"])
    text = slicer.read_gcode(path) or ""
    cfg = slicer.config(text)
    if not cfg:
        raise ValueError("no settings block in this file")
    used = used_tools(job) if job.get("stats") else [1]

    def vals(key):
        v = cfg.get(key)
        return None if v is None else [x.strip().strip('"') for x in (v.split(";") if ";" in v else v.split(","))]

    def tool(key, t):
        v = vals(key)
        return None if not v else v[t - 1] if t - 1 < len(v) else v[0]

    def one(key, unit=""):
        v = cfg.get(key)
        return None if v is None else v.strip().strip('"') + unit

    def group(name, rows):
        rows = [[k, v] for k, v in rows if v not in (None, "")]
        return {"name": name, "rows": rows} if rows else None

    parts = []
    for src in (path, os.path.join(d, "slice", "out", "slice-input.3mf")):
        if src.lower().endswith(".3mf") and os.path.isfile(src) and not parts:
            parts = [{"name": n, "rows": [[k, v] for k, v in s]} for n, s in slicer.part_settings(src)]
    over = {}  # per-part values the settings block cannot show, by key
    for p in parts:
        for k, v in p["rows"]:
            over.setdefault(k.split(": ")[-1], set()).add(v)

    def eff(key, unit=""):
        # the value the parts really print with: a per-part change wins over the plate-wide line
        if key not in over:
            return one(key, unit)
        return f"{' / '.join(sorted(over[key]))}{unit} (set per part, file default {one(key, unit)})"

    plate = (cfg.get("curr_bed_type") or "").strip('"')
    pk = next((k for w, k in (("textured", "textured_plate_temp"), ("cool", "cool_plate_temp"),
                              ("engineering", "eng_plate_temp")) if w in plate.lower()), "hot_plate_temp")
    num = lambda x: float(x) if re.fullmatch(r"-?[\d.]+", x or "") else 0
    bed = [max((num(tool(k, t)) for t in used), default=0) for k in (pk, pk + "_initial_layer")]
    brim = one("brim_type") or ""
    brim = "none" if brim == "no_brim" else f"{brim.replace('_', ' ').replace('auto brim', 'auto')}, {one('brim_width', ' mm')}" if brim else None
    groups = [group("Presets", [["Printer", one("printer_settings_id")], ["Process", one("print_settings_id")],
                                ["Slicer", (re.search(r"^; generated by (.+)$", text[:5000], re.M) or [None, None])[1]]]),
              group("Layers", [["Layer height", one("layer_height", " mm")
                                + (" (Detail on: finer where slopes would step)" if job.get("detail") else "")],
                               ["First layer", one("initial_layer_print_height", " mm")],
                               ["First layer flow", one("first_layer_flow_ratio")],
                               ["Elephant foot", one("elefant_foot_compensation", " mm")]]),
              group("Plate", [["Plate", plate], ["Bed", f"{bed[0]:g}° (first layer {bed[1]:g}°)" if bed[0] else None],
                              ["Brim", brim], ["Supports", one("support_type") if one("enable_support") == "1" else "off"],
                              ["Prime tower", f"on, {one('prime_tower_width', ' mm')}" if one("enable_prime_tower") == "1"
                               else "off" if len(used) > 1 else None]]),
              group("Strength", [["Walls", eff("wall_loops")], ["Top layers", eff("top_shell_layers")],
                                 ["Bottom layers", eff("bottom_shell_layers")],
                                 ["Infill", f"{eff('sparse_infill_density')} {eff('sparse_infill_pattern') or ''}".strip()
                                  if one("sparse_infill_density") else None]]),
              group("Speed (mm/s)", [["Outer wall", one("outer_wall_speed")], ["Inner wall", one("inner_wall_speed")],
                                     ["Infill", one("sparse_infill_speed")], ["Top surface", one("top_surface_speed")],
                                     ["First layer", one("initial_layer_speed")], ["Travel", one("travel_speed")]]),
              group("Surface", [["Seam", one("seam_position")], ["Top pattern", one("top_surface_pattern")],
                                ["Ironing", "off" if one("ironing_type") in ("no ironing", None) else one("ironing_type")]])]
    names = vals("filament_settings_id") or []
    for t in used:
        fan = [tool("fan_min_speed", t), tool("fan_max_speed", t)]
        ch = tool("chamber_temperature", t)
        groups.append(group(f"T{t} - {names[t - 1] if t - 1 < len(names) else '?'} ({tool('filament_type', t)})", [
            ["Nozzle", f"{tool('nozzle_temperature', t)}° (first layer {tool('nozzle_temperature_initial_layer', t)}°)"],
            ["Chamber", f"{ch}°" if num(ch) > 0 else None],
            ["Flow ratio", tool("filament_flow_ratio", t)],
            ["Max flow", f"{tool('filament_max_volumetric_speed', t)} mm³/s"],
            ["Retraction", f"{tool('retraction_length', t)} mm at {tool('retraction_speed', t)} mm/s"],
            ["Pressure advance", tool("pressure_advance", t) if tool("enable_pressure_advance", t) == "1" else "off"],
            ["Fan", f"{fan[0]}%" if fan[0] == fan[1] else f"{fan[0]}-{fan[1]}%"]]))
    hub = [["Sent", job.get("sent")], ["Bed leveling", "yes" if job.get("leveling") else "no"],
           ["Z offset", f"{job['z_offset']:+.2f} mm" if job.get("z_offset") else "none"]]
    with HOLD_LOCK:
        if HOLD["file"] == job["sliced_file"]:
            hub += [[f"Changed during print: {k.upper() if k != 'speed' else 'speed'}",
                     f"{v['value']}{'%' if k == 'speed' else '°'}"] for k, v in HOLD["items"].items()]
    groups.insert(0, group(f"{NAME} at send", hub))
    return {"name": job["name"], "file": job["sliced_file"], "groups": [g for g in groups if g], "parts": parts,
            "all": [[k, v if len(v) <= 300 else v[:300] + " ..."] for k, v in sorted(cfg.items())]}


def list_jobs():
    out = []
    for jid in sorted(os.listdir(JOBS), reverse=True)[:40]:
        try:
            out.append(load_job(jid))
        except Exception:
            pass
    return out


# ---------- http ----------

def token():
    return hmac.new(CFG["secret"].encode(), CFG["password"].encode(), hashlib.sha256).hexdigest()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def setup_open(self):
        """The setup page without a PIN: only before setup is done, and only from this PC."""
        return not CFG.get("setup_done") and self.local()

    def authed(self):
        if hmac.compare_digest(self.headers.get("X-Hub-Key", ""), CFG["password"]):
            return True
        for c in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = c.strip().partition("=")
            if k == "hub" and hmac.compare_digest(v, token()):
                return True
        return False

    def send(self, code, body, ctype="application/json", extra=None):
        if not isinstance(body, bytes):
            body = (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype, cache, gz=None):
        """A file with an ETag (304 when the phone already has it), streamed. gz: "stored" when the file on disk is
        gzipped, "make" to gzip it here (small scripts only), None to send it as it is."""
        st = os.stat(path)
        tag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        head = {"ETag": tag, "Cache-Control": cache}
        if gz:
            head["Vary"] = "Accept-Encoding"
        if self.headers.get("If-None-Match") == tag:
            self.send_response(304)
            for k, v in head.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        body = None
        if gz:
            accepts = "gzip" in (self.headers.get("Accept-Encoding") or "")
            raw = open(path, "rb").read()
            if gz == "make" and accepts:
                key = (path, st.st_mtime_ns)
                if key not in STATIC_GZ:
                    STATIC_GZ[key] = gzip.compress(raw, 9)
                body = STATIC_GZ[key]
            elif gz == "stored" and not accepts:
                body = gzip.decompress(raw)
            else:
                body = raw
            if accepts:
                head["Content-Encoding"] = "gzip"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body) if body is not None else st.st_size))
        for k, v in head.items():
            self.send_header(k, v)
        self.end_headers()
        if body is not None:
            return self.wfile.write(body)
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def body(self, limit=300_000_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("file too large")
        return self.rfile.read(n)

    def do_GET(self):
        try:
            self.route("GET")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except ValueError as e:  # refusals meant for the user (not ready, plate not confirmed, ...)
            self.send(400, {"error": str(e)})
        except Exception as e:
            log(traceback.format_exc())
            self.send(400, {"error": str(e)})

    def do_POST(self):
        try:
            self.route("POST")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except ValueError as e:  # refusals meant for the user (not ready, plate not confirmed, ...)
            self.send(400, {"error": str(e)})
        except Exception as e:
            log(traceback.format_exc())
            self.send(400, {"error": str(e)})

    def route(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p == "/login" and method == "POST":
            pw = json.loads(self.body(10000) or b"{}").get("password", "")
            if hmac.compare_digest(pw.strip(), CFG["password"]):
                return self.send(200, {"ok": True}, extra={
                    "Set-Cookie": f"hub={token()}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict"})
            time.sleep(1)
            return self.send(403, {"error": "wrong password"})
        if p in ("/", "/index.html", "/setup"):
            if not CFG.get("setup_done"):
                if self.local():
                    page = "setup.html"
                else:
                    return self.send(200, NOT_SET_UP, "text/html; charset=utf-8")
            elif not self.authed():
                page = "login.html"
            else:
                page = "setup.html" if p == "/setup" else "index.html"
            return self.send(200, open(os.path.join(HERE, "static", page), "rb").read(), "text/html; charset=utf-8")
        if p.startswith("/api/setup"):
            if not (self.setup_open() or self.authed()):
                return self.send(401, {"error": "login"})
            return setup_route(self, method, p, q)
        if p == "/manifest.json":
            return self.send(200, open(os.path.join(HERE, "static", "manifest.json"), "rb").read(),
                             "application/manifest+json")
        if p == "/icon.svg":
            return self.send(200, open(os.path.join(HERE, "static", "icon.svg"), "rb").read(), "image/svg+xml")
        if not self.authed():
            return self.send(401, {"error": "login"})

        if p == "/api/status":
            return self.send(200, {"printer": summary(), "slicing": SLICE_LOCK.locked(), "watch": WATCH.state,
                                   "colours": CFG["colours"], "leveling": CFG["leveling"], "hold": hold_state(),
                                   "webcam": bool(WEBCAM), "drying": DRY.public(), "watch_ready": bool(watcher.backend(CFG))})
        if p == "/api/watch" and method == "POST":
            req = json.loads(self.body(10000) or b"{}")
            if "enabled" in req:
                WATCH.set_enabled(req["enabled"])
                save_cfg(watch=bool(req["enabled"]))
            if req.get("dismiss"):
                WATCH.state["event"] = None
            return self.send(200, WATCH.state)
        m = re.fullmatch(r"/watch/(\d{8}-\d{6}\.jpg)", p)
        if m:
            for d in (WATCH.dir, WATCH.keep):  # rolling frames, then the ones kept for good
                if os.path.exists(os.path.join(d, m.group(1))):
                    return self.send(200, open(os.path.join(d, m.group(1)), "rb").read(), "image/jpeg")
        if p == "/api/drying" and method == "POST":
            req = json.loads(self.body(10000) or b"{}")
            a = req.get("action")
            if a == "start":
                r = DRY.start(req.get("preset"), req.get("bed"), req.get("chamber"), req.get("hours"), req.get("confirm"))
            elif a == "stop":
                r = DRY.stop()
            elif a == "resume":
                r = DRY.resume(req.get("hours"), req.get("confirm"))
            elif a == "check":
                r = DRY.bed_check(bool(req.get("all_out")))
            elif a == "unlock":
                if not req.get("i_checked"):
                    raise ValueError("confirm you looked at the bed yourself")
                DRY.unlock("checked by eye on the phone page")
                r = DRY.public()
            else:
                raise ValueError("unknown drying action")
            return self.send(200, r)
        m = re.fullmatch(r"/drying/(bedcheck-\d{8}-\d{6}-(?:phone|printer)\.jpg)", p)
        if m:
            f = os.path.join(DRY.dir, m.group(1))
            return self.send(200, open(f, "rb").read(), "image/jpeg") if os.path.exists(f) else self.send(404, {"error": "not there"})
        if p == "/api/printing/settings":  # the settings in the file the printer is running now
            s = summary()
            job = job_for_file(s.get("file")) if s["online"] else None
            if not job:
                return self.send(404, {"error": f"This print was not sent from {NAME}, so its file is not here."
                                       if s.get("file") else "Nothing printing."})
            return self.send(200, job_settings(job))
        if p == "/api/raw":
            return self.send(200, {"detail": STATE["detail"], "error": STATE["error"]})
        if p in ("/cam.jpg", "/webcam.jpg") or p in ("/cam.mjpg", "/webcam.mjpg"):
            cam = WEBCAM if p.startswith("/webcam") else CAM
            if not cam:
                return self.send(404, {"error": "no webcam set"})
            if p.endswith(".mjpg"):
                return self.stream(cam)
            f = cam.snapshot(cam.wake)
            return self.send(200, f, "image/jpeg") if f else self.send(503, {"error": "camera not answering"})
        m = re.fullmatch(r"/static/(.+)", p)
        if m and method == "GET":  # the 3D viewer's scripts and the vendored three.js (and its licence)
            rel, root = unquote(m.group(1)), os.path.realpath(os.path.join(HERE, "static"))
            if (".." in rel or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*", rel)
                    or not (rel.endswith(".js") or rel.endswith("/LICENSE"))):
                raise ValueError("bad file name")
            f = os.path.realpath(os.path.join(root, rel))
            if not f.startswith(root + os.sep) or not os.path.isfile(f):
                return self.send(404, {"error": "not there"})
            return self.send_file(f, "text/javascript; charset=utf-8" if rel.endswith(".js") else "text/plain",
                                  "private, max-age=604800" if rel.startswith("vendor/") else "private, no-cache",
                                  "make")
        m = re.fullmatch(r"/api/jobs/([^/]+)/stl/(.*)", p)
        if m and method == "GET":  # one STL part of a job, as uploaded; only files listed in that job's parts
            jid, name = m.group(1), unquote(m.group(2))
            f = job_file(jid, name)
            if not name.lower().endswith(".stl") or name not in {x.get("file") for x in load_job(jid).get("parts") or []} \
                    or not os.path.isfile(f):
                return self.send(404, {"error": "not there"})
            return self.send_file(f, "model/stl", "private, max-age=86400")
        if p == "/api/jobs" and method == "GET":
            return self.send(200, list_jobs())
        if p == "/api/upload" and method == "POST":
            fname = os.path.basename(q.get("name", "upload.stl")).replace("..", "")
            with JOB_LOCK:
                job = load_job(q["job"]) if q.get("job") else new_job(
                    q.get("title") or os.path.splitext(fname)[0], q.get("source", "phone"))
            data = self.body()
            stored = f"{len(os.listdir(job_dir(job['id']))):02d}-{re.sub(r'[^A-Za-z0-9._-]+', '_', fname)}"
            open(os.path.join(job_dir(job["id"]), stored), "wb").write(data)
            with JOB_LOCK:
                job = load_job(job["id"])
                add_file(job, fname, os.path.join(job_dir(job["id"]), stored))
            log(f"upload {job['id']} {fname} {len(data)} bytes from {q.get('source', 'phone')}")
            return self.send(200, job)
        m = re.fullmatch(r"/api/jobs/([0-9a-f-]+)(?:/(\w+(?:\.\w+)?))?", p)
        if m:
            jid, act = m.groups()
            d = job_dir(jid)
            if method == "GET" and act in ("preview.png", "verify.txt", "slice.log"):
                f = os.path.join(d, act)
                if not os.path.exists(f):
                    return self.send(404, {"error": "not there"})
                ctype = "image/png" if act.endswith(".png") else "text/plain; charset=utf-8"
                return self.send(200, open(f, "rb").read(), ctype)
            if method == "GET" and act is None:
                return self.send(200, load_job(jid))
            if method == "GET" and act == "model":  # what the 3D viewer can show for this job
                return self.send(200, model_info(load_job(jid)))
            if method == "GET" and act == "toolpath":  # 202 + progress while it builds, then the gzipped toolpath
                job = load_job(jid)
                if not job.get("sliced_file") or not os.path.isfile(os.path.join(d, job["sliced_file"])):
                    return self.send(404, {"error": "no 3D model for this job"})
                if toolpath_ready(job):
                    return self.send_file(toolpath_out(jid), "application/octet-stream", "private, no-cache", "stored")
                with TP_LOCK:
                    st = TP.get(jid)
                    if st and "error" in st:
                        TP.pop(jid)  # the next tap tries again
                        return self.send(500, {"error": st["error"]})
                    if not st:
                        st = TP[jid] = {"progress": 0}
                        threading.Thread(target=build_toolpath, args=(jid, os.path.join(d, job["sliced_file"])),
                                         daemon=True).start()
                return self.send(202, {"building": True, "progress": st.get("progress", 0)})
            if method == "GET" and act == "precheck":  # what the send sheet must show before Start print
                job = backfill(load_job(jid))
                s = summary()
                return self.send(200, {"check": job.get("check"),
                                       "problems": material_problems(job, s) if s["online"] else [],
                                       "notes": (colour_notes(job, s) if s["online"] else [])
                                                + [n for n in [z_offset(job)[1]] if n]})
            if method == "POST" and act == "update":
                req = json.loads(self.body(100000))
                with JOB_LOCK:
                    job = load_job(jid)
                    if job["state"] == "slicing":
                        raise ValueError("slicing, wait")
                    if "name" in req:
                        job["name"] = str(req["name"])[:60]
                    if "layer" in req and req["layer"] in ("0.12", "0.16", "0.2", "0.24"):
                        job["layer"] = req["layer"]
                    if "detail" in req:
                        job["detail"] = bool(req["detail"])
                    for i, t in (req.get("tools") or {}).items():
                        if int(t) in (1, 2, 3, 4):
                            job["parts"][int(i)]["tool"] = int(t)
                    if job.get("kind") == "model":
                        job.update(state="new", sliced_file=None, check=None, stats=None)
                    save_job(job)
                return self.send(200, job)
            if method == "POST" and act == "slice":
                job = load_job(jid)
                if job.get("kind") != "model" or not job["parts"]:
                    raise ValueError("nothing to slice")
                threading.Thread(target=slice_job, args=(jid,), daemon=True).start()
                job["state"] = "queued"
                return self.send(200, job)
            if method == "POST" and act == "print":
                req = json.loads(self.body(10000) or b"{}")
                job = send_job(jid, bool(req.get("leveling", CFG["leveling"])), bool(req.get("plate_clear")),
                               bool(req.get("allow_unchecked")))
                return self.send(200, job)
            if method == "POST" and act == "delete":
                shutil.rmtree(d, ignore_errors=True)
                return self.send(200, {"ok": True})
        if p == "/api/adjust" and method == "POST":
            req = json.loads(self.body(10000) or b"{}")
            return self.send(200, adjust(str(req.get("item", "")), req.get("value")))
        if p == "/api/control" and method == "POST":
            a = json.loads(self.body(10000)).get("action")
            fn = {"pause": lambda: PR.job("pause"), "resume": lambda: PR.job("continue"),
                  "cancel": lambda: PR.job("cancel"), "light_on": lambda: PR.light(True),
                  "light_off": lambda: PR.light(False), "clear_plate": PR.clear_platform}.get(a)
            if not fn:
                raise ValueError("unknown action")
            r = fn()
            LAST_CONTROL[a] = time.time()
            log(f"control {a}: {r}")
            return self.send(200 if PR.ok(r) else 409, {"ok": PR.ok(r), "reply": r})
        return self.send(404, {"error": "not found"})

    def stream(self, cam):
        cam.acquire()
        try:
            asked = time.time()  # 503 before any headers when no frame comes, so the page can fall back to the other camera
            f, t = cam.wait_frame(asked - 5, cam.wake)
            if not f or t <= asked - 5:
                return self.send(503, {"error": "camera not answering"})
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            last = seen = time.time() - 5
            while time.time() - seen < 30:  # give up if the camera stops sending
                f, t = cam.wait_frame(last, 10)
                if t <= last or not f:
                    continue
                seen = time.time()
                last = t
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(f)).encode() + b"\r\n\r\n" + f + b"\r\n")
                self.wfile.flush()
        finally:
            cam.release()
            self.close_connection = True


# ---------- first-run setup ----------
# The setup page (static/setup.html) walks through: find the printer, serial + check code, PIN, cameras, your own
# Flash Studio export as the slicing template, and the optional watcher. Before setup is done it opens without a PIN,
# from this PC only; after that it sits behind the PIN like every other page.

NOT_SET_UP = (f"<!doctype html><meta name=viewport content='width=device-width'><title>{NAME}</title>"
              "<body style='font-family:sans-serif;background:#14161a;color:#eee;padding:16px'>"
              f"<h2>{NAME} is not set up yet</h2><p>Finish setup on the PC that runs the hub: open "
              "<b>http://127.0.0.1:%d/setup</b> in a browser there.</p>" % CFG["port"])
SCAN = {"running": False, "found": [], "done": 0, "total": 0}
FS_SLICES = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp", "orca-flashforge", "slice")


def apply_printer():
    PR.ip, PR.serial, PR.check = CFG["printer_ip"], CFG["serial"], CFG["check_code"]
    CAM.url = f"http://{CFG['printer_ip']}:8080/?action=stream"
    STATE.update(error="checking the printer", detail=None)


def apply_webcam():
    """A changed phone camera address takes effect at once, for the page, the watcher and the drying check."""
    global WEBCAM
    WEBCAM = (printer.Camera(url=webcam_stream(CFG["webcam_url"]), fps=CFG["webcam_fps"], wake=8)
              if CFG["webcam_url"] else None)
    WATCH.cams = ([("phone", WEBCAM, 8)] if WEBCAM else []) + [("printer", CAM, 20)]
    DRY.cams = list(WATCH.cams)


def pc_ips():
    """This PC's own private IPv4 addresses (no loopback)."""
    ips = set()
    try:
        ips |= {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        pass
    try:  # the address of the interface with the default route; connect() on UDP sends nothing
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    good = lambda a: a.is_private and not a.is_loopback and not a.is_link_local
    return sorted(ip for ip in ips if good(ipaddress.ip_address(ip)))


def local_networks():
    """The /24 networks this PC sits on."""
    return sorted({str(ipaddress.ip_network(f"{ip}/24", strict=False)) for ip in pc_ips()})


def probe(ip):
    """A printer answers its LAN API on :8898. Returns what it said, or None."""
    try:
        with socket.create_connection((ip, 8898), timeout=0.6):
            pass
    except OSError:
        return None
    try:
        r = printer.Printer(ip).detail()
    except Exception as e:
        r = {"message": str(e)[:100]}
    d = r.get("detail") or {}
    return {"ip": ip, "name": d.get("name") or d.get("machineName") or "", "reply": r.get("message") or ""}


def scan(nets):
    hosts = [str(h) for n in nets for h in ipaddress.ip_network(n).hosts()][:1024]
    SCAN.update(running=True, found=[], done=0, total=len(hosts))
    try:
        with ThreadPoolExecutor(64) as ex:
            for r in ex.map(probe, hosts):
                SCAN["done"] += 1
                if r:
                    SCAN["found"].append(r)
    finally:
        SCAN["running"] = False
    log(f"setup: scanned {nets}, printers answering on 8898: {[f['ip'] for f in SCAN['found']]}")


def private_url(url):
    """Camera addresses must be http on this network: the setup page never fetches the internet for you."""
    u = urlparse(url or "")
    if u.scheme != "http" or not u.hostname:
        raise ValueError("camera address must start with http:// and the phone's address")
    try:
        a = ipaddress.ip_address(socket.gethostbyname(u.hostname))
    except (OSError, ValueError):
        raise ValueError(f"cannot find {u.hostname} on this network")
    if not (a.is_private or a.is_loopback):
        raise ValueError("camera address must be on your own network")
    return url


def template_info(path):
    with zipfile.ZipFile(path) as z:
        cfg = json.loads(z.read("Metadata/project_settings.config"))
        head = z.read("Metadata/plate_1.gcode")[:400].decode(errors="ignore")
    gen = re.search(r"generated by (.+)", head)
    return {"file": os.path.basename(path), "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path))),
            "printer": cfg.get("printer_settings_id"), "process": cfg.get("print_settings_id"),
            "filaments": cfg.get("filament_settings_id") or [], "slicer": gen.group(1).strip() if gen else None,
            "problems": slicer.check_template(path)}


def setup_state():
    s = summary()
    tpl = None
    if slicer.template_ready():
        try:
            tpl = template_info(slicer.TEMPLATE)
        except Exception as e:
            tpl = {"problems": [str(e)]}
    key = CFG.get("anthropic_api_key") or ""
    return {"name": NAME, "setup_done": bool(CFG.get("setup_done")), "local": True,
            "printer": {"ip": CFG["printer_ip"], "serial_set": bool(CFG["serial"]), "check_set": bool(CFG["check_code"]),
                        "online": s["online"], "status": s.get("status"), "error": s.get("error")},
            "pin_set": bool(CFG.get("pin_chosen")), "port": CFG["port"], "host": CFG["host"],
            "lan_urls": [f"http://{ip}:{CFG['port']}" for ip in pc_ips()],
            "webcam_url": CFG["webcam_url"],
            "flash_studio": {"path": slicer.FLASH_STUDIO, "found": os.path.isfile(slicer.FLASH_STUDIO)},
            "template": tpl, "fs_slices": os.path.isdir(FS_SLICES),
            "watcher": {"backend": watcher.backend(CFG), "chosen": CFG.get("watch_backend") or "",
                        "key_end": key[-4:] if key else "", "enabled": WATCH.state["enabled"],
                        "claude_cli": bool(shutil.which("claude") or os.path.exists(WATCH.claude or ""))},
            "scan": SCAN}


def setup_route(h, method, p, q):
    req = json.loads(h.body(20000) or b"{}") if method == "POST" and p != "/api/setup/template/upload" else {}
    if p == "/api/setup" and method == "GET":
        return h.send(200, setup_state())
    if p == "/api/setup/scan" and method == "POST":
        nets = [req["network"]] if req.get("network") else local_networks()
        for n in nets:
            if not ipaddress.ip_network(n, strict=False).is_private or ipaddress.ip_network(n, strict=False).num_addresses > 1024:
                raise ValueError("scan only your own network, /22 or smaller")
        if not SCAN["running"]:
            threading.Thread(target=scan, args=([str(ipaddress.ip_network(n, strict=False)) for n in nets],), daemon=True).start()
            SCAN["running"] = True
        return h.send(200, {"networks": nets, **SCAN})
    if p == "/api/setup/printer" and method == "POST":
        ip = (req.get("ip") or "").strip()
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            raise ValueError("printer address looks like 192.168.1.50")
        serial, check = (req.get("serial") or "").strip(), (req.get("check_code") or "").strip()
        if not serial or not check:
            raise ValueError("type the serial number and the check code from the printer screen")
        try:
            r = printer.Printer(ip, serial, check).detail()
        except Exception as e:
            raise ValueError(f"no answer from {ip}:8898 ({e}). Is the printer on and on this network?")
        if r.get("code") != 0:
            msg = r.get("message") or str(r)
            hint = (" Turn on LAN Only mode on the printer screen first." if "lan" in msg.lower() else
                    " Check the serial number and check code against the printer screen.")
            raise ValueError(f"the printer said: {msg}.{hint}")
        save_cfg(printer_ip=ip, serial=serial, check_code=check)
        apply_printer()
        d = r.get("detail") or {}
        log(f"setup: printer {ip} answers, status {d.get('status')}")
        return h.send(200, {"ok": True, "status": d.get("status"), "name": d.get("name")})
    if p == "/api/setup/pin" and method == "POST":
        pin = str(req.get("pin") or "").strip()
        if not re.fullmatch(r"\d{4,8}", pin):
            raise ValueError("PIN must be 4 to 8 digits")
        save_cfg(password=pin, pin_chosen=True, secret=secrets.token_hex(16))  # new secret: old logins end
        log("setup: PIN changed")
        return h.send(200, {"ok": True}, extra={
            "Set-Cookie": f"hub={token()}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict"})
    if p == "/api/setup/camera.jpg" and method == "GET":  # live preview for the camera step
        if q.get("which") == "printer":
            cam = CAM
        else:
            cam = printer.Camera(url=webcam_stream(private_url(q.get("url"))), wake=8)
        f = cam.snapshot(cam.wake if q.get("which") != "printer" else 20)
        return h.send(200, f, "image/jpeg") if f else h.send(503, {"error": "camera not answering"})
    if p == "/api/setup/camera" and method == "POST":
        url = (req.get("url") or "").strip()
        if url:
            private_url(url)
            f = printer.Camera(url=webcam_stream(url), wake=8).snapshot(8)
            if not f:
                raise ValueError(f"no picture from {webcam_stream(url)} in 8 s. Is the camera app running, with the "
                                 "phone on the same Wi-Fi as this PC? Check the address the app shows.")
        save_cfg(webcam_url=url)
        apply_webcam()
        log(f"setup: phone camera {'set' if url else 'off'}")
        return h.send(200, {"ok": True, "stream": webcam_stream(url) if url else None})
    if p == "/api/setup/template/latest" and method == "GET":  # newest plate Flash Studio sliced on this PC
        files = sorted(glob.glob(os.path.join(FS_SLICES, "*.3mf")), key=os.path.getmtime, reverse=True)
        out = []
        for f in files[:5]:
            try:
                out.append(template_info(f))
            except Exception as e:
                out.append({"file": os.path.basename(f), "problems": [f"unreadable: {e}"]})
        return h.send(200, {"folder": FS_SLICES, "files": out})
    if p in ("/api/setup/template/use", "/api/setup/template/upload") and method == "POST":
        if p.endswith("/use"):
            name = os.path.basename(req.get("file") or "")
            src = os.path.join(FS_SLICES, name)
            if not name.lower().endswith(".3mf") or not os.path.isfile(src):
                raise ValueError("that Flash Studio file is gone - slice again in Flash Studio and look again")
            presets = bool(req.get("only_these_presets"))
        else:
            src = os.path.join(HERE, "template", "upload.3mf.part")
            os.makedirs(os.path.dirname(src), exist_ok=True)
            open(src, "wb").write(h.body(200_000_000))
            presets = q.get("only_these_presets") == "1"
        problems = slicer.check_template(src)
        if problems:
            raise ValueError("this file cannot be the template: " + "; ".join(problems))
        dst = slicer.TEMPLATE
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):  # keep the one it replaces
            shutil.copy(dst, os.path.join(os.path.dirname(dst), time.strftime("template-before-%Y%m%d-%H%M%S.3mf")))
        shutil.copy(src, dst + ".part")
        os.replace(dst + ".part", dst)
        if src.endswith(".part"):
            os.remove(src)
        info = template_info(dst)
        if presets:
            save_cfg(house_presets=sorted(set(info["filaments"])))
        log(f"setup: new template {info['filaments']} ({info['slicer']})")
        return h.send(200, info)
    if p == "/api/setup/watcher" and method == "POST":
        b = req.get("backend") or ""
        if b not in ("", "api", "cli"):
            raise ValueError("unknown watcher choice")
        ch = {"watch_backend": b}
        if b == "api":
            key = (req.get("api_key") or "").strip()
            if key and not key.startswith("sk-ant-"):
                raise ValueError("an Anthropic API key starts with sk-ant-")
            if key:
                ch["anthropic_api_key"] = key
            elif not CFG.get("anthropic_api_key"):
                raise ValueError("paste your Anthropic API key")
        if req.get("forget_key"):
            ch["anthropic_api_key"] = ""
        save_cfg(**ch)
        on = bool(req.get("enabled")) and bool(watcher.backend(CFG))
        WATCH.state["enabled"] = on
        save_cfg(watch=on)
        log(f"setup: watcher backend '{watcher.backend(CFG)}', {'on' if on else 'off'}")
        return h.send(200, setup_state()["watcher"])
    if p == "/api/setup/watcher/test" and method == "POST":  # one real check on a printer camera frame
        if not watcher.backend(CFG):
            raise ValueError("choose the API key or Claude Code first and save")
        f = CAM.snapshot(20)
        if not f:
            raise ValueError("the printer camera is not answering, so there is no frame to test with")
        img = os.path.join(WATCH.dir, "setup-test.jpg")
        open(img, "wb").write(f)
        v = WATCH.ask(img, "Print context: setup test, the plate may be empty.", CFG.get("watch_model", "haiku"), "printer")
        return h.send(200, v)
    if p == "/api/setup/done" and method == "POST":
        if not CFG.get("pin_chosen"):
            raise ValueError("choose your PIN first")
        save_cfg(setup_done=True)
        log("setup: done")
        return h.send(200, {"ok": True}, extra={
            "Set-Cookie": f"hub={token()}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict"})
    return h.send(404, {"error": "not found"})


if __name__ == "__main__":
    threading.Thread(target=poll_status, daemon=True).start()
    threading.Thread(target=WATCH.run, daemon=True).start()
    threading.Thread(target=push_events, daemon=True).start()
    threading.Thread(target=DRY.run, daemon=True).start()
    srv = ThreadingHTTPServer((CFG["host"], CFG["port"]), H)
    srv.daemon_threads = True
    log(f"{NAME} on port {CFG['port']}, printer {CFG['printer_ip'] or 'not set up'}"
        + ("" if CFG.get("setup_done") else f" - finish setup at http://127.0.0.1:{CFG['port']}/setup"))
    srv.serve_forever()
