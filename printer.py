# Creator 5 Pro LAN client (HTTP API on :8898, camera MJPEG on :8080). Stdlib only.
# Wire format taken from @ghosttypes/ff-api 2.1.0 (Creator5Backend / JobControl), checked against our printer.
# The control API answers only in LAN Only mode; the camera answers in either mode.
import json, os, threading, time, urllib.request, uuid


class Printer:
    def __init__(self, ip, serial="", check_code=""):
        self.ip, self.serial, self.check = ip, serial, check_code
        # One command at a time, like FlashForge's own clients. Status reads and uploads stay outside the lock
        # (as in ff-5mp-api-py), so a long upload never holds up a pause or cancel and status keeps coming.
        self.lock = threading.Lock()

    def _post(self, path, body, timeout=8, locked=True):
        req = urllib.request.Request(f"http://{self.ip}:8898{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        if not locked:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode(errors="ignore") or "{}")
        with self.lock, urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode(errors="ignore") or "{}")

    def _auth(self):
        return {"serialNumber": self.serial, "checkCode": self.check}

    @staticmethod
    def ok(reply):
        return isinstance(reply, dict) and reply.get("code") == 0

    def detail(self):
        return self._post("/detail", self._auth(), locked=False)

    def control(self, cmd, args):
        return self._post("/control", {**self._auth(), "payload": {"cmd": cmd, "args": args}})

    def job(self, action):  # pause | continue | cancel
        return self.control("jobCtl_cmd", {"jobID": "", "action": action})

    def clear_platform(self):  # the "plate cleared" confirmation after a finished print
        return self.control("stateCtrl_cmd", {"action": "setClearPlatform"})

    def light(self, on):
        return self.control("lightControl_cmd", {"status": "open" if on else "close"})

    # Temperatures and speed, from Parallel-7/flashforge-api-docs endpoints_creator5_1.9.2.yaml. -200 = no change.
    # The C5 drives tool temperatures only through the 4-entry nozzles array; the left/right scalars are ignored.
    def temperature(self, tool=None, temp=None, bed=None):
        nozzles = [-200] * 4
        if tool:
            nozzles[tool - 1] = int(temp)
        return self.control("temperatureCtl_cmd", {"platform": -200 if bed is None else int(bed),
                                                   "chamber": -200, "nozzles": nozzles})

    def speed(self, pct):
        return self.control("printerCtl_cmd", {"zAxisCompensation": -200.0, "speed": int(pct),
                                               "chamberFan": -200, "coolingFan": -200})

    def upload(self, path, name, tools, leveling):
        """Upload without starting (start is a separate /printGcode so the tool mapping applies)."""
        data = open(path, "rb").read()
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"gcodeFile\"; filename=\"{name}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        headers = {"serialNumber": self.serial, "checkCode": self.check, "fileSize": str(len(data)),
                   "printNow": "false", "levelingBeforePrint": str(bool(leveling)).lower(),
                   "flowCalibration": "false", "timeLapseVideo": "false", "useMatlStation": "true",
                   "gcodeToolCnt": str(max(1, tools)), "Content-Type": f"multipart/form-data; boundary={boundary}"}
        req = urllib.request.Request(f"http://{self.ip}:8898/uploadGcode", data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode(errors="ignore") or "{}")

    def start(self, name, leveling, mappings=None):
        body = {**self._auth(), "fileName": name, "levelingBeforePrint": bool(leveling),
                "flowCalibration": False, "timeLapseVideo": False}
        if mappings:
            body["materialMappings"] = mappings
        return self._post("/printGcode", body, timeout=15)


class Camera:
    """One upstream MJPEG reader shared by every viewer and the print watcher; stops when nobody needs it."""

    FRESH_AFTER = 2  # frames a new stream sends before its picture is current

    def __init__(self, ip=None, url=None, fps=0, wake=20):
        # url: any MJPEG stream (the phone webcam). fps caps what is passed on; the phone sends ~58 fps.
        self.url = url or f"http://{ip}:8080/?action=stream"
        self.gap = 1.0 / fps if fps else 0.0
        self.wake = wake  # seconds to wait for a first frame from a cold stream (the printer's can take >10 s)
        self.frame, self.frame_time, self.users = None, 0.0, 0
        self.seq = 0  # frames since the upstream stream opened
        self.cv = threading.Condition()
        self.thread = None

    def acquire(self):
        with self.cv:
            self.users += 1
            if not (self.thread and self.thread.is_alive()):
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def release(self):
        with self.cv:
            self.users = max(0, self.users - 1)

    def wait_frame(self, after=0.0, timeout=10):
        with self.cv:
            self.cv.wait_for(lambda: self.frame_time > after, timeout)
            return self.frame, self.frame_time

    def snapshot(self, timeout=20):
        # Only a frame received after this call. An idle camera can take >10 s to wake, and handing back the
        # remembered frame made a cleared plate look occupied (2026-10-25 22:39). A freshly opened printer stream
        # also starts with a frame from before it went idle (a lid shown on the plate 25 min after it was taken
        # off, 2026-10-29 19:35), so the first FRESH_AFTER frames of a new stream are skipped.
        asked = time.time()
        self.acquire()
        try:
            with self.cv:
                self.cv.wait_for(lambda: self.frame_time > asked and self.seq > self.FRESH_AFTER, timeout)
                ok = self.frame_time > asked and self.seq > self.FRESH_AFTER
                return self.frame if ok else None
        finally:
            self.release()

    def _run(self):
        idle_since = None
        while True:
            with self.cv:
                if self.users == 0:
                    idle_since = idle_since or time.time()
                    if time.time() - idle_since > 20:
                        self.thread = None
                        self.frame, self.frame_time = None, 0.0  # never serve a frame from a closed stream
                        return
                    self.cv.wait(1)
                    continue
                idle_since = None
            try:
                with urllib.request.urlopen(self.url, timeout=10) as s:
                    with self.cv:
                        self.seq = 0
                    buf = b""
                    while True:
                        chunk = s.read1(65536)  # whatever has arrived; read() waits for a full chunk and adds lag
                        if not chunk:
                            break
                        buf += chunk
                        frame = None
                        while True:  # take every complete frame, publish the newest
                            a = buf.find(b"\xff\xd8")
                            b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                            if a < 0 or b < 0:
                                break
                            frame, buf = buf[a:b + 2], buf[b + 2:]
                        if frame and time.time() - self.frame_time >= self.gap:
                            with self.cv:
                                self.frame, self.frame_time = frame, time.time()
                                self.seq += 1
                                self.cv.notify_all()
                        elif len(buf) > 2_000_000:
                            buf = b""
                        with self.cv:
                            if self.users == 0:
                                break
            except Exception:
                time.sleep(3)
