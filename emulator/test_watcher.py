# Watcher escalation rule (2026-10-29): Haiku every check, a quick Haiku re-check after a flag, Sonnet only when an
# action is due. Scripted verdicts, no camera, no Claude, no printer.
# Run: python -I emulator/test_watcher.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watcher

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed, failed = passed + bool(ok), failed + (not ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))


class Printer:
    def __init__(self): self.paused = 0
    def job(self, a): self.paused += a == "pause"; return {"code": 0}
    def ok(self, r): return r.get("code") == 0


def make(second_says, cfg=None):
    w = watcher.Watcher.__new__(watcher.Watcher)
    w.cfg = dict({"watch_every_min": 2, "watch_action": "pause", "watch_confirm_model": "sonnet"}, **(cfg or {}))
    w.pr, w.log, w.pushes, w.judged = Printer(), lambda m: None, [], []
    w.notify = lambda m, p="high": w.pushes.append(m)
    w.state = {"fails": 0, "flags": 0, "unsure_told": False, "event": None, "last": None}
    w.last_frame = ("frame.jpg", "", "printer", None)
    answers = list(second_says)
    def fake_ask(img, claude, cwd, context="", model="haiku", **k):
        w.judged.append(model)
        return {"verdict": answers.pop(0) if answers else "ok", "reason": f"{model} says", "cost": 0.011}
    watcher.ask_claude = fake_ask
    w.claude, w.dir = "claude", "."
    return w


def run(w, haiku):
    delays = []
    for h in haiku:
        v = {"verdict": h, "reason": "haiku says", "cost": 0.0005, "image": "frame.jpg"}
        w.state["last"] = v
        delays.append(w.decide({"file": "x.gcode", "layer": 5}, v))
    return delays


w = make([]); d = run(w, ["ok"] * 10)
check("clean print: no Sonnet, no push, 2 min cadence", not w.judged and not w.pushes and set(d) == {120})

w = make([]); d = run(w, ["fail", "ok"])
check("one Haiku fail: quick 45 s re-check, no Sonnet", d[0] == 45 and not w.judged and not w.pr.paused, str(d))

w = make([]); d = run(w, ["suspect", "ok", "suspect", "ok"])
check("single suspects with ok between: 90 s re-check, no Sonnet", d[0] == 90 and not w.judged, str(d))

w = make(["fail"]); run(w, ["fail", "fail"])
check("2 Haiku fails + Sonnet fail: paused, 1 Sonnet call", w.pr.paused == 1 and w.judged == ["sonnet"] and w.pushes, str(w.judged))

w = make(["ok"]); run(w, ["fail", "fail", "ok"])
check("2 Haiku fails + Sonnet ok: false alarm, no pause, no push", w.pr.paused == 0 and not w.pushes)

w = make(["error"]); run(w, ["fail", "fail"])
check("2 Haiku fails + Sonnet unreachable: pause anyway (safe side)", w.pr.paused == 1 and "second check failed" in w.pushes[0], str(w.pushes))

w = make(["fail"]); run(w, ["fail", "suspect", "fail"])
check("fail, suspect, fail still counts as 2 fails", w.pr.paused == 1)

w = make(["suspect", "suspect"]); run(w, ["suspect"] * 6)
check("only a Sonnet FAIL pauses: suspects all print long give one unsure push, never a pause (2026-10-30)",
      w.pr.paused == 0 and len(w.pushes) == 1, str(w.pushes))

# rings x3 8daa 2026-10-30 (black PLA, false pause at layer 95): Haiku suspect x11 then fails; Sonnet said suspect each time
w = make(["suspect"] * 6); run(w, ["suspect"] * 11 + ["fail", "fail", "suspect", "fail"])
check("rings 8daa replay (Sonnet suspect, never fail): no pause", w.pr.paused == 0, str(w.pushes))

w = make(["fail"]); run(w, ["suspect", "suspect", "suspect"])
check("3 suspects + Sonnet fail: paused", w.pr.paused == 1)

# the failed lid 2c4d, Haiku verdicts 04:00-04:29: Sonnet once, pause on the last frame (as it did on the night)
lid = list("ooofoooooossoff")
w = make(["fail"]); run(w, [{"o": "ok", "s": "suspect", "f": "fail"}[c] for c in lid])
check("lid 2c4d replay: 1 Sonnet call, paused at the last frame", w.judged == ["sonnet"] and w.pr.paused == 1, str(w.judged))

w = make([], {"watch_confirm_model": ""}); run(w, ["fail", "fail"])
check("no second model set: Haiku's own verdict decides (public install)", w.pr.paused == 1 and not w.judged)

w = make(["fail"], {"watch_action": "alert"}); run(w, ["fail", "fail"])
check("watch_action alert: push, no pause", w.pr.paused == 0 and "Looks like a failure" in w.pushes[0])

# printer cam zoom (camera fit 2026-10-30): the plaque (77-179 mm square, layer 13) gets a close-up; a part that fills
# the plate gets none; the drone base near corner (10, 15) at 24 mm lands where it was marked on the frame
box = watcher.zoom_box({"part": [77, 179, 77, 179], "tower": None}, 2.1)
check("zoom box: small plaque gets a close-up inside the frame", box and 0 <= box[0] < box[2] <= 640 and 0 <= box[1] < box[3] <= 480
      and (box[2] - box[0]) * (box[3] - box[1]) < 0.3 * 640 * 480, str(box))
check("zoom box: whole-plate part gets none", watcher.zoom_box({"part": [5, 251, 2, 250], "tower": None}, 30) is None)
u, v = watcher.to_pixel(10, 15, -24.25)
check("camera fit: drone base near corner within 25 px of the marked spot", abs(u - 320) < 25 and abs(v - 277) < 25, f"{u:.0f},{v:.0f}")

# context: no sizes, colours kept, dark warning (2026-10-30)
sc = {"z": [0.25, 0.45, 0.65, 0.85, 1.05], "part": [79, 177, 79, 177], "tower": [200, 235, 200, 235], "tools": [[0]] * 5}
st = {"layer": 5, "slots": [{"slot": 1, "colour": "#1B1B1B"}, {"slot": 2, "colour": "#FFF245"}]}
ctx = watcher.describe(sc, st, lambda h: {"#1B1B1B": "black", "#FFF245": "yellow"}[h])
check("context has no mm, sizes or layer counts", "mm" not in ctx and "tall" not in ctx and " of " not in ctx, ctx)
check("context names the colour and warns dark on dark", "black" in ctx and "dark colour" in ctx, ctx)
sc2 = dict(sc, tools=[[1]] * 5)
check("yellow part: no dark warning", "dark colour" not in watcher.describe(sc2, st, lambda h: "c"))
check("dark(): black yes, yellow and red no", watcher.dark("#1B1B1B") and not watcher.dark("#FFF245") and not watcher.dark("#F82D29"))
check("prompt no longer asks for size judgement", "much smaller than expected" not in watcher.PROMPT and "Do not judge the size" in watcher.PROMPT)

print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
