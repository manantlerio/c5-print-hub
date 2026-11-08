# Drying mode: spools sit on the bed, the hub holds bed + chamber heat for a set time, and the hub is locked
# (no Print, no temperature changes) until a camera check says the bed is empty again.
# The hub never moves anything: the printer's LAN API has no move command, and any print file starts by homing the
# bed up to the nozzle, which would crush the spools. You park the toolhead (back corner) and lower the bed from the
# printer screen BEFORE the spools go in; a jog after that asks to home first.
# Fw 2.0.0 drops idle heat after 11-14 min, and a re-send did not always reset that timer (2026-10-28), so the heat is
# re-sent every 5 min and again within one status poll of any drop.
import json, os, threading, time

PRESETS = {"petg": {"label": "PETG", "bed": 65, "chamber": 65, "hours": 8},   # Elegoo Rapid PETG label: 65 C 8 h
           "tpu": {"label": "TPU", "bed": 70, "chamber": 65, "hours": 8},     # Elegoo TPU 95A label: 70 C 8 h; chamber max 65
           "pla": {"label": "PLA", "bed": 50, "chamber": 50, "hours": 6},
           "both": {"label": "PETG + PLA", "bed": 65, "chamber": 50, "hours": 8}}
LIMITS = {"bed": (30, 70), "chamber": (0, 65), "hours": (0.5, 12)}  # 70 = Elegoo PETG Vicat softening
LOADED_CHAMBER_MAX = 45   # PLA (or unknown) in the heads softens from ~55 C
LOADED_SAFE = ("PETG",)   # any material name containing these; PETG in the heads takes the full 65 chamber (softens ~80 C; its own drying temp)
REFRESH_S = 300
IDLE = ("ready", "completed", "cancel")
CHECK_PROMPT = ("You check a 3D printer camera frame before the printer is allowed to move again. Filament spools were "
                "drying on the build plate. {view}Answer ONLY one line of JSON: {\"verdict\": \"empty\"|\"not_empty\"|"
                "\"unsure\", \"reason\": \"short plain reason\"}. empty: the build plate is clearly in view and nothing "
                "is on it - no spool, box, bag, tool, part or loose filament. The toolhead, gantry, cables and the plate's "
                "own texture or marks are normal. not_empty: anything sits on the plate. unsure: dark, blurred, blocked or "
                "you cannot see the whole plate. When in doubt answer unsure.")


