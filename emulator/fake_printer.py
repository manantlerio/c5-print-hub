# Fake Creator 5 Pro: answers the printer's LAN API (:8898) and serves an MJPEG camera (:8080) on 127.0.0.1,
# so the hub and ForkaSlicer send can be tested while the real printer is busy. Stdlib only.
# Run: python -I emulator/fake_printer.py [--speed 60] [--multi-cam] [--host 127.0.0.3]
#
# Copied from the real printer (fw 2.0.0, 2026-10-25): every /detail field and its shape, status values
# ready / printing / pause / completed / cancel, {"code": 0, "message": "Success"} on upload, start and control,
# E0162 with an empty slot on a run-out pause, camera 640 x 480 at 1.3 fps and one viewer at a time.
# Guessed (never seen on the real printer): every refusal reply (code 1 + message), errorCode "" when idle,
# status while bed leveling (kept "printing"), what a second camera viewer gets (connection closed).
#
# The print is replayed from the G-code: move lengths / feed rates give each layer's share of the time, scaled to
# the slicer's own estimate, and --speed runs it faster (60 = an hour of print in a minute). Times reported in
# /detail are print-time seconds, as the real printer would show them.
#
# Test hooks (no auth, this PC only), on the API port:
#   GET /sim/state                       everything the fake printer holds, including the last start request
#   GET /sim/runout?slot=2               run-out pause: status pause, E0162, slot empty
#   GET /sim/load?slot=2&colour=%23000000&material=PLA
#   GET /sim/offline?seconds=30          stop answering for a while (connections are dropped)
#   GET /sim/camquiet?seconds=30         camera accepts the stream but sends no frames (slow wake-up)
#   GET /sim/finish                      jump to the end of the print
#   GET /sim/speed?x=600
#   GET /sim/idlecut?seconds=20         idle heat cutoff: with no print, bed + chamber targets go to 0 this long
#                                        after the last temperature command (real fw 2.0.0: 11-14 min, 2026-10-28)
#   GET /sim/status?s=printing           force a status (a print started from the screen)
import argparse, base64, bisect, copy, glob, json, math, os, re, socket, sys, threading, time, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "storage")
SERIAL, CHECK = "SIMULATOR0001", "simcheck"
# A plain grey test frame, used when the frames folder has no JPEGs (a fresh install has no watcher frames yet).
SIM_FRAME = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYV"
    "pQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAB4"
    "AKADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhBy"
    "JxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKT"
    "lJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAA"
    "AAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRom"
    "JygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExc"
    "bHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDnqKKKskKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiig"
    "AooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAL2lFxNL5cbuxjx+6bEi8jlff+ma1o3a2uIZ"
    "d3mMjXL5K4JPkIcEevY++a5uikM6SJY7JIIdwEaX8MxY+h34P/AHyqn8aq2kE0UUFtJE6zEXZEZHzEGEAce5B/KsWigDV05RYGSa+SaIMRFtMf"
    "JU8twSOwx+NS2sstrDLb3FxKIYr2KNwHONn7zcPocVi0UCOhsftn25P7R37ftEXk7um7zF+57bd3Tjp7VnahJ5tnauJpZxuceZKMNn5fl6ngcH"
    "r/ABGs+igYUUUUxBRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFT/Yrj/nn/48KgrerGtU"
    "cLWNKcFLcyPsVx/zz/8AHhR9iuP+ef8A48K16Kw+sSNPZRMj7Fcf88//AB4UfYrj/nn/AOPCteij6xIPZRMj7Fcf88//AB4UfYrj/nn/AOPCte"
    "ij6xIPZRMj7Fcf88//AB4UfYrj/nn/AOPCteij6xIPZRMj7Fcf88//AB4VCylWKsMEHBrdrFuP+PiT/fP862pVXNu5E4KOxHRRRW5kFFFFABRR"
    "RQAUUUUAFb1YNb1cuJ6G9HqFFFFcpsFFFFABRRRQAUUUUAFYtx/x8Sf75/nW1WLcf8fEn++f5104bdmNXZEdFFFdZgFFFFABRRRQAUUUUAFaf9"
    "oQ/wB2T8h/jWZRUTpqe5UZOOxp/wBoQ/3ZPyH+NH9oQ/3ZPyH+NZlFR7CBXtZGn/aEP92T8h/jR/aEP92T8h/jWZRR7CAe1kaf9oQ/3ZPyH+NH"
    "9oQ/3ZPyH+NZlFHsIB7WRp/2hD/dk/If40f2hD/dk/If41mUUewgHtZGn/aEP92T8h/jWdKweV2HRmJptFVCnGGxMpuW4UUUVoSFFFFABRRRQA"
    "UUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQB//2Q=="
)
AMBIENT = 27.0
LEVELING_S = 90  # print-time seconds added at the start when levelingBeforePrint is on (real one not measured)

