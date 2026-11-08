# OpenAI-compatible watcher backend against a local mock server: the request shape, a good verdict, a garbled reply,
# an odd verdict, HTTP errors, a timeout, a model with no image support, and the fast / strong model choice.
# No network, no real AI. Run: python -I emulator/test_openai.py
import base64, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import mock_openai, watcher

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed, failed = passed + bool(ok), failed + (not ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""), flush=True)


def main():
    srv, url = mock_openai.start()
    img = os.path.join(tempfile.mkdtemp(), "frame.jpg")
    open(img, "wb").write(b"\xff\xd8fake-jpeg\xff\xd9")
    ask = lambda model, key="", timeout=10, **k: watcher.ask_openai(img, url, key, model, "Print context: printing.", timeout, **k)

    v = ask("good", key="test-key")
    check("good verdict read from a ```json block", v.get("verdict") == "ok" and v.get("part") == "seen", v)
    L = mock_openai.LAST
    parts = L["body"]["messages"][1]["content"]
    check("POST to <base>/chat/completions", L["path"] == "/v1/chat/completions", L["path"])
    check("key sent as Bearer", L["headers"].get("Authorization") == "Bearer test-key", L["headers"].get("Authorization"))
    check("frame sent as a base64 data URI image_url",
          parts[0]["type"] == "image_url" and parts[0]["image_url"]["url"]
          == "data:image/jpeg;base64," + base64.b64encode(open(img, "rb").read()).decode(), parts[0])
    check("same watcher prompt as Claude", L["body"]["messages"][0]["content"] == watcher.PROMPT.replace("{view}", watcher.VIEWS["printer"]))
    check("context goes with the frame", parts[-1]["type"] == "text" and "Print context: printing." in parts[-1]["text"], parts[-1])

    ask("good", zoom=img)
    check("zoomed close-up sent as a second image", [p["type"] for p in mock_openai.LAST["body"]["messages"][1]["content"]]
          == ["image_url", "image_url", "text"])
    ask("good")
    check("no Authorization header without a key (local servers)", "Authorization" not in mock_openai.LAST["headers"])

    v = ask("garbled")
    check("garbled reply = unsure", v.get("verdict") == "unsure" and "not a verdict" in v.get("reason", ""), v)
    v = ask("oddverdict")
    check("verdict outside ok/suspect/fail = unsure", v.get("verdict") == "unsure", v)
    v = ask("nochoices")
    check("reply with no choices = unsure", v.get("verdict") == "unsure", v)
    v = ask("http500")
    check("HTTP 500 = unreachable error", v.get("verdict") == "error" and v["reason"].startswith("unreachable: HTTP 500"), v)
    v = ask("slow", timeout=1)
    check("timeout = unreachable error", v.get("verdict") == "error" and v["reason"].startswith("unreachable"), v)
    v = ask("noimage")
    check("model without image support: plain error", v.get("verdict") == "error" and v.get("no_images")
          and "does not take images" in v["reason"], v)
    v = watcher.ask_openai(img, "http://127.0.0.1:9/v1", "", "good", "", 3)
    check("server not running = unreachable error", v.get("verdict") == "error" and v["reason"].startswith("unreachable"), v)

    # backend choice and model routing inside the watcher
    cfg = {"watch_backend": "openai", "openai_base_url": url, "openai_model": "good", "openai_strong_model": ""}
    check("backend 'openai' needs an address and a model", watcher.backend(cfg) == "openai"
          and watcher.backend(dict(cfg, openai_model="")) == "")
    w = watcher.Watcher.__new__(watcher.Watcher)
    w.cfg, w.claude, w.dir = cfg, "claude", "."
    w.ask(img, "", "haiku", "printer")
    check("first look uses the fast model", mock_openai.LAST["body"]["model"] == "good")
    w.ask(img, "", "sonnet", "printer", strong=True)
    check("re-check with no strong model reuses the fast one", mock_openai.LAST["body"]["model"] == "good")
    cfg["openai_strong_model"] = "oddverdict"
    v = w.ask(img, "", "sonnet", "printer", strong=True)
    check("re-check uses the strong model when set", mock_openai.LAST["body"]["model"] == "oddverdict", mock_openai.LAST["body"]["model"])

    # an 'unsure' re-check alerts once, never pauses
    paused = []
    w.pr = type("P", (), {"job": lambda s, a: paused.append(a) or {"code": 0}, "ok": lambda s, r: True})()
    w.log, pushes = (lambda m: None), []
    w.notify = lambda m, p="high": pushes.append(m)
    w.state = {"fails": 0, "flags": 0, "unsure_told": False, "event": None, "last": None}
    w.last_frame = (img, "", "printer", None)
    for _ in range(3):
        w.decide({"file": "x.gcode", "layer": 5}, {"verdict": "suspect", "reason": "maybe"})
    check("3 flags + unsure re-check: one alert, no pause", not paused and len(pushes) == 1, f"{paused} {pushes}")
    srv.shutdown()
    print(f"\n{passed}/{passed + failed} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