class Dryer:
    def __init__(self, cfg, printer, summary, log, notify, cams, judge, folder):
        """cams: [(view name, camera, wake s)], best first. judge(image path, prompt, view) -> {"verdict", "reason"}."""
        self.cfg, self.pr, self.summary, self.log, self.notify = cfg, printer, summary, log, notify
        self.cams, self.judge, self.dir = cams, judge, folder
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "drying.json")
        self.lock = threading.Lock()
        self.st = {"phase": "off"}
        if os.path.exists(self.path):
            try:
                self.st = json.load(open(self.path))
            except Exception as e:
                self.log(f"drying state unreadable, starting off: {e}")
        self.checking = False

    # ---- state ----
    def save(self):
        json.dump(self.st, open(self.path + ".tmp", "w"), indent=1)
        os.replace(self.path + ".tmp", self.path)

    def locked(self):
        return self.st.get("phase") in ("drying", "bedcheck")

    def public(self):
        with self.lock:
            p = dict(self.st)
        if p.get("phase") == "drying":
            p["left_s"] = max(0, round(p["end"] - time.time()))
        p["checking"] = self.checking
        p["presets"] = PRESETS
        return p

    def refuse(self):
        """Why the hub must not send or heat anything now (None = allowed)."""
        ph = self.st.get("phase")
        if ph == "drying":
            return "drying mode is on - stop it and check the bed first"
        if ph == "bedcheck":
            return "drying finished but the bed check has not passed - take the spools out and tap Check bed"
        return None

    def heat(self, bed, chamber):
        return self.pr.control("temperatureCtl_cmd", {"platform": int(bed), "chamber": int(chamber), "nozzles": [-200] * 4})

    # ---- actions from the phone ----
    def start(self, preset, bed=None, chamber=None, hours=None, confirm=None):
        need = ("parked", "bed_down", "spools", "door")
        if not all((confirm or {}).get(k) for k in need):
            raise ValueError("tick all four checks first")
        p = dict(PRESETS.get(preset) or {"label": "Custom", "bed": 0, "chamber": 0, "hours": 0})
        for k, v in (("bed", bed), ("chamber", chamber), ("hours", hours)):
            if v not in (None, ""):
                p[k] = float(v)
        lim = dict(LIMITS, hours=(float(self.cfg.get("dry_hours_min", LIMITS["hours"][0])), LIMITS["hours"][1]))
        for k, (lo, hi) in lim.items():
            if not lo <= float(p[k]) <= hi:
                raise ValueError(f"{k} {p[k]} is outside {lo}-{hi}")
        with self.lock:
            if self.locked():
                raise ValueError(self.refuse())
            s = self.summary()
            if not s.get("online"):
                raise ValueError(f"printer not reachable: {s.get('error')}")
            st = (s.get("status") or "").lower()
            if st not in IDLE:
                raise ValueError(f"printer is {st} - drying only starts on an idle printer")
            note = None
            loaded = [x["slot"] for x in s.get("slots") or [] if x.get("loaded")
                      and not any(m in (x.get("material") or "").upper() for m in LOADED_SAFE)]
            if loaded and p["chamber"] > LOADED_CHAMBER_MAX:
                note = f"slot {', '.join(map(str, loaded))} loaded: chamber capped at {LOADED_CHAMBER_MAX}"
                p["chamber"] = LOADED_CHAMBER_MAX
            r = self.heat(p["bed"], p["chamber"])
            if not self.pr.ok(r):
                raise ValueError(f"printer refused the heat: {r}")
            now = time.time()
            self.st = {"phase": "drying", "label": p["label"], "bed": int(p["bed"]), "chamber": int(p["chamber"]),
                       "hours": float(p["hours"]), "start": now, "end": now + float(p["hours"]) * 3600, "sent": now,
                       "drops": 0, "unheld_since": None, "note": note, "started": time.strftime("%Y-%m-%d %H:%M"),
                       "ends": time.strftime("%H:%M", time.localtime(now + float(p["hours"]) * 3600)), "check": None}
            self.save()
        self.log(f"drying start {self.st}")
        self.notify(f"Drying {p['label']} started: bed {int(p['bed'])}, chamber {int(p['chamber'])}, "
                    f"until {self.st['ends']}" + (f" ({note})" if note else ""), "default")
        return self.public()

    def stop(self, why="stopped from the phone"):
        with self.lock:
            if self.st.get("phase") != "drying":
                raise ValueError("not drying")
            s = self.summary()
            idle = (s.get("status") or "").lower() in IDLE if s.get("online") else True
            r = self.heat(0, 0) if idle else self.heat(-200, 0)
            self.st.update(phase="bedcheck", stopped=time.strftime("%Y-%m-%d %H:%M"), why=why)
            self.save()
        self.log(f"drying ended ({why}): heat off {r}")
        self.notify(f"Drying done ({why}). Take the spools out, then tap Check bed in C5 Print Hub.", "default")
        return self.public()

    def resume(self, hours=None, confirm=None):
        """Keep drying after a run ended (timer or Stop) with the spools still on the bed: same bed, chamber and label,
        a new number of hours. Refused after a print start, because the bed may have moved."""
        need = ("parked", "spools", "door")
        if not all((confirm or {}).get(k) for k in need):
            raise ValueError("confirm the head is still parked, the spools are still on the bed and the door is shut")
        with self.lock:
            if self.st.get("phase") != "bedcheck":
                raise ValueError("only after a drying run has ended")
            why = self.st.get("why") or ""
            if why.startswith("printer went"):
                raise ValueError(f"drying ended because the {why} - check the bed first, then start a new run")
            h = float(hours) if hours not in (None, "") else float(self.st.get("hours") or 0)
            lo, hi = float(self.cfg.get("dry_hours_min", LIMITS["hours"][0])), LIMITS["hours"][1]
            if not lo <= h <= hi:
                raise ValueError(f"hours {h} is outside {lo}-{hi}")
            s = self.summary()
            if not s.get("online"):
                raise ValueError(f"printer not reachable: {s.get('error')}")
            st = (s.get("status") or "").lower()
            if st not in IDLE:
                raise ValueError(f"printer is {st} - drying only runs on an idle printer")
            bed, chamber, note = int(self.st["bed"]), int(self.st["chamber"]), self.st.get("note")
            loaded = [x["slot"] for x in s.get("slots") or [] if x.get("loaded")
                      and not any(m in (x.get("material") or "").upper() for m in LOADED_SAFE)]
            if loaded and chamber > LOADED_CHAMBER_MAX:
                note = f"slot {', '.join(map(str, loaded))} loaded: chamber capped at {LOADED_CHAMBER_MAX}"
                chamber = LOADED_CHAMBER_MAX
            r = self.heat(bed, chamber)
            if not self.pr.ok(r):
                raise ValueError(f"printer refused the heat: {r}")
            now = time.time()
            runs = int(self.st.get("runs") or 1) + 1
            self.st = {"phase": "drying", "label": self.st.get("label") or "Custom", "bed": bed, "chamber": chamber,
                       "hours": h, "start": now, "end": now + h * 3600, "sent": now, "drops": 0, "unheld_since": None,
                       "note": note, "started": time.strftime("%Y-%m-%d %H:%M"),
                       "ends": time.strftime("%H:%M", time.localtime(now + h * 3600)), "check": None, "runs": runs}
            self.save()
        self.log(f"drying resumed (run {runs}) {self.st}")
        self.notify(f"Drying {self.st['label']} continued: bed {bed}, chamber {chamber}, until {self.st['ends']}"
                    + (f" ({note})" if note else ""), "default")
        return self.public()

    def unlock(self, why):
        with self.lock:
            if self.st.get("phase") != "bedcheck":
                raise ValueError("nothing to unlock")
            self.st.update(phase="off", unlocked=time.strftime("%Y-%m-%d %H:%M"), unlocked_by=why)
            self.save()
        self.log(f"drying lock off: {why}")

    def bed_check(self, all_out=False):
        # Two keys: you tick that every spool and the silica are out, AND every camera frame must
        # be called empty. The printer's own camera sits low at the front (with 5 spools stacked it showed only the
        # bottom one), so it cannot vouch for the whole plate alone; the phone camera is not always running.
        if not all_out:
            raise ValueError("tick 'everything is out' first")
        if self.st.get("phase") != "bedcheck":
            raise ValueError("the bed check comes after drying ends")
        if self.checking:
            raise ValueError("already checking")
        self.checking = True
        try:
            # No wait: the "lag" seen on 2026-10-28 (a spool shown after it was taken out) was a reopened
            # stream's stale first frame, which Camera.snapshot now skips (2026-10-29).
            time.sleep(float(self.cfg.get("dry_check_delay_s", 0)))
            results = []
            for view, cam, wake in self.cams:
                frame = cam.snapshot(wake) if cam else None
                if not frame:
                    results.append({"cam": view, "verdict": "unsure", "reason": "camera not answering"})
                    continue
                name = f"bedcheck-{time.strftime('%Y%m%d-%H%M%S')}-{view}.jpg"
                img = os.path.join(self.dir, name)
                open(img, "wb").write(frame)
                v = self.judge(img, CHECK_PROMPT, view)
                v.update(cam=view, image=name)
                results.append(v)
            seen = [r for r in results if r.get("image")]
            # Real test 2026-10-28: with the bed lowered the printer camera only sees a corner of the plate ("unsure")
            # while the top-down phone camera sees it all ("empty"). A camera that cannot see does not block; one that
            # sees anything on the plate does.
            ok = (any(r.get("verdict") == "empty" for r in seen)
                  and not any(r.get("verdict") not in ("empty", "unsure") for r in seen))

            check = {"time": time.strftime("%H:%M"), "passed": ok, "results": results, "all_out": True}
            with self.lock:
                self.st["check"] = check
                if ok:
                    self.st.update(phase="off", unlocked=time.strftime("%Y-%m-%d %H:%M"), unlocked_by="camera check")
                self.save()
            self.log(f"drying bed check: {check}")
            return self.public()
        finally:
            self.checking = False

    # ---- background ----
    def run(self, every=10):
        while True:
            time.sleep(every)
            try:
                self.tick()
            except Exception as e:
                self.log(f"drying tick failed: {e}")

    def tick(self):
        if self.st.get("phase") != "drying":
            return
        s = self.summary()
        if not s.get("online"):
            return
        st = (s.get("status") or "").lower()
        if st not in IDLE:
            # a print was started from the printer screen or app with the spools on the bed
            r = self.heat(-200, 0)
            with self.lock:
                self.st.update(phase="bedcheck", stopped=time.strftime("%Y-%m-%d %H:%M"), why=f"printer went {st}")
                self.save()
            self.log(f"DRYING: printer went {st} during drying, chamber off {r}")
            self.notify(f"Printer went {st} during drying - were the spools still on the bed? Chamber heat off. "
                        "C5 Print Hub stays locked until the bed check passes.")
            return
        if time.time() >= self.st["end"]:
            self.stop("timer")
            return
        bt, ct = float(s.get("bed_target") or 0), float(s.get("chamber_target") or 0)
        want_b, want_c = self.st["bed"], self.st["chamber"]
        settled = time.time() - self.st["sent"] > 15  # the status poll lags a re-send by a few seconds
        dropped = settled and (bt < want_b - 0.5 or ct < want_c - 0.5)
        if dropped or time.time() - self.st["sent"] >= REFRESH_S:
            r = self.heat(want_b, want_c)
            with self.lock:
                if dropped:
                    self.st["drops"] += 1
                    self.st["unheld_since"] = self.st.get("unheld_since") or time.time()
                self.st["sent"] = time.time()
                self.save()
            if dropped:
                self.log(f"drying: heat had dropped to bed {bt:.0f} chamber {ct:.0f}, sent {want_b}/{want_c} again: {r}")
        elif settled and self.st.get("unheld_since"):
            with self.lock:
                self.st.update(unheld_since=None, unheld_told=False)
                self.save()
        u = self.st.get("unheld_since")
        if u and time.time() - u > 180 and not self.st.get("unheld_told"):
            self.notify(f"Drying: the printer keeps dropping the heat (bed {bt:.0f}, chamber {ct:.0f}) for 3 min")
            with self.lock:
                self.st["unheld_told"] = True
                self.save()