# The real printer's /detail reply, 2026-10-25 (idle values filled in where it was mid-print).
DETAIL = {
    "autoShutdown": "close", "autoShutdownTime": 0, "camera": 1,
    "cameraStreamUrl": "http://127.0.0.1:8080/?action=stream",
    "chamberFanSpeed": 0, "chamberTargetTemp": 0, "chamberTemp": 27, "coolingFanSpeed": 0,
    "cumulativeFilament": 631.9329833984375, "cumulativePrintTime": 4162, "currentPrintSpeed": 0,
    "doorStatus": "close", "errorCode": "", "estimatedLeftLen": 0, "estimatedLeftWeight": 0.0,
    "estimatedRightLen": 0, "estimatedRightWeight": 0.0, "estimatedTime": 0, "externalFanStatus": "close",
    "fillAmount": 20, "firmwareVersion": "2.0.0-sim", "flashRegisterCode": "", "internalFanStatus": "close",
    "ipAddr": "127.0.0.1", "leftFilamentType": "PLA;PLA;PLA;PLA", "leftTargetTemp": 0, "leftTemp": 27,
    "lidar": 1, "lightStatus": "open", "location": "Group A", "macAddr": "00:00:00:00:00:00",
    "matlStationInfo": {"currentLoadSlot": 0, "currentSlot": 0, "slotCnt": 4, "slotInfos": [
        {"hasFilament": True, "materialColor": "#000000", "materialName": "PETG", "slotId": 1},
        {"hasFilament": True, "materialColor": "#000000", "materialName": "PLA", "slotId": 2},
        {"hasFilament": True, "materialColor": "#2E54DD", "materialName": "PLA", "slotId": 3},
        {"hasFilament": True, "materialColor": "#F82D29", "materialName": "PLA", "slotId": 4}],
        "stateAction": 0, "stateStep": 0},
    "measure": "256X256X256", "model": "Creator 5 Pro", "name": "Creator 5 Pro (simulator)", "nozzleCnt": 4,
    "nozzleModel": "0.4mm;0.4mm;0.4mm;0.4mm", "nozzleStyle": 0, "nozzleTargetTemps": [0, 0, 0, 0],
    "nozzleTemps": [27, 27, 27, 27], "pid": 41, "platTargetTemp": 0, "platTemp": 27, "polarRegisterCode": "",
    "printDuration": 0, "printFileName": "", "printFileThumbUrl": "", "printLayer": 0, "printProgress": 0.0,
    "printSpeedAdjust": 100, "remainingDiskSpace": 4.729286193847656, "rightFilamentType": "PLA;PLA;PLA;PLA",
    "rightTargetTemp": 0, "rightTemp": 27, "status": "ready", "targetPrintLayer": 0, "tvoc": 0,
    "zAxisCompensation": 0.0}


# ---------- G-code replay ----------

def read_gcode(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)), None)
            if not name:
                raise ValueError("no sliced plate in that 3MF")
            return z.read(name).decode(errors="ignore")
    return open(path, encoding="utf-8", errors="ignore").read()


# Layer timing lives in the hub's slicer.py, so the hub's finish-time estimate and this replay use the same plan.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from slicer import ARG, duration, eta_model, eta_times, timeline  # noqa: E402


# ---------- printer state ----------

def file_share(prof, pt, total):
    """The real printer's printProgress is the share of the file read (bytes), not of time or filament."""
    if pt <= 0:
        return 0.0
    if not prof:
        return pt / total if total else 1.0
    t, b = prof
    i = bisect.bisect_left(t, pt * t[-1] / total)
    return b[i] if i < len(b) else 1.0


