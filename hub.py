# C5 Print Hub: phone page for the Flashforge Creator 5 Pro (unofficial). Upload, slice, send, watch, pause/cancel.
# Run: python -I hub.py   (config.json next to this file; jobs/ holds every upload and slice)
# First run: setup.ps1 opens the one-time setup link (also in setup-url.txt next to this file) on this PC.
import bisect, glob, gzip, hashlib, hmac, ipaddress, json, math, os, re, secrets, shutil, socket, ssl, sys, threading, \
    time, traceback, urllib.request, zipfile
import socketserver
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dryer, printer, security, slicer, toolpath, watcher

CFG_PATH = os.path.join(HERE, "config.json")
JOBS = os.path.join(HERE, "jobs")
NAME = "C5 Print Hub"
# host 0.0.0.0: phones on your own Wi-Fi can open the page. This plain-http port answers home-network addresses only
# (and Tailscale's, with "tailscale": true); never forward it on your router. Away from home use Tailscale, a
# Cloudflare Tunnel to remote_tunnel_port, or the HTTPS port (remote_https_port + your own certificate): see README.
DEFAULTS = {"setup_done": False, "printer_ip": "", "serial": "", "check_code": "", "port": 8765, "host": "0.0.0.0",
            "tailscale": False, "lan_hosts": [],
            # Remote access (off until set): both ports need a passphrase (12+ characters) and an authenticator code.
            "remote_tunnel_port": 0, "remote_https_port": 0, "remote_https_host": "0.0.0.0", "tls_cert": "",
            "tls_key": "", "remote_hosts": [],
            # Limits no button overrides (can only be set lower): see slicer.HARD_CAPS.
            "hard_caps": {},
            "camera_allow_loopback": False,  # a camera served by this PC itself (the emulator uses this)
            "http_timeout_s": 20, "http_header_timeout_s": 30, "http_max_conns": 48, "http_max_conns_per_ip": 20,
            "bed_cleared_at": 0, "bed_cleared_by": "", "last_print_end": 0,
            "colours": ["#FFFFFF", "#1E5BD8", "#F5D000", "#FF69B4"], "leveling": False,
            # Phone webcam (an IP camera app), e.g. "http://192.168.1.50:8080"; "" = printer camera only.
            "webcam_url": "", "webcam_fps": 8,
            # Failure watcher: off until set up. watch_backend "api" (your own Anthropic API key), "cli" (Claude Code)
            # or "openai" (any OpenAI-compatible server: experimental).
            "watch": False, "watch_backend": "", "anthropic_api_key": "", "watch_every_min": 2, "watch_action": "pause",
            "openai_preset": "", "openai_base_url": "", "openai_api_key": "", "openai_model": "", "openai_strong_model": "",
            "openai_timeout_s": 120,
            "ntfy_topic": "", "ntfy_server": "https://ntfy.sh", "ntfy_token": "",
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
    # Older versions kept the PIN in plain text and signed one shared cookie with "secret": hash the PIN and drop
    # both (every phone logs in once more). A PIN the old version made up before setup is dropped, never kept.
    if cfg.get("password") and cfg.get("pin_chosen") and not cfg.get("pin_hash"):
        cfg["pin_hash"] = security.hash_secret(str(cfg["password"]))
    cfg.pop("password", None)
    cfg.pop("secret", None)
    if not cfg.get("api_token"):  # hubctl.py on this PC only (X-Hub-Key from 127.0.0.1), never the PIN
        cfg["api_token"] = secrets.token_urlsafe(32)
    json.dump(cfg, open(CFG_PATH + ".tmp", "w", encoding="utf-8"), indent=1)
    os.replace(CFG_PATH + ".tmp", CFG_PATH)
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


def cam_guard(url):
    """A typed camera address is fetched only on the home network, resolved again at every connect."""
    return security.pinned(url, bool(CFG.get("camera_allow_loopback")))


def phone_camera(url, fps=0):
    return printer.Camera(url=webcam_stream(url), fps=fps, wake=8, guard=cam_guard)


WEBCAM = phone_camera(CFG["webcam_url"], CFG["webcam_fps"]) if CFG["webcam_url"] else None
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
                was = ((STATE["detail"] or {}).get("status") or "").lower()
                if was in ACTIVE and (r["detail"].get("status") or "").lower() in ("completed", "cancel"):
                    # the moment the hub first sees a print end: a 'bed cleared' mark from before it no longer counts
                    save_cfg(last_print_end=time.time())
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
        # Whatever answers as the printer is not trusted with the page: colours must be #RRGGBB, numbers numbers.
        slots.append({"slot": num(s.get("slotId")), "material": text(s.get("materialName"), 40),
                      "colour": colour(s.get("materialColor")), "loaded": s.get("hasFilament") if isinstance(s.get("hasFilament"), bool) else None})
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
    return {"online": True, "status": text(d.get("status"), 30), "file": text(d.get("printFileName"), 200),
            "progress": round(prog, 1), "layer": num(d.get("printLayer")),
            "layers": num(d.get("targetPrintLayer")),
            # the file's own layer and height at the printer's place in the file (its printLayer bounces)
            "file_layer": where[0] if where else None, "z": where[1] if where else None,
            "z_top": where[2] if where else None, "file_layers": where[3] if where else None, "left_s": left, "eta_from": eta_from,
            "printer_left_s": num(d.get("estimatedTime")), "speed": num(d.get("printSpeedAdjust")),
            "elapsed_s": num(d.get("printDuration")),
            "bed": round(float(d.get("platTemp") or 0)), "bed_target": round(float(d.get("platTargetTemp") or 0)),
            "chamber_target": round(float(d.get("chamberTargetTemp") or 0)),
            # chamberTemp -108 means no chamber sensor (ff-5mp-api-py changelog)
            "chamber": round(float(d.get("chamberTemp") or 0)) if float(d.get("chamberTemp") or 0) > -50 else None,
            "tools": tools, "slots": slots,
            "light": text(d.get("lightStatus"), 10), "door": text(d.get("doorStatus"), 10),
            "error_code": text(d.get("errorCode"), 12), "thumb": bool(d.get("printFileThumbUrl"))}


def num(v):
    try:
        return None if v is None else round(float(v)) if float(v) == int(float(v)) else round(float(v), 2)
    except (TypeError, ValueError, OverflowError):
        return None


def text(v, n):
    return None if v is None else str(v)[:n]


def colour(v):
    return v if isinstance(v, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", v) else None


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


def push(msg, priority="high"):
    """A phone push without holding up the reply (ntfy can take seconds to answer)."""
    threading.Thread(target=WATCH.notify, args=(msg, priority), daemon=True).start()


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
    return WATCH.ask(img, "", CFG.get("dry_check_model", "sonnet"), view, system=prompt, strong=True)


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
        # The "generated by" line is only information: anyone can write it. The hard check runs on every file.
        named = (job["stats"].get("slicer") or "").startswith("Flash Studio")
        job["hard"] = slicer.hard_check_file(path, CFG.get("hard_caps"))
        job["sanity"] = slicer.sanity(text, CFG["material_ranges"]) + slicer.preset_check(text, CFG.get("house_presets"))
        reasons = [hard_reason(r) for r in job["hard"]]
        reasons += ([] if named else [f"sliced by {job['stats'].get('slicer') or 'an unknown slicer'}, not Flash Studio"])
        reasons += job["sanity"]
        job["check"] = {"passed": not reasons, "reasons": reasons}
        if job["hard"]:
            job["state"] = "blocked"
        slicer.preview_png(text, pad_colours(job["stats"]["colours"]), os.path.join(job_dir(job["id"]), "preview.png"))
        job["design"] = {"state": "checking"}
    else:
        raise ValueError("upload .stl, .gcode or a sliced .3mf")
    save_job(job)
    if job.get("design") == {"state": "checking"}:
        threading.Thread(target=design_job, args=(job["id"],), daemon=True).start()


def hard_reason(r):
    return f"NEVER SENT (not even with Send anyway): {r}"


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
                hard = slicer.hard_check(text, CFG.get("hard_caps"))
                reasons = [hard_reason(x) for x in hard] + r["reasons"] + slicer.preset_check(text, CFG.get("house_presets"))
                job.update(state="blocked" if reasons else "ready", sliced_file=out, stats=slicer.stats(text), hard=hard,
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
    if "materials" in job["stats"] and "hard" in job and not (job.get("kind") == "sliced" and "sanity" not in job):
        return job
    path = os.path.join(job_dir(job["id"]), job["sliced_file"])
    text = slicer.read_gcode(path)
    with JOB_LOCK:
        job = load_job(job["id"])
        job["stats"]["materials"] = slicer.materials(text)
        if "hard" not in job:  # jobs from before the hard check
            job["hard"] = slicer.hard_check_file(path, CFG.get("hard_caps"))
            if job["hard"]:
                c = job.get("check") or {"reasons": []}
                job["check"] = {"passed": False, "reasons": [hard_reason(r) for r in job["hard"]] + c.get("reasons", [])}
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


class NeedConfirm(ValueError):
    """A send that needs one more yes from the phone (409, "confirm": what to ask)."""
    def __init__(self, msg, what):
        super().__init__(msg)
        self.what = what


def bed_state(s=None):
    """The optional 'bed cleared' mark: when it was made, and whether a print has ended since."""
    s = s or summary()
    at, end = float(CFG.get("bed_cleared_at") or 0), float(CFG.get("last_print_end") or 0)
    idle_after_print = (s.get("status") or "").lower() in ("completed", "cancel")
    return {"cleared_at": at or None, "cleared_by": CFG.get("bed_cleared_by") or None, "last_print_end": end or None,
            "needs_mark": at <= end if end else (idle_after_print and not at)}


def send_job(jid, leveling, plate_clear, allow_unchecked=False, remote=None, no_mark_ok=False):
    """remote: the device name when the request came in on a remote port (an extra confirm when the bed was not
    marked cleared since the last print, and a push to the phone)."""
    if DRY.refuse():
        raise ValueError(DRY.refuse())
    job = load_job(jid)
    if job.get("sliced_file") and job.get("stats"):
        job = backfill(job)
    if job.get("hard"):  # temperature over the hardware cap or a firmware-level command: no override
        raise ValueError("this file is never sent: " + "; ".join(job["hard"]))
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
    if remote and bed_state(s)["needs_mark"] and not no_mark_ok:
        raise NeedConfirm("The bed was not marked cleared since the last print ended. Look at the camera frame: "
                          "is the bed really clear?", "bed")
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
                   z_offset=dz, sent_from=remote or "home network")
        save_job(job)
    if remote:
        push(f"Print started remotely from {remote}: {job['name']}"
                     + ("" if bed_state(s)["cleared_at"] and not bed_state(s)["needs_mark"] else " (bed not marked cleared)"))
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
# Three kinds of door, decided only by the port a request came in on, never by its address or headers:
#   lan     the plain-http port (CFG port): home-network and this-PC addresses only, PIN login, 30-day sessions.
#   remote  remote_tunnel_port (127.0.0.1 only, for cloudflared) and remote_https_port (TLS with your own
#           certificate): passphrase + authenticator code, 7-day sessions, Secure cookies, a push on every new device.
# Every door: Host allowlist, JSON or file bodies only, Origin check on every POST, bounded connections and time.

SESS = security.Sessions(os.path.join(HERE, "sessions.json"), lambda m: log(m))
LOGIN_LOCK = threading.Lock()  # one secret check at a time (each costs ~50 ms of scrypt plus a fixed wait)
UPLOADS = threading.BoundedSemaphore(2)
INCOMING = os.path.join(JOBS, ".incoming")
SETUP_TOKEN = secrets.token_urlsafe(24) if not CFG.get("setup_done") else None
SETUP_URL_FILE = os.path.join(HERE, "setup-url.txt")
PENDING_TOTP = {}  # session id -> authenticator secret being set up, until its first code is typed
COOKIE = {"lan": "hub", "remote": "__Host-hub"}


def on_lock(kind, n):
    if kind == "remote":
        save_cfg(remote_locked=True)
        msg = f"Remote login LOCKED after {n} wrong tries in an hour. Log in once on your home network to open it again."
    else:
        msg = f"Home-network login paused for 15 min after {n} wrong tries in an hour."
    log("SECURITY " + msg)
    push(msg)


LIMIT = security.Limiter(on_lock, remote_locked=bool(CFG.get("remote_locked")))


def remote_ready():
    return bool(CFG.get("remote_pass_hash") and CFG.get("totp_secret"))


def lan_names():
    h = socket.gethostname().lower()
    return ({"localhost"} | {h + x for x in ("", ".local", ".lan", ".home", ".home.arpa", ".localdomain", ".internal")}
            | {str(x).lower().rstrip(".") for x in CFG.get("lan_hosts") or []})


def remote_names():
    return {"localhost"} | {str(x).lower().rstrip(".") for x in CFG.get("remote_hosts") or []}


class Refused(Exception):
    def __init__(self, code, msg, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


class StepUp(Refused):
    def __init__(self, msg):
        super().__init__(403, msg, step_up=True, what="passphrase" if CFG.get("remote_pass_hash") else "PIN")


class HubServer(ThreadingHTTPServer):
    """http.server with a connection cap (in all and per address), socket timeouts, a deadline for the request line
    and headers (slow-loris), and optional TLS. kind: 'lan' or 'remote'."""
    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = False  # Windows: SO_REUSEADDR would let a second program take over the port

    def __init__(self, addr, kind, tls=None, secure=False, per_ip=True):
        self.kind, self.tls, self.secure, self.per_ip = kind, tls, secure or bool(tls), per_ip
        self.cap = threading.BoundedSemaphore(int(CFG.get("http_max_conns") or 48))
        self.by_ip, self.heads, self.lock = {}, {}, threading.Lock()
        super().__init__(addr, H)

    def server_bind(self):
        # http.server looks up the address's full DNS name here, which can take seconds on Windows; not needed
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def get_request(self):
        sock, addr = super().get_request()
        if self.tls:  # the handshake runs in the handler thread, under its timeout, not in the accept loop
            sock = self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, addr

    def process_request(self, request, client_address):
        ip = client_address[0]
        with self.lock:
            busy = self.per_ip and self.by_ip.get(ip, 0) >= int(CFG.get("http_max_conns_per_ip") or 20)
            if not busy and self.cap.acquire(blocking=False):
                self.by_ip[ip] = self.by_ip.get(ip, 0) + 1
            else:
                busy = True
        if busy:  # full: say so at once and hang up, so nobody waits on a queue that never moves
            try:
                if not self.tls:
                    request.settimeout(2)
                    request.sendall(b"HTTP/1.1 503 Busy\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._done(ip)
            raise

    def _done(self, ip):
        with self.lock:
            self.by_ip[ip] = self.by_ip.get(ip, 1) - 1
            if self.by_ip[ip] <= 0:
                self.by_ip.pop(ip, None)
        self.cap.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.heads.pop(request, None)
            self._done(client_address[0])

    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        if not isinstance(e, (OSError, ssl.SSLError, ConnectionError)):
            log(f"http error from {client_address[0]}: {traceback.format_exc()}")

    def reap(self):
        """Hang up on connections still sending their request line or headers after http_header_timeout_s."""
        limit = float(CFG.get("http_header_timeout_s") or 30)
        for sock, t in list(self.heads.items()):
            if time.time() - t > limit:
                self.heads.pop(sock, None)
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


SERVERS = []


def reaper():
    while True:
        time.sleep(1)
        for s in list(SERVERS):
            s.reap()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "C5PrintHub"
    sys_version = ""

    def log_message(self, *a):
        pass

    def setup(self):
        self.server.heads[self.request] = time.time()  # the TLS handshake counts against the header deadline too
        self.timeout = float(CFG.get("http_timeout_s") or 20)
        super().setup()
        if self.server.tls:
            self.connection.do_handshake()

    def handle_one_request(self):
        self.server.heads.setdefault(self.request, time.time())  # until the headers are in (parse_request)
        self.session = self.token = None
        self.cookie_out = []
        super().handle_one_request()

    def parse_request(self):
        ok = super().parse_request()
        self.server.heads.pop(self.request, None)
        return ok

    def end_headers(self):
        # Security headers on every reply. The pages still use inline script, so the CSP allows it but nothing
        # from anywhere else: no other site can load, frame or receive anything from these pages.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; "
                         "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        if self.server.secure:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for c in self.cookie_out:
            self.send_header("Set-Cookie", c)
        self.cookie_out = []
        super().end_headers()

    # ---- who is asking ----
    def ip(self):
        return self.client_address[0]

    def cookie(self, name):
        for c in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = c.strip().partition("=")
            if k == name:
                return v
        return None

    def set_session_cookie(self, token, max_age=None):
        kind = self.server.kind
        age = security.TTL[kind] if max_age is None else max_age
        flags = "; Secure" if self.server.secure else ""
        same = "Lax" if kind == "remote" else "Strict"  # Lax: the first load after a Cloudflare Access sign-in
        self.cookie_out.append(f"{COOKIE[kind]}={token}; Max-Age={age}; Path=/; HttpOnly; SameSite={same}{flags}")

    def device(self):
        if self.session == "api":
            return "this PC (hubctl)"
        return (self.session or {}).get("device") if isinstance(self.session, dict) else None

    def authed(self):
        """The session behind this request (a dict), 'api' for hubctl's token from this PC, or None."""
        if self.session:
            return self.session
        key = self.headers.get("X-Hub-Key")
        if key is not None:  # only on the home port, only from this PC, never through a proxy, never the PIN
            if (self.server.kind == "lan" and ipaddress.ip_address(security.ip_of(self.ip())).is_loopback
                    and not security.proxied(self.headers) and CFG.get("api_token")
                    and hmac.compare_digest(key.encode(), CFG["api_token"].encode())):
                self.session = "api"
                return self.session
            return None
        tok = self.cookie(COOKIE[self.server.kind])
        rec = SESS.get(tok, self.server.kind) if tok else None
        if rec:
            self.session, self.token = rec, tok
            if SESS.touch_cookie(tok):
                self.set_session_cookie(tok)
        return self.session

    def setup_cookie_ok(self):
        v = self.cookie("hubsetup")
        return bool(SETUP_TOKEN and v and hmac.compare_digest(v.encode(), SETUP_TOKEN.encode()))

    # ---- replies ----
    def send(self, code, body, ctype="application/json", extra=None):
        if not isinstance(body, bytes):
            body = (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def page(self, name, **subs):
        html = open(os.path.join(HERE, "static", name), encoding="utf-8").read()
        for k, v in subs.items():
            html = html.replace("__" + k.upper() + "__", v)
        return self.send(200, html, "text/html; charset=utf-8")

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

    # ---- request bodies ----
    def content_length(self, limit):
        if self.headers.get("Transfer-Encoding"):
            raise Refused(411, "send a Content-Length")
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        if not re.fullmatch(r"\d{1,12}", raw.strip()):
            raise Refused(400, "bad Content-Length")
        n = int(raw)
        if n > limit:
            raise Refused(413, "too large")
        return n

    def json_body(self, limit=100_000):
        n = self.content_length(limit)
        data = self.rfile.read(n) if n else b""
        if len(data) < n:
            raise ConnectionError("body cut short")
        self.body_read = True
        try:
            req = json.loads(data or b"{}")
        except ValueError:
            raise Refused(400, "the body is not JSON")
        if not isinstance(req, dict):
            raise Refused(400, "the body must be a JSON object")
        return req

    def read_to_file(self, path, limit):
        """Stream an upload to disk in 1 MB pieces (never all of it in memory); refuses a body over limit before
        reading any of it, and a trickle slower than ~20 KB/s."""
        n = self.content_length(limit)
        t0, left = time.time(), n
        try:
            with open(path, "wb") as f:
                while left:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        raise ConnectionError("upload cut short")
                    f.write(chunk)
                    left -= len(chunk)
                    if time.time() - t0 > max(120, n / 20000):
                        raise Refused(408, "upload too slow")
        except BaseException:
            self.close_connection = True
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        self.body_read = True
        return n

    def do_GET(self):
        self.handle_method("GET")

    def do_POST(self):
        self.handle_method("POST")

    def handle_method(self, method):
        self.body_read = method == "GET"
        try:
            self.route(method)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ConnectionError, socket.timeout):
            self.close_connection = True
        except Refused as e:
            if not self.body_read:
                self.close_connection = True  # the body was not read: this connection cannot carry another request
            extra = {"Retry-After": str(math.ceil(e.extra["wait_s"]))} if e.extra.get("wait_s") else None
            self.send(e.code, dict({"error": str(e)}, **e.extra), extra=extra)
        except NeedConfirm as e:
            self.send(409, {"error": str(e), "confirm": e.what})
        except FileNotFoundError:
            self.send(404, {"error": "not found"})
        except ValueError as e:  # refusals meant for the user (not ready, plate not confirmed, ...)
            self.send(400, {"error": str(e)[:500]})
        except Exception:
            log(traceback.format_exc())  # the detail stays in hub.log; the page gets no paths or internals
            self.send(500, {"error": "something went wrong in the hub (details in hub.log)"})

    def gate(self, method, p):
        """Refuse what never reaches a route: the internet on the home port, a proxy pointed at the home port,
        a Host this hub does not answer to, and a POST that is not this hub's own page (or hubctl)."""
        kind = self.server.kind
        if kind == "lan":
            if not security.home_client(self.ip(), bool(CFG.get("tailscale"))):
                log(f"SECURITY refused {self.ip()} on the home port (not a home-network address)")
                raise Refused(403, "this port only answers your home network")
            if security.proxied(self.headers):
                raise Refused(403, "this port is for your home network: point a tunnel or proxy at remote_tunnel_port")
        names = lan_names() if kind == "lan" else remote_names()
        host = self.headers.get("Host") or ""
        if not security.host_ok(host, names):
            raise Refused(421, "unknown host name: open the hub by its address, or add the name to lan_hosts in "
                          "config.json" if kind == "lan" else
                          "unknown host name: add your remote address to remote_hosts in config.json")
        if method == "POST":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            want = "application/octet-stream" if p in ("/api/upload", "/api/setup/template/upload") else "application/json"
            if ctype != want:
                raise Refused(415, f"send {want}")
            if not security.origin_ok(self.headers, host, remote_names() if kind == "remote" else ()):
                log(f"SECURITY refused a cross-site POST {p} from {self.ip()} (Origin {self.headers.get('Origin')})")
                raise Refused(403, "this request did not come from the hub's own page")

    def check_secret(self, given, stored, what):
        """One login or step-up try: rate limited, a fixed ~0.5 s whatever the outcome."""
        kind, ip = self.server.kind, self.ip()
        w = LIMIT.wait(kind, ip)
        if w == float("inf"):
            raise Refused(423, "remote login is locked after too many wrong tries. Log in once on your home network "
                          "to open it again." if kind == "remote" else
                          "login is paused for 15 minutes after too many wrong tries")
        if w > 0:
            raise Refused(429, f"too many tries: wait {math.ceil(w)} s", wait_s=w)
        if not LOGIN_LOCK.acquire(timeout=3):
            raise Refused(429, "busy, try again in a moment", wait_s=2)
        try:
            t0 = time.time()
            ok = security.check_secret(str(given or ""), stored)
            ok = what(ok) if callable(what) else ok
            time.sleep(max(0.0, 0.5 - (time.time() - t0)))
        finally:
            LOGIN_LOCK.release()
        if ok:
            LIMIT.success(kind, ip)
            if kind == "lan" and CFG.get("remote_locked"):
                save_cfg(remote_locked=False)
                log("SECURITY remote login open again (owner logged in on the home network)")
        else:
            LIMIT.fail(kind, ip)
            log(f"SECURITY wrong secret from {ip} on the {kind} port")
        return ok

    def step_up(self, req):
        """Changes that send secrets or traffic somewhere else (printer, cameras, AI address or key, alerts, PIN,
        passphrase) need the passphrase again (the PIN on the home port when no passphrase is set). Good for 10 min."""
        if not CFG.get("setup_done") or self.session in ("api", "setup"):
            return
        if self.token and SESS.elevated(self.token):
            return
        stored = CFG.get("remote_pass_hash") or (CFG.get("pin_hash") if self.server.kind == "lan" else None)
        if not req.get("confirm_secret"):
            raise StepUp("type your passphrase again to change this" if CFG.get("remote_pass_hash")
                         else "type your PIN again to change this")
        if not self.check_secret(req.get("confirm_secret"), stored, None):
            raise StepUp("that was not right")
        SESS.elevate(self.token, 600)

    def login(self):
        kind = self.server.kind
        req = self.json_body(10_000)
        if not CFG.get("setup_done") or not CFG.get("pin_hash"):
            raise Refused(409, "finish setup on the hub PC first")
        if kind == "remote":
            if not remote_ready():
                raise Refused(409, "remote login is not set up: set a passphrase and an authenticator on the setup page at home")
            seen = {}

            def code_ok(pass_ok):  # both checked every time, so the time taken says nothing about which was wrong
                st = security.totp_check(CFG["totp_secret"], req.get("code"), int(CFG.get("totp_last_step") or 0))
                seen["step"] = st
                return pass_ok and st is not None
            ok = self.check_secret(req.get("passphrase"), CFG["remote_pass_hash"], code_ok)
            if ok:
                save_cfg(totp_last_step=seen["step"])  # each code works once
        else:
            ok = self.check_secret(str(req.get("password") or "").strip(), CFG["pin_hash"], None)
        if not ok:
            raise Refused(403, "wrong passphrase or code" if kind == "remote" else "wrong PIN")
        dev = security.device_name(self.headers.get("User-Agent"))
        tok = SESS.create(kind, dev, self.ip())
        self.set_session_cookie(tok)
        log(f"login: new {kind} session for {dev} from {self.ip()}")
        push(f"New sign-in to {NAME}: {dev} ({'remote' if kind == 'remote' else 'home network'}, {self.ip()})",
                     "high" if kind == "remote" else "default")
        return self.send(200, {"ok": True})

    def route(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        self.gate(method, p)
        kind = self.server.kind
        if p == "/login" and method == "POST":
            return self.login()
        if p == "/logout" and method == "POST":
            self.json_body(1000)
            if self.authed() and isinstance(self.session, dict):
                SESS.revoke(token=self.token)
            self.set_session_cookie("", 0)
            return self.send(200, {"ok": True})
        if p == "/setup" and q.get("token") and not CFG.get("setup_done"):
            # the one-time setup link: swap the token for a cookie and drop it from the address bar
            if not (SETUP_TOKEN and hmac.compare_digest(q["token"].encode(), SETUP_TOKEN.encode())) or kind != "lan":
                return self.send(403, NOT_SET_UP, "text/html; charset=utf-8")
            self.cookie_out.append(f"hubsetup={SETUP_TOKEN}; Path=/; HttpOnly; SameSite=Strict")
            return self.send(303, b"", "text/plain", extra={"Location": "/setup"})
        if p in ("/", "/index.html", "/setup"):
            if not CFG.get("setup_done"):
                if self.setup_cookie_ok():
                    return self.page("setup.html")
                return self.send(200, NOT_SET_UP, "text/html; charset=utf-8")
            if not self.authed():
                return self.page("login.html", mode=kind)
            return self.page("setup.html" if p == "/setup" else "index.html")
        if p.startswith("/api/setup"):
            if not CFG.get("setup_done") and self.setup_cookie_ok():
                self.session = "setup"
            elif not self.authed():
                return self.send(401, {"error": "login"})
            return setup_route(self, method, p, q)
        if p == "/manifest.json":
            return self.send(200, open(os.path.join(HERE, "static", "manifest.json"), "rb").read(),
                             "application/manifest+json")
        if p == "/icon.svg":
            return self.send(200, open(os.path.join(HERE, "static", "icon.svg"), "rb").read(), "image/svg+xml")
        if not self.authed():
            return self.send(401, {"error": "login"})
        if kind == "remote" and method == "POST":
            log(f"remote {self.device()} ({self.ip()}): POST {p}")  # audit trail of everything done from away

        if p == "/api/sessions" and method == "GET":
            return self.send(200, {"sessions": SESS.listing(self.token), "remote_locked": LIMIT.remote_locked()})
        if p == "/api/sessions/revoke" and method == "POST":
            req = self.json_body(1000)
            n = SESS.revoke(kind=None, keep=self.token) if req.get("all_others") else SESS.revoke(sid=str(req.get("id") or ""))
            log(f"sessions: {n} revoked by {self.device()}")
            return self.send(200, {"revoked": n, "sessions": SESS.listing(self.token)})
        if p == "/api/bed_cleared" and method == "POST":
            self.json_body(1000)
            save_cfg(bed_cleared_at=time.time(), bed_cleared_by=self.device() or "?")
            log(f"bed marked cleared by {self.device()}")
            return self.send(200, bed_state())
        if p == "/api/snapshot" and method == "GET":  # a fresh frame and when it was taken, for the send sheet
            cam = WEBCAM if q.get("cam") == "webcam" and WEBCAM else CAM
            f = cam.snapshot(cam.wake)
            if not f:
                return self.send(503, {"error": "camera not answering"})
            return self.send(200, f, "image/jpeg", extra={"X-Frame-Time": f"{time.time():.0f}"})

        if p == "/api/status":
            return self.send(200, {"printer": summary(), "slicing": SLICE_LOCK.locked(), "watch": WATCH.state,
                                   "colours": CFG["colours"], "leveling": CFG["leveling"], "hold": hold_state(),
                                   "webcam": bool(WEBCAM), "drying": DRY.public(), "watch_ready": bool(watcher.backend(CFG)),
                                   "bed": bed_state(), "remote": kind == "remote", "device": self.device()})
        if p == "/api/watch" and method == "POST":
            req = self.json_body(10000)
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
            req = self.json_body(10000)
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
        if p == "/api/raw":  # the printer's own status, less its cloud registration codes and network ids
            d = {k: v for k, v in (STATE["detail"] or {}).items()
                 if k not in ("flashRegisterCode", "polarRegisterCode", "macAddr", "ipAddr", "cameraStreamUrl")}
            return self.send(200, {"detail": d or None, "error": STATE["error"]})
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
            fname = os.path.basename(q.get("name", "upload.stl")).replace("..", "")[:120]
            if q.get("job"):
                load_job(q["job"])  # a real job, before any of the body is read
            if not UPLOADS.acquire(timeout=1):
                raise Refused(503, "another upload is running, try again in a moment", wait_s=5)
            try:
                os.makedirs(INCOMING, exist_ok=True)
                tmp = os.path.join(INCOMING, secrets.token_hex(8))
                n = self.read_to_file(tmp, 300_000_000)  # streamed to disk; the job folder only once it all came in
                with JOB_LOCK:
                    job = load_job(q["job"]) if q.get("job") else new_job(
                        (q.get("title") or os.path.splitext(fname)[0])[:60], q.get("source", "phone")[:20])
                stored = f"{len(os.listdir(job_dir(job['id']))):02d}-{re.sub(r'[^A-Za-z0-9._-]+', '_', fname)}"
                os.replace(tmp, os.path.join(job_dir(job["id"]), stored))
            finally:
                UPLOADS.release()
            with JOB_LOCK:
                job = load_job(job["id"])
                add_file(job, fname, os.path.join(job_dir(job["id"]), stored))
            log(f"upload {job['id']} {fname} {n} bytes from {q.get('source', 'phone')[:20]}")
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
                return self.send(200, {"check": job.get("check"), "hard": job.get("hard") or [],
                                       "bed": bed_state(s) if s["online"] else None, "remote": kind == "remote",
                                       "problems": material_problems(job, s) if s["online"] else [],
                                       "notes": (colour_notes(job, s) if s["online"] else [])
                                                + [n for n in [z_offset(job)[1]] if n]})
            if method == "POST" and act == "update":
                req = self.json_body(100000)
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
                self.json_body(1000)
                job = load_job(jid)
                if job.get("kind") != "model" or not job["parts"]:
                    raise ValueError("nothing to slice")
                threading.Thread(target=slice_job, args=(jid,), daemon=True).start()
                job["state"] = "queued"
                return self.send(200, job)
            if method == "POST" and act == "print":
                req = self.json_body(10000)
                job = send_job(jid, bool(req.get("leveling", CFG["leveling"])), bool(req.get("plate_clear")),
                               bool(req.get("allow_unchecked")), self.device() if kind == "remote" else None,
                               bool(req.get("no_mark_ok")))
                return self.send(200, job)
            if method == "POST" and act == "delete":
                self.json_body(1000)
                shutil.rmtree(d, ignore_errors=True)
                return self.send(200, {"ok": True})
        if p == "/api/adjust" and method == "POST":
            req = self.json_body(10000)
            return self.send(200, adjust(str(req.get("item", "")), req.get("value")))
        if p == "/api/control" and method == "POST":
            a = self.json_body(10000).get("action")
            fn = {"pause": lambda: PR.job("pause"), "resume": lambda: PR.job("continue"),
                  "cancel": lambda: PR.job("cancel"), "light_on": lambda: PR.light(True),
                  "light_off": lambda: PR.light(False), "clear_plate": PR.clear_platform}.get(a)
            if not fn:
                raise ValueError("unknown action")
            file = summary().get("file")
            r = fn()
            LAST_CONTROL[a] = time.time()
            log(f"control {a}: {r}" + (f" (remote, {self.device()})" if kind == "remote" else ""))
            if kind == "remote" and a == "cancel" and PR.ok(r):
                push(f"Print cancelled remotely from {self.device()}: {file}")
            return self.send(200 if PR.ok(r) else 409, {"ok": PR.ok(r), "reply": r})
        if method == "POST" and not self.body_read:
            self.close_connection = True
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
# Flash Studio export as the slicing template, the optional watcher, alerts and remote access. Before setup is done
# it opens only with the one-time setup link (setup-url.txt) on this PC, and the hub listens on this PC only; after
# that it sits behind the PIN like every other page, and changes that move secrets or traffic need it again.

NOT_SET_UP = (f"<!doctype html><meta name=viewport content='width=device-width'><title>{NAME}</title>"
              "<body style='font-family:sans-serif;background:#14161a;color:#eee;padding:16px'>"
              f"<h2>{NAME} is not set up yet</h2><p>Finish setup on the PC that runs the hub, with the one-time "
              "setup link: <b>setup.ps1</b> opens it for you, and it is also in <b>setup-url.txt</b> in the hub's "
              "folder. Open it in a browser on that PC.</p>")
SCAN = {"running": False, "found": [], "done": 0, "total": 0}
FS_SLICES = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp", "orca-flashforge", "slice")


def apply_printer():
    PR.ip, PR.serial, PR.check = CFG["printer_ip"], CFG["serial"], CFG["check_code"]
    CAM.url = f"http://{CFG['printer_ip']}:8080/?action=stream"
    STATE.update(error="checking the printer", detail=None)


def apply_webcam():
    """A changed phone camera address takes effect at once, for the page, the watcher and the drying check."""
    global WEBCAM
    WEBCAM = phone_camera(CFG["webcam_url"], CFG["webcam_fps"]) if CFG["webcam_url"] else None
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
    """A phone camera address: http, on the home network (resolved and checked; this PC only with
    camera_allow_loopback). The setup page never fetches the internet, loopback or 169.254 for you."""
    security.pinned(url, bool(CFG.get("camera_allow_loopback")))
    return url


def short_path(path):
    """A path for the setup page with the user's own folder shown as ~ (no user name on the page)."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and path.lower().startswith(home.lower()) else path


def template_info(path):
    with zipfile.ZipFile(path) as z:
        slicer.zip_sizes_ok(z)
        cfg = json.loads(slicer.zip_entry(z, "Metadata/project_settings.config", 50 * 2 ** 20))
        head = slicer.zip_head(z, "Metadata/plate_1.gcode", 400).decode(errors="ignore")
    gen = re.search(r"generated by (.+)", head)
    return {"file": os.path.basename(path), "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path))),
            "printer": cfg.get("printer_settings_id"), "process": cfg.get("print_settings_id"),
            "filaments": cfg.get("filament_settings_id") or [], "slicer": gen.group(1).strip() if gen else None,
            "problems": slicer.check_template(path)}


def setup_state(h=None):
    s = summary()
    tpl = None
    if slicer.template_ready():
        try:
            tpl = template_info(slicer.TEMPLATE)
        except Exception as e:
            tpl = {"problems": [str(e)[:200]]}
    key, okey = CFG.get("anthropic_api_key") or "", CFG.get("openai_api_key") or ""
    running = {srv.kind + ("_https" if srv.tls else "") for srv in SERVERS}
    return {"name": NAME, "setup_done": bool(CFG.get("setup_done")), "local": True,
            "door": h.server.kind if h else "lan",
            "step_up": "passphrase" if CFG.get("remote_pass_hash") else "PIN",
            "printer": {"ip": CFG["printer_ip"], "serial_set": bool(CFG["serial"]), "check_set": bool(CFG["check_code"]),
                        "online": s["online"], "status": s.get("status"), "error": s.get("error")},
            "pin_set": bool(CFG.get("pin_chosen")), "port": CFG["port"], "host": CFG["host"],
            "lan_urls": [f"http://{ip}:{CFG['port']}" for ip in pc_ips()],
            "webcam_url": CFG["webcam_url"],
            "flash_studio": {"path": short_path(slicer.FLASH_STUDIO), "found": os.path.isfile(slicer.FLASH_STUDIO)},
            "template": tpl, "fs_slices": os.path.isdir(FS_SLICES),
            "watcher": {"backend": watcher.backend(CFG), "chosen": CFG.get("watch_backend") or "",
                        "key_end": key[-4:] if key else "", "enabled": WATCH.state["enabled"],
                        "openai": {"preset": CFG.get("openai_preset") or "", "base_url": CFG.get("openai_base_url") or "",
                                   "model": CFG.get("openai_model") or "", "strong_model": CFG.get("openai_strong_model") or "",
                                   "key_end": okey[-4:] if okey else ""},
                        "claude_cli": bool(shutil.which("claude") or os.path.exists(WATCH.claude or ""))},
            "ntfy": {"server": CFG.get("ntfy_server") or "https://ntfy.sh", "topic": CFG.get("ntfy_topic") or "",
                     "token_set": bool(CFG.get("ntfy_token"))},
            "remote": {"pass_set": bool(CFG.get("remote_pass_hash")), "totp_set": bool(CFG.get("totp_secret")),
                       "tunnel_port": CFG.get("remote_tunnel_port") or 0, "https_port": CFG.get("remote_https_port") or 0,
                       "tunnel_on": "remote" in running, "https_on": "remote_https" in running,
                       "hosts": CFG.get("remote_hosts") or [], "locked": LIMIT.remote_locked(),
                       "tailscale": bool(CFG.get("tailscale"))},
            "scan": SCAN}


def local_ai(url):
    """True for an AI server address on this PC or the home network (Ollama, LM Studio)."""
    host = urlparse(url).hostname or ""
    try:
        return all(security.home_client(i[4][0]) for i in socket.getaddrinfo(host, None)) and bool(host)
    except OSError:
        return False


def ntfy_url_ok(url):
    u = urlparse(url)
    if u.scheme == "https" and u.hostname:
        return True
    return u.scheme == "http" and bool(u.hostname) and local_ai(url)


def setup_route(h, method, p, q):
    req = h.json_body(20000) if method == "POST" and p != "/api/setup/template/upload" else {}
    if p == "/api/setup" and method == "GET":
        return h.send(200, setup_state(h))
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
        h.step_up(req)
        ip = (req.get("ip") or "").strip()
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            raise ValueError("printer address looks like 192.168.1.50")
        if not (a.is_private or a.is_loopback) or a.is_multicast or a.is_unspecified:
            raise ValueError("the printer must be on your own network (an address like 192.168.1.50)")
        serial, check = (req.get("serial") or "").strip()[:64], (req.get("check_code") or "").strip()[:64]
        if not serial or not check:
            raise ValueError("type the serial number and the check code from the printer screen")
        try:
            r = printer.Printer(ip, serial, check).detail()
        except Exception as e:
            raise ValueError(f"no answer from {ip}:8898 ({type(e).__name__}). Is the printer on and on this network?")
        if r.get("code") != 0:
            msg = str(r.get("message") or "")[:80] or "no"
            hint = (" Turn on LAN Only mode on the printer screen first." if "lan" in msg.lower() else
                    " Check the serial number and check code against the printer screen.")
            raise ValueError(f"the printer said: {msg}.{hint}")
        save_cfg(printer_ip=ip, serial=serial, check_code=check)
        apply_printer()
        d = r.get("detail") or {}
        log(f"setup: printer {ip} answers, status {d.get('status')}")
        return h.send(200, {"ok": True, "status": text(d.get("status"), 30), "name": text(d.get("name"), 60)})
    if p == "/api/setup/pin" and method == "POST":
        h.step_up(req)
        pin = str(req.get("pin") or "").strip()
        if not re.fullmatch(r"\d{4,8}", pin):
            raise ValueError("PIN must be 4 to 8 digits")
        save_cfg(pin_hash=security.hash_secret(pin), pin_chosen=True)
        n = SESS.revoke(kind="lan", keep=h.token)  # every other home-network login ends
        if h.server.kind == "lan" and not isinstance(h.session, dict):
            h.set_session_cookie(SESS.create("lan", security.device_name(h.headers.get("User-Agent")), h.ip()))
        log(f"setup: PIN changed, {n} other home-network logins ended")
        return h.send(200, {"ok": True})
    if p == "/api/setup/camera.jpg" and method == "GET":  # live preview for the camera step
        if q.get("which") == "printer":
            cam = CAM
        else:
            cam = phone_camera(private_url(q.get("url")))
        f = cam.snapshot(cam.wake if q.get("which") != "printer" else 20)
        return h.send(200, f, "image/jpeg") if f else h.send(503, {"error": "camera not answering"})
    if p == "/api/setup/camera" and method == "POST":
        url = (req.get("url") or "").strip()
        if url:
            if url != CFG.get("webcam_url"):
                h.step_up(req)
            private_url(url)
            f = phone_camera(url).snapshot(8)
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
                out.append({"file": os.path.basename(f), "problems": [f"unreadable: {str(e)[:100]}"]})
        return h.send(200, {"folder": short_path(FS_SLICES), "files": out})
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
            h.read_to_file(src, 200_000_000)
            presets = q.get("only_these_presets") == "1"
        problems = slicer.check_template(src)
        if problems:
            raise ValueError("this file cannot be the template: " + "; ".join(problems))
        hard = slicer.hard_check_file(src, CFG.get("hard_caps"))
        if hard:
            raise ValueError("this file cannot be the template: " + "; ".join(hard))
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
        if b not in ("", "api", "cli", "openai"):
            raise ValueError("unknown watcher choice")
        ch = {"watch_backend": b}
        redirects = b != (CFG.get("watch_backend") or "") and b in ("api", "openai")
        note = None
        if b == "openai":
            url = (req.get("base_url") or "").strip()
            u = urlparse(url)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise ValueError("type the server address, like https://api.openai.com/v1")
            if u.hostname.startswith("169.254.") or u.hostname in ("metadata.google.internal",):
                raise ValueError("that address is not an AI server")
            if u.scheme == "http":
                if not local_ai(url):
                    raise ValueError("an AI server on the internet must use https:// (the key would travel in clear text)")
                if not req.get("local_server"):
                    raise ValueError("plain http:// is only for your own server on this PC or network: tick "
                                     "'my own local server' to confirm")
            model = (req.get("model") or "").strip()
            if not model:
                raise ValueError("type the model name (one that can read images)")
            new_key = (req.get("openai_key") or "").strip()
            old = urlparse(CFG.get("openai_base_url") or "")
            moved = (u.scheme, u.hostname, u.port) != (old.scheme, old.hostname, old.port)
            redirects = redirects or moved or bool(new_key)
            ch.update(openai_preset=str(req.get("preset") or "")[:20], openai_base_url=url, openai_model=model[:100],
                      openai_strong_model=(req.get("strong_model") or "").strip()[:100])
            if new_key:
                ch["openai_api_key"] = new_key
            elif moved and CFG.get("openai_api_key"):
                ch["openai_api_key"] = ""  # a saved key never follows the address to another server
                note = "the saved key was for another server, so it was cleared: type the key for this one"
        if b == "api":
            key = (req.get("api_key") or "").strip()
            if key and not key.startswith("sk-ant-"):
                raise ValueError("an Anthropic API key starts with sk-ant-")
            if key:
                ch["anthropic_api_key"] = key
                redirects = True
            elif not CFG.get("anthropic_api_key"):
                raise ValueError("paste your Anthropic API key")
        if req.get("forget_key"):  # forgetting works whatever is chosen
            ch["anthropic_api_key"] = ""
        if req.get("forget_openai_key"):
            ch["openai_api_key"] = ""
        if redirects:
            h.step_up(req)
        save_cfg(**ch)
        on = bool(req.get("enabled")) and bool(watcher.backend(CFG))
        WATCH.state["enabled"] = on
        save_cfg(watch=on)
        log(f"setup: watcher backend '{watcher.backend(CFG)}', {'on' if on else 'off'}")
        return h.send(200, dict(setup_state(h)["watcher"], note=note))
    if p == "/api/setup/watcher/test" and method == "POST":  # one real check on a printer camera frame
        if not watcher.backend(CFG):
            raise ValueError("choose an AI for the watcher first and save")
        f = CAM.snapshot(20)
        if not f:
            raise ValueError("the printer camera is not answering, so there is no frame to test with")
        img = os.path.join(WATCH.dir, "setup-test.jpg")
        open(img, "wb").write(f)
        v = WATCH.ask(img, "Print context: setup test, the plate may be empty.", CFG.get("watch_model", "haiku"), "printer")
        return h.send(200, {k: v.get(k) for k in ("verdict", "part", "reason", "cost", "no_images") if k in v})
    if p == "/api/setup/ntfy" and method == "POST":
        if req.get("test"):
            if not CFG.get("ntfy_topic"):
                raise ValueError("save a topic first")
            WATCH.notify(f"Test alert from {NAME}", "default")
            return h.send(200, setup_state(h)["ntfy"])
        h.step_up(req)
        server = (req.get("server") or "https://ntfy.sh").strip().rstrip("/")
        if not ntfy_url_ok(server):
            raise ValueError("the ntfy server must be https:// (or http:// on your own network)")
        topic = secrets.token_urlsafe(18) if req.get("new_topic") else (req.get("topic") or "").strip()
        if topic and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
            raise ValueError("a topic is letters, digits, - and _ only")
        if topic and len(topic) < 16 and "ntfy.sh" in server:
            raise ValueError("on the public ntfy.sh server the topic is the only secret: use 16 or more characters "
                             "(tap Make a private topic)")
        ch = {"ntfy_server": server, "ntfy_topic": topic}
        if (req.get("token") or "").strip():
            ch["ntfy_token"] = req["token"].strip()
        if req.get("forget_token"):
            ch["ntfy_token"] = ""
        save_cfg(**ch)
        log(f"setup: alerts {'on' if topic else 'off'} ({urlparse(server).hostname})")
        return h.send(200, setup_state(h)["ntfy"])
    if p == "/api/setup/remote/totp" and method == "POST":  # a new authenticator secret, saved once a code matches
        h.step_up(req)
        sec = security.totp_new_secret()
        PENDING_TOTP[(h.session or {}).get("id") if isinstance(h.session, dict) else "x"] = sec
        return h.send(200, {"secret": sec, "uri": security.otpauth_uri(sec, "owner")})
    if p == "/api/setup/remote" and method == "POST":
        h.step_up(req)
        ch, what = {}, []
        pw = req.get("passphrase")
        if pw is not None and pw != "":
            pw = str(pw)
            if len(pw) < 12:
                raise ValueError("the passphrase needs 12 or more characters (a few words works well)")
            if pw != str(req.get("passphrase2") or ""):
                raise ValueError("the two passphrases do not match")
            ch["remote_pass_hash"] = security.hash_secret(pw)
            what.append("passphrase")
        if req.get("code"):
            sid = (h.session or {}).get("id") if isinstance(h.session, dict) else "x"
            sec = PENDING_TOTP.get(sid)
            st = security.totp_check(sec, req["code"]) if sec else None
            if st is None:
                raise ValueError("that code does not match: check the phone's clock and type the newest code")
            ch.update(totp_secret=sec, totp_last_step=st)
            PENDING_TOTP.pop(sid, None)
            what.append("authenticator")
        if not ch:
            raise ValueError("nothing to change")
        save_cfg(**ch)
        n = SESS.revoke(kind="remote")  # new secrets: every remote login ends
        log(f"setup: remote {' and '.join(what)} set, {n} remote logins ended")
        push(f"{NAME}: remote login {' and '.join(what)} changed", "default")
        return h.send(200, setup_state(h)["remote"])
    if p == "/api/setup/remote/unlock" and method == "POST":
        if h.server.kind != "lan":
            raise ValueError("unlock remote login from your home network")
        LIMIT.unlock("remote")
        save_cfg(remote_locked=False)
        log(f"SECURITY remote login unlocked by {h.device()}")
        return h.send(200, setup_state(h)["remote"])
    if p == "/api/setup/done" and method == "POST":
        if not CFG.get("pin_chosen"):
            raise ValueError("choose your PIN first")
        first = not CFG.get("setup_done")
        save_cfg(setup_done=True)
        log("setup: done")
        if first:
            finish_setup()
        return h.send(200, {"ok": True})
    return h.send(404, {"error": "not found"})


# ---------- listeners ----------

def finish_setup():
    """Setup done: the one-time link stops working, the home port opens to the network (it listened on this PC
    only until now) and the remote ports start."""
    global SETUP_TOKEN
    SETUP_TOKEN = None
    try:
        os.remove(SETUP_URL_FILE)
    except OSError:
        pass
    if CFG["host"] not in ("127.0.0.1", "localhost") and SERVERS:
        threading.Timer(1.0, lambda: [s.shutdown() for s in SERVERS if s.kind == "lan"]).start()
    start_remote()


def tls_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CFG["tls_cert"], CFG.get("tls_key") or None)
    return ctx


def serve(srv):
    SERVERS.append(srv)
    try:
        srv.serve_forever()
    finally:
        if srv in SERVERS:
            SERVERS.remove(srv)
        srv.server_close()


def start_remote():
    if not CFG.get("setup_done"):
        return
    if CFG.get("remote_tunnel_port") and not any(s.kind == "remote" and not s.tls for s in SERVERS):
        try:  # for cloudflared on this PC: loopback only, the browser sees https through Cloudflare
            srv = HubServer(("127.0.0.1", int(CFG["remote_tunnel_port"])), "remote", secure=True, per_ip=False)
            threading.Thread(target=serve, args=(srv,), daemon=True).start()
            log(f"remote tunnel port 127.0.0.1:{CFG['remote_tunnel_port']} (passphrase + code)")
        except OSError as e:
            log(f"remote tunnel port {CFG['remote_tunnel_port']} not started: {e}")
    if CFG.get("remote_https_port") and not any(s.tls for s in SERVERS):
        try:
            srv = HubServer((CFG.get("remote_https_host") or "0.0.0.0", int(CFG["remote_https_port"])), "remote",
                            tls=tls_context())
            threading.Thread(target=serve, args=(srv,), daemon=True).start()
            log(f"remote HTTPS port {CFG['remote_https_port']} (passphrase + code)")
        except (OSError, ssl.SSLError, KeyError) as e:
            log(f"remote HTTPS port {CFG.get('remote_https_port')} not started (certificate tls_cert / tls_key?): {e}")
    if (CFG.get("remote_tunnel_port") or CFG.get("remote_https_port")) and not remote_ready():
        log("remote ports are open but remote login is not set up yet (setup page: Remote access)")


if __name__ == "__main__":
    threading.Thread(target=poll_status, daemon=True).start()
    threading.Thread(target=WATCH.run, daemon=True).start()
    threading.Thread(target=push_events, daemon=True).start()
    threading.Thread(target=DRY.run, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()
    if SETUP_TOKEN:
        url = f"http://127.0.0.1:{CFG['port']}/setup?token={SETUP_TOKEN}"
        open(SETUP_URL_FILE, "w", encoding="utf-8").write(url + "\n")
    start_remote()
    while True:
        # Until setup is done the home port listens on this PC only, so nobody else can reach the setup page.
        host = CFG["host"] if CFG.get("setup_done") else "127.0.0.1"
        try:
            srv = HubServer((host, CFG["port"]), "lan")
        except OSError as e:
            log(f"port {CFG['port']} busy ({e}), trying again in 5 s")
            time.sleep(5)
            continue
        log(f"{NAME} on {host}:{CFG['port']}, printer {CFG['printer_ip'] or 'not set up'}"
            + ("" if CFG.get("setup_done") else f" - finish setup with the link in {SETUP_URL_FILE}"))
        serve(srv)
        if not CFG.get("setup_done"):
            break