class Sim:
    def __init__(self, speed):
        self.lock = threading.RLock()
        self.speed = speed
        self.d = copy.deepcopy(DETAIL)
        self.files = {}  # name -> path
        self.job = None  # {"name", "plan", "t", "lead", "fired"}
        self.offline_until = 0.0
        self.idle_cut, self.temp_at = 0.0, time.time()
        self.last_start = None
        self.log = []
        os.makedirs(STORE, exist_ok=True)
        for p in glob.glob(os.path.join(STORE, "*")):
            self.files[os.path.basename(p)] = p

    def note(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        self.log = (self.log + [line])[-200:]

    def start(self, name, leveling, mappings=None):
        with self.lock:
            if self.d["status"] != "ready":
                return {"code": 1, "message": f"printer is {self.d['status']}, not ready"}
            if name not in self.files:
                return {"code": 1, "message": f"file not found: {name}"}
            try:
                text = read_gcode(self.files[name])
                plan = timeline(text)
                model = eta_model(text)
                prof = (eta_times(model), model["b"]) if model else None
            except Exception as e:
                return {"code": 1, "message": f"bad file: {e}"}
            self.job = {"name": name, "plan": plan, "prof": prof, "t": 0.0, "lead": LEVELING_S if leveling else 0,
                        "fired": 0}
            self.d.update(status="printing", printFileName=name, printLayer=0, printProgress=0.0,
                          targetPrintLayer=len(plan["layers"]), estimatedTime=int(plan["total"] + self.job["lead"]),
                          printDuration=0, errorCode="", currentPrintSpeed=100, coolingFanSpeed=100,
                          printFileThumbUrl="http://127.0.0.1:8898/getThum" if plan["thumb"] else "")
            self.last_start = {"fileName": name, "levelingBeforePrint": leveling, "materialMappings": mappings,
                               "time": time.strftime("%Y-%m-%d %H:%M:%S")}
            self.note(f"start {name}: {len(plan['layers'])} layers, {plan['total'] / 60:.1f} min print time, "
                      f"leveling={leveling}, mappings={mappings}")
            return {"code": 0, "message": "Success"}

    def job_ctl(self, action):
        with self.lock:
            st = self.d["status"]
            if action == "pause" and st == "printing":
                self.d["status"] = "pause"
            elif action == "continue" and st == "pause":
                slot = self.d["matlStationInfo"]["slotInfos"][self.d["matlStationInfo"]["currentSlot"] - 1] \
                    if self.d["matlStationInfo"]["currentSlot"] else None
                if self.d["errorCode"] == "E0162" and slot and not slot["hasFilament"]:
                    return {"code": 1, "message": "no filament in the current slot"}
                self.d.update(status="printing", errorCode="")
            elif action == "cancel" and st in ("printing", "pause"):
                self.end("cancel")
            else:
                return {"code": 1, "message": f"cannot {action} while {st}"}
            self.note(f"job {action}: now {self.d['status']}")
            return {"code": 0, "message": "Success"}

    def end(self, status):
        self.d.update(status=status, nozzleTargetTemps=[0, 0, 0, 0], platTargetTemp=0, currentPrintSpeed=0,
                      coolingFanSpeed=0)
        if status == "completed":
            self.d.update(printProgress=1.0, estimatedTime=0, printLayer=self.d["targetPrintLayer"])
            self.d["cumulativePrintTime"] += int(self.job["plan"]["total"] / 60)

    def clear_platform(self):
        with self.lock:
            if self.d["status"] not in ("completed", "cancel"):
                return {"code": 1, "message": f"nothing to clear while {self.d['status']}"}
            self.job = None
            self.d.update(status="ready", printFileName="", printLayer=0, targetPrintLayer=0, printProgress=0.0,
                          estimatedTime=0, printDuration=0, printFileThumbUrl="")
            self.d["matlStationInfo"]["currentSlot"] = 0
            self.note("plate cleared: ready")
            return {"code": 0, "message": "Success"}

    def runout(self, slot):
        with self.lock:
            ms = self.d["matlStationInfo"]
            ms["slotInfos"][slot - 1].update(hasFilament=False, materialColor="", materialName="")
            if self.d["status"] == "printing" and ms["currentSlot"] == slot:
                self.d.update(status="pause", errorCode="E0162")
            self.note(f"run-out slot {slot}: status {self.d['status']}")

    def tick(self, dt):
        with self.lock:
            j = self.job
            if j and self.d["status"] == "printing":
                j["t"] += dt * self.speed
                plan, pt = j["plan"], j["t"] - j["lead"]
                ev = plan["events"]
                while j["fired"] < len(ev) and ev[j["fired"]][0] <= max(pt, 0):  # start heats during leveling
                    _, kind, v = ev[j["fired"]]
                    if kind == "tool":
                        self.d["matlStationInfo"]["currentSlot"] = v + 1
                        slot = self.d["matlStationInfo"]["slotInfos"][v]
                        if not slot["hasFilament"]:  # a real run-out pauses on the empty slot
                            self.d.update(status="pause", errorCode="E0162")
                            self.note(f"tool T{v} has no filament in slot {v + 1}: pause E0162")
                    elif kind == "nozzle":
                        self.d["nozzleTargetTemps"][v[0]] = v[1]
                    elif kind == "bed":
                        self.d["platTargetTemp"] = v
                    elif kind == "speed":
                        self.d["printSpeedAdjust"] = int(v)
                    j["fired"] += 1
                    if self.d["status"] != "printing":
                        break
                if pt >= plan["total"]:
                    self.end("completed")
                    self.note(f"completed {j['name']}")
                else:
                    self.d.update(printDuration=int(j["t"]), estimatedTime=int(plan["total"] + j["lead"] - j["t"]),
                                  printProgress=file_share(j.get("prof"), pt, plan["total"]),
                                  printLayer=bisect.bisect_right(plan["layers"], pt) if pt >= 0 else 0)
            # temperatures follow their targets (rates rough, in print-time seconds)
            sim_dt = dt * (self.speed if self.d["status"] == "printing" else 1)
            for i, target in enumerate(self.d["nozzleTargetTemps"]):
                self.d["nozzleTemps"][i] = approach(self.d["nozzleTemps"][i], target or AMBIENT, 4 * sim_dt)
            self.d["platTemp"] = approach(self.d["platTemp"], self.d["platTargetTemp"] or AMBIENT, 1 * sim_dt)
            self.d["chamberTemp"] = approach(self.d["chamberTemp"], self.d["chamberTargetTemp"] or AMBIENT, 0.2 * sim_dt)
            if (self.idle_cut and self.d["status"] in ("ready", "completed", "cancel")
                    and time.time() - self.temp_at > self.idle_cut
                    and (self.d["platTargetTemp"] or self.d["chamberTargetTemp"])):
                self.d.update(platTargetTemp=0, chamberTargetTemp=0)
                self.note("idle heat cutoff: bed and chamber off")

    def run(self):
        last = time.time()
        while True:
            time.sleep(0.2)
            now = time.time()
            self.tick(now - last)
            last = now

    def detail(self):
        with self.lock:
            d = copy.deepcopy(self.d)
        d["nozzleTemps"] = [round(v) for v in d["nozzleTemps"]]
        d["platTemp"] = round(d["platTemp"])
        return d


def approach(cur, target, step):
    return min(target, cur + step) if cur < target else max(target, cur - step)


# ---------- API server (:8898) ----------

def reply(h, body, code=200, ctype="application/json"):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


def multipart_file(body, ctype):
    m = re.search(r"boundary=([^;]+)", ctype or "")
    if not m:
        raise ValueError("no multipart boundary")
    for part in body.split(b"--" + m.group(1).strip('"').encode()):
        head, _, data = part.partition(b"\r\n\r\n")
        fm = re.search(rb'name="gcodeFile"; filename="([^"]+)"', head)
        if fm:
            return fm.group(1).decode(errors="ignore"), data[:-2] if data.endswith(b"\r\n") else data
    raise ValueError("no gcodeFile part")


class Api(BaseHTTPRequestHandler):
    sim = None

    def log_message(self, *a):
        pass

    def gone(self):
        if time.time() < self.sim.offline_until and not self.path.startswith("/sim/"):
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return True
        return False

    def do_GET(self):
        if self.gone():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        sim = self.sim
        if u.path == "/getThum":
            t = sim.job and sim.job["plan"]["thumb"]
            return reply(self, t, 200, "image/png") if t else reply(self, {"code": 1, "message": "no thumbnail"}, 404)
        if u.path == "/sim/state":
            with sim.lock:
                return reply(self, {"detail": sim.detail(), "files": sorted(sim.files), "speed": sim.speed,
                                    "last_start": sim.last_start, "log": sim.log[-40:],
                                    "print_time_s": sim.job and round(sim.job["t"])})
        if u.path == "/sim/runout":
            sim.runout(int(q.get("slot", 1)))
        elif u.path == "/sim/load":
            with sim.lock:
                sim.d["matlStationInfo"]["slotInfos"][int(q["slot"]) - 1].update(
                    hasFilament=True, materialColor=q.get("colour", "#000000").upper(),
                    materialName=q.get("material", "PLA"))
            sim.note(f"loaded slot {q['slot']}")
        elif u.path == "/sim/offline":
            sim.offline_until = time.time() + float(q.get("seconds", 30))
            sim.note(f"offline for {q.get('seconds', 30)} s")
        elif u.path == "/sim/camquiet":
            Cam.quiet_until = time.time() + float(q.get("seconds", 30))
            sim.note(f"camera quiet for {q.get('seconds', 30)} s")
        elif u.path == "/sim/finish":
            with sim.lock:
                if sim.job and sim.d["status"] in ("printing", "pause"):
                    sim.job["t"] = sim.job["plan"]["total"] + sim.job["lead"]
                    sim.d["status"] = "printing"
        elif u.path == "/sim/idlecut":
            sim.idle_cut = float(q.get("seconds", 0))
        elif u.path == "/sim/status":
            with sim.lock:
                sim.d["status"] = q.get("s", "ready")
        elif u.path == "/sim/speed":
            sim.speed = float(q.get("x", sim.speed))
        else:
            return reply(self, {"code": 1, "message": "not found"}, 404)
        return reply(self, {"code": 0, "message": "Success"})

    def do_POST(self):
        if self.gone():
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        sim, p = self.sim, urlparse(self.path).path
        if p == "/uploadGcode":
            if (self.headers.get("serialNumber"), self.headers.get("checkCode")) != (SERIAL, CHECK):
                return reply(self, {"code": 1, "message": "serial number or check code wrong"})
            try:
                name, data = multipart_file(body, self.headers.get("Content-Type"))
            except ValueError as e:
                return reply(self, {"code": 1, "message": str(e)})
            if str(len(data)) != self.headers.get("fileSize"):
                return reply(self, {"code": 1, "message": f"fileSize {self.headers.get('fileSize')} != {len(data)}"})
            name = os.path.basename(name)
            path = os.path.join(STORE, name)
            open(path, "wb").write(data)
            with sim.lock:
                sim.files[name] = path
            sim.note(f"upload {name} {len(data)} bytes, tools {self.headers.get('gcodeToolCnt')}, "
                     f"printNow {self.headers.get('printNow')}")
            if self.headers.get("printNow") == "true":
                return reply(self, sim.start(name, self.headers.get("levelingBeforePrint") == "true"))
            return reply(self, {"code": 0, "message": "Success"})
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            return reply(self, {"code": 1, "message": "bad json"})
        if (req.get("serialNumber"), req.get("checkCode")) != (SERIAL, CHECK):
            return reply(self, {"code": 1, "message": "serial number or check code wrong"})
        if p == "/detail":
            return reply(self, {"code": 0, "message": "Success", "detail": sim.detail()})
        if p == "/printGcode":
            return reply(self, sim.start(req.get("fileName", ""), bool(req.get("levelingBeforePrint")),
                                         req.get("materialMappings")))
        if p == "/control":
            pl = req.get("payload") or {}
            cmd, args = pl.get("cmd"), pl.get("args") or {}
            if cmd == "jobCtl_cmd":
                return reply(self, sim.job_ctl(args.get("action")))
            if cmd == "stateCtrl_cmd" and args.get("action") == "setClearPlatform":
                return reply(self, sim.clear_platform())
            if cmd == "temperatureCtl_cmd":
                with sim.lock:
                    noz = args.get("nozzles")
                    if isinstance(noz, list) and len(noz) == 4:  # the firmware skips anything but exactly 4
                        for i, v in enumerate(noz):
                            if v != -200:
                                sim.d["nozzleTargetTemps"][i] = 0 if v == -100 else v
                    if args.get("platform", -200) != -200:
                        sim.d["platTargetTemp"] = 0 if args["platform"] == -100 else args["platform"]
                    if args.get("chamber", -200) != -200:
                        sim.d["chamberTargetTemp"] = 0 if args["chamber"] == -100 else args["chamber"]
                    sim.temp_at = time.time()
                sim.note(f"temperature {args}")
                return reply(self, {"code": 0, "message": "Success"})
            if cmd == "printerCtl_cmd":
                with sim.lock:
                    if args.get("speed", -200) != -200:
                        sim.d["printSpeedAdjust"] = args["speed"]
                sim.note(f"printer control {args}")
                return reply(self, {"code": 0, "message": "Success"})
            if cmd == "lightControl_cmd" and args.get("status") in ("open", "close"):
                with sim.lock:
                    sim.d["lightStatus"] = args["status"]
                sim.note(f"light {args['status']}")
                return reply(self, {"code": 0, "message": "Success"})
            return reply(self, {"code": 1, "message": f"unknown command {cmd}"})
        return reply(self, {"code": 1, "message": "not found"}, 404)


# ---------- camera (:8080) ----------

class Cam(BaseHTTPRequestHandler):
    frames, fps, multi, quiet_until = [], 1.3, False, 0.0
    busy = threading.Semaphore(1)

    def log_message(self, *a):
        pass

    def do_GET(self):
        snap = "action=snapshot" in self.path
        if not self.frames:
            return reply(self, b"no frames", 503, "text/plain")
        if snap:
            return reply(self, self.frames[int(time.time()) % len(self.frames)], 200, "image/jpeg")
        if not self.multi and not self.busy.acquire(blocking=False):
            self.close_connection = True  # the real camera serves one viewer at a time
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=boundarydonotcross")
            self.end_headers()
            i = 0
            while True:
                while time.time() < Cam.quiet_until:
                    time.sleep(0.2)
                f = self.frames[i % len(self.frames)]
                self.wfile.write(b"--boundarydonotcross\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(f)).encode() + b"\r\n\r\n" + f + b"\r\n")
                self.wfile.flush()
                i += 1
                time.sleep(1 / self.fps)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            if not self.multi:
                self.busy.release()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=60, help="print-time seconds per real second")
    ap.add_argument("--host", default="127.0.0.1", help="loopback address to answer on (127.0.0.3 keeps clear of another fake printer)")
    ap.add_argument("--api-port", type=int, default=8898)
    ap.add_argument("--cam-port", type=int, default=8080)
    ap.add_argument("--frames", default=os.path.join(HERE, "..", "watch"), help="folder of camera JPEGs to loop")
    ap.add_argument("--multi-cam", action="store_true", help="let more than one viewer watch the camera")
    a = ap.parse_args()
    sim = Sim(a.speed)
    Api.sim = sim
    # read once: a running hub's watcher deletes old frames from watch\ while a test runs
    Cam.frames = [open(f, "rb").read() for f in sorted(glob.glob(os.path.join(a.frames, "*.jpg")))]
    Cam.frames = Cam.frames or [base64.b64decode(SIM_FRAME)]
    Cam.multi = a.multi_cam
    threading.Thread(target=sim.run, daemon=True).start()
    for port, handler in ((a.cam_port, Cam), (a.api_port, Api)):
        srv = ThreadingHTTPServer((a.host, port), handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    sim.note(f"fake Creator 5 Pro on {a.host}:{a.api_port} (camera :{a.cam_port}, {len(Cam.frames)} frames), "
             f"serial {SERIAL} check code {CHECK}, speed x{a.speed:g}")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
