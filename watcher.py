# Print watcher (optional, off by default): every few minutes during a print, Claude (Haiku) looks at a camera frame, told
# what the G-code should have put on the plate by now. A flag gets a quick Haiku re-check on a fresh frame (45 s after
# a fail, 90 s after a suspect). Sonnet is only asked when an action is due (2026-10-29: replay of 13 prints, Sonnet
# calls 122 -> 12, $1.79 -> $0.55, both real catches at the same moment):
#   2 Haiku fails in a row -> Sonnet on that frame: fail (or Sonnet unreachable) pauses the print (or only alerts,
#     per config), suspect alerts, ok = false alarm;
#   3 flags with no ok between -> Sonnet decides: fail pauses, suspect alerts once (until the next ok), ok = nothing.
# Only a Sonnet FAIL pauses (2026-10-30): a 'second suspect pauses' rule paused good black prints.
# Backends (config "watch_backend"): "api" = your own Anthropic API key, straight to the Messages API (stdlib urllib,
# no SDK, so the hub stays stdlib only); "cli" = the Claude Code CLI signed in on this PC; "openai" = any server that
# speaks OpenAI Chat Completions (OpenAI, Gemini, OpenRouter, Ollama, LM Studio). The Claude backends are the tested
# ones. The OpenAI-compatible one is experimental: the prompt and the escalation rule were tuned on Claude only.
# No sizes or heights in the context: from the low printer camera the model cannot judge them, and 'much smaller
# than expected' was the main false flag (multi-part plates, black on the dark plate).
# A pause is recoverable; a cancel is not.
import base64, json, os, re, shutil, subprocess, threading, time, urllib.error, urllib.request

# Where the camera looks from: the phone webcam (top-down) when it answers, else the printer's own (low front).
VIEWS = {"printer": "The camera sits low at the front of the bed, so the toolhead often hides a small or low part. ",
         "phone": "The camera looks straight down, printer front at the bottom of the frame. The tool dock at the top is never the print. "
                  "The plate below it is bright gold-brown and, whatever the words Textured PEI Plate on it suggest, has NO honeycomb, grid or "
                  "cells of its own: its only texture is a fine sandpaper speckle. So any honeycomb, grid or rows of cells you see IS the printed "
                  "part, an open lattice whose cells are holes showing plate: part seen, never missing or hidden. A black part stands out here "
                  "even in a dim or shadowed frame, so the low or dark view rule below does not apply. Gantry bar, toolhead and thick blurred "
                  "cables hide only what is under them. If the plate looks plain and the toolhead sits over its middle, a small part may be under "
                  "the head: hidden, ok. If it looks plain and no toolhead is over its middle: missing. Flag thin tangled strands, a lump at the "
                  "nozzle, a piece that moved, a lifted corner. "}
PROMPT = ("You watch a 3D printer camera for failed prints. {view}"
          "Each frame comes with what should be on the plate right now. "
          "Answer ONLY one line of JSON: {\"part\": \"seen\"|\"hidden\"|\"missing\", "
          "\"verdict\": \"ok\"|\"suspect\"|\"fail\", \"reason\": \"short plain reason\"}. "
          "part: seen = you can see the printed part; hidden = the toolhead, glare or darkness could be covering it; "
          "missing = the plate is clearly in view and the part is not on it. "
          "fail only for a clear failure: spaghetti or loose strands, a part knocked over or off the plate, "
          "a large blob around the nozzle, or printing into thin air. "
          "suspect: something looks wrong but not clearly failed, such as a first loose strand or a part that may have moved. "
          "Do not judge the size or height of the part: this camera cannot show it. "
          "On a low or dark view a black or dark part on the dark textured plate is often invisible: not seeing it is NOT a failure, call it hidden, unless you see strands, a blob or something knocked loose. "
          "The prime tower is a second, smaller square column; it is normal only when the context says there is one. "
          "Never call the part a purge or prime tower. Glare and a dark frame are normal. "
          "A second image, when sent, is the same frame zoomed in on where the part should be: look for the part there "
          "and use the first image for the whole plate.")
NO_WINDOW = 0x08000000

# Printer camera (Creator 5 Pro): fixed to the frame, low at the front left, while the bed drops as the print grows.
# Pinhole model fitted 2026-10-30 to saved frames of known parts (twisty cube, drone base, lid, caddy, hair), 10-25 px:
# bed point (x, y mm, h = mm above the nozzle plane, so the plate is at -z) -> pixel in the 640 x 480 frame.
CAM_R = ((0.7012, -0.71188, 0.03934), (-0.17244, -0.22287, -0.95947), (0.6918, 0.666, -0.27904))
CAM_T, CAM_F, CAM_K1 = (1.26, -3.74, 157.19), 489.5, -0.1326


def to_pixel(x, y, h):
    a, b, c = (sum(r * p for r, p in zip(row, (x, y, h))) + t for row, t in zip(CAM_R, CAM_T))
    a, b = a / c, b / c
    d = 1 + CAM_K1 * (a * a + b * b)
    return 320 + CAM_F * a * d, 240 + CAM_F * b * d


def zoom_box(sc, z, w=640, h=480):
    """Pixel box (left, top, right, bottom) around where the part and prime tower should be in a printer cam frame,
    from the plate up to the nozzle, with a margin for the fit. None when that is most of the frame anyway."""
    pts = [to_pixel(x, y, hh) for fp in (sc["part"], sc["tower"]) if fp for x in fp[:2] for y in fp[2:] for hh in (-z, 0)]
    if not pts:
        return None
    u0, u1, v0, v1 = min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts), max(p[1] for p in pts)
    pad = 25 + 0.1 * max(u1 - u0, v1 - v0)
    bw, bh = min(w, max(u1 - u0 + 2 * pad, 200)), min(h, max(v1 - v0 + 2 * pad, 120))
    left = min(max(0, (u0 + u1 - bw) / 2), w - bw)
    top = min(max(0, (v0 + v1 - bh) / 2), h - bh)
    return None if bw * bh > 0.6 * w * h else (round(left), round(top), round(left + bw), round(top + bh))


def zoom_image(img, box, out):
    """Crop a frame to box and scale it up (at most 4x, 1000 px wide), so the model spends its pixels on the part.
    None without Pillow: the check then runs on the whole frame alone."""
    try:
        from PIL import Image
    except ImportError:  # the hub runs isolated (-I); Pillow lives in the user's site-packages, added last
        import site, sys
        sys.path.append(site.getusersitepackages())
        try:
            from PIL import Image
        except ImportError:
            return None
    im = Image.open(img).crop(box)
    k = min(4.0, 1000 / im.width, 750 / im.height)
    im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS).save(out, quality=92)
    return out


# Short names in config.json -> API model ids (the CLI takes the short names as they are). Prices are US$ per million
# tokens, input / output, for the cost line in the watcher log.
MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
API_URL = "https://api.anthropic.com/v1/messages"


def backend(cfg):
    """'api', 'cli', 'openai' or '' (no AI checks: watcher off, drying bed check by eye)."""
    b = (cfg.get("watch_backend") or "").lower()
    if b == "api" and cfg.get("anthropic_api_key"):
        return "api"
    if b == "openai" and cfg.get("openai_base_url") and cfg.get("openai_model"):
        return "openai"
    return "cli" if b == "cli" else ""


VERDICTS = {"ok", "suspect", "fail", "empty", "not_empty", "unsure"}  # watcher prompt + drying bed check prompt
NO_IMAGES = ("image", "vision", "multimodal", "multi-modal")


def _data_uri(f):
    mime = "image/png" if f.lower().endswith(".png") else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(open(f, "rb").read()).decode()


def ask_openai(img, base_url, key, model, context="", timeout=120, view="printer", system=None, zoom=None):
    """One verdict from an OpenAI-compatible server (Chat Completions, images as base64 data URIs).
    Experimental. An answer that is not a verdict = 'unsure'; no answer (HTTP error, timeout) = 'error' (unreachable),
    as for the Claude backends. A model that cannot take images says so in the reason ('no_images': True)."""
    url = base_url.rstrip("/") + "/chat/completions"
    content = [{"type": "image_url", "image_url": {"url": _data_uri(f)}} for f in [img] + ([zoom] if zoom else [])]
    content.append({"type": "text", "text": (context + "\n" if context else "") + "Verdict for this frame?"})
    body = {"model": model, "messages": [{"role": "system", "content": (system or PROMPT).replace("{view}", VIEWS[view])},
                                         {"role": "user", "content": content}]}
    head = {"Content-Type": "application/json"}
    if key:
        head["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=head)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="ignore")[:400]
        try:
            j = json.loads(msg)
            j = j[0] if isinstance(j, list) and j else j  # Gemini can answer with a list
            msg = (j.get("error") or {}).get("message") or j.get("message") or msg if isinstance(j, dict) else msg
        except Exception:
            pass
        if e.code in (400, 404, 415, 422) and any(w in str(msg).lower() for w in NO_IMAGES):
            return {"verdict": "error", "no_images": True,
                    "reason": f"this model does not take images - pick a vision model ({msg})"[:200]}
        return {"verdict": "error", "reason": f"unreachable: HTTP {e.code}: {msg}"[:200]}
    except Exception as e:  # timeout, refused, DNS, bad JSON body
        return {"verdict": "error", "reason": f"unreachable: {e}"[:200]}
    try:
        text = out["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"verdict": "unsure", "reason": f"no answer in the reply: {json.dumps(out)[:150]}", "cost": None}
    if isinstance(text, list):  # some servers send content parts
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    m = re.search(r"\{[^{}]*\"verdict\"[^{}]*\}", text or "")
    try:
        v = json.loads(m.group(0)) if m else None
    except ValueError:
        v = None
    if not isinstance(v, dict) or str(v.get("verdict")).lower() not in VERDICTS:
        return {"verdict": "unsure", "reason": f"answer was not a verdict: {(text or '')[:150]}", "cost": None}
    v["verdict"] = str(v["verdict"]).lower()
    v["cost"] = None  # price unknown for other providers
    return v


def ask_claude(img, claude, cwd, context="", model="haiku", timeout=120, view="printer", system=None, zoom=None,
               api_key=None):
    """One verdict for one frame. api_key set = the Messages API with that key, else the Claude Code CLI at claude.
    system: another prompt with a {view} slot (the drying bed check); the answer must still carry "verdict".
    zoom: a close-up of the same frame (zoom_image), sent as a second image."""
    if api_key:
        return ask_api(img, api_key, context, model, timeout, view, system, zoom)
    return ask_cli(img, claude, cwd, context, model, timeout, view, system, zoom)


def _image(f):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png" if f.lower().endswith(".png") else "image/jpeg",
                                        "data": base64.b64encode(open(f, "rb").read()).decode()}}


def _verdict(text, fallback=None):
    m = re.search(r"\{[^{}]*\"verdict\"[^{}]*\}", text or "")
    try:
        return json.loads(m.group(0)) if m else {"verdict": "error", "reason": (text or fallback or "no answer")[:200]}
    except ValueError:
        return {"verdict": "error", "reason": (text or "")[:200]}


def ask_api(img, key, context="", model="haiku", timeout=120, view="printer", system=None, zoom=None):
    """The Messages API with the user's own key. Low effort: one line of JSON about one picture."""
    mid = MODELS.get(model, model)
    body = {"model": mid, "max_tokens": 2048, "output_config": {"effort": "low"},
            "system": (system or PROMPT).replace("{view}", VIEWS[view]),
            "messages": [{"role": "user", "content": [_image(img)] + ([_image(zoom)] if zoom else []) + [
                {"type": "text", "text": (context + "\n" if context else "") + "Verdict for this frame?"}]}]}
    head = {"content-type": "application/json", "x-api-key": key, "anthropic-version": "2026-10-21"}
    if mid.startswith(("claude-sonnet-5-5", "claude-opus-5-5")):
        # On a safety decline the API re-runs the request on a fallback model it picks. Retried without it on a 400.
        body["fallbacks"] = "default"
        head["anthropic-beta"] = "server-side-fallback-2026-07-01"
    out = {}
    for attempt in (1, 2):
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), headers=head)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="ignore")[:300]
            if e.code == 400 and attempt == 1 and "fallbacks" in body:
                body.pop("fallbacks")
                head.pop("anthropic-beta")
                continue
            try:
                msg = json.loads(msg)["error"]["message"]
            except Exception:
                pass
            return {"verdict": "error", "reason": f"API {e.code}: {msg}"[:200]}
        except Exception as e:
            return {"verdict": "error", "reason": f"API not reachable: {e}"[:200]}
    if out.get("stop_reason") == "refusal":
        return {"verdict": "error", "reason": "the model declined this frame"}
    text = "".join(b.get("text", "") for b in out.get("content") or [] if b.get("type") == "text")
    v = _verdict(text, out.get("stop_reason"))
    u, p = out.get("usage") or {}, PRICES.get(out.get("model") or mid) or PRICES.get(mid)
    v["cost"] = round((u.get("input_tokens", 0) * p[0] + u.get("output_tokens", 0) * p[1]) / 1e6, 6) if p else None
    return v


def ask_cli(img, claude, cwd, context="", model="haiku", timeout=120, view="printer", system=None, zoom=None):
    # Image goes inline, with a short system prompt and no tools, MCP servers, skills or settings:
    # about 1,500 tokens a check, against 68,000 tokens for a default `claude -p`.
    if not claude or not (os.path.exists(claude) or shutil.which(claude)):
        return {"verdict": "error", "reason": "Claude Code CLI not found on this PC"}
    image = lambda f: {"type": "image", "source": {"type": "base64", "media_type": "image/png" if f.lower().endswith(".png") else "image/jpeg",
                                                    "data": base64.b64encode(open(f, "rb").read()).decode()}}
    msg = {"type": "user", "message": {"role": "user", "content": [image(img)] + ([image(zoom)] if zoom else []) + [
        {"type": "text", "text": (context + "\n" if context else "") + "Verdict for this frame?"}]}}
    p = subprocess.run([claude, "-p", "--model", model, "--input-format", "stream-json", "--output-format", "stream-json",
                        "--verbose", "--system-prompt", (system or PROMPT).replace("{view}", VIEWS[view]), "--tools", "", "--strict-mcp-config",
                        "--disable-slash-commands", "--setting-sources", "", "--max-turns", "1"],
                       input=json.dumps(msg) + "\n", capture_output=True, text=True, errors="ignore", timeout=timeout,
                       cwd=cwd, creationflags=NO_WINDOW)
    res = [x for x in (json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")) if x.get("type") == "result"]
    out = res[-1] if res else {}
    v = _verdict(out.get("result"), p.stderr)
    v["cost"] = out.get("total_cost_usd")
    return v


def dark(hex_colour):
    """True for a colour that disappears on the dark textured plate (relative luminance under 0.2)."""
    try:
        r, g, b = (int(hex_colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, AttributeError):
        return False
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.2


def describe(sc, s, colour_name):
    """What should be on the plate now, in words, from slicer.scene() of the running file and the printer status.
    Colours and the prime tower only: no sizes, heights or layer counts (2026-10-30)."""
    layer = s.get("layer") or 0
    text = "Print context: the print has only just started, a thin first layer or two." if layer <= 3 else "Print context: printing."
    if not sc or not sc["z"] or not sc["part"]:
        return text
    i = max(0, min(layer, len(sc["z"])) - 1)
    slot = {x.get("slot"): x.get("colour") for x in s.get("slots") or []}
    name = lambda t: colour_name(slot[t + 1]) if slot.get(t + 1) else f"tool {t + 1}"
    bands = []
    for ts in sc["tools"][:i + 1]:
        b = " + ".join(name(t) for t in ts)
        if b and (not bands or bands[-1] != b):
            bands.append(b)
    if bands:
        text += f" Colours so far, bottom to top: {', '.join(bands[-6:])}."
    used = {t for ts in sc["tools"][:i + 1] for t in ts}
    if used and all(dark(slot.get(t + 1)) for t in used):
        text += " The part is a dark colour on a dark plate, so it may be very hard to see."
    text += (" A prime tower also prints: a second, smaller square column in all the colours used." if sc["tower"]
             else " There is no prime tower on this print.")
    return text


class Watcher:
    def __init__(self, cfg, printer, camera, summary, log, scene=None, colour_name=str, webcam=None):
        self.cfg, self.pr, self.cam, self.summary, self.log = cfg, printer, camera, summary, log
        self.cams = ([("phone", webcam, 8)] if webcam else []) + [("printer", camera, 20)]  # name, cam, wake time s
        # scene(file name) -> slicer.scene() of the running file, so each check says what should be on the plate
        self.scene_of, self.colour_name, self.scene = scene, colour_name, None
        self.dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch")
        self.keep = os.path.join(self.dir, "keep")  # every non-ok frame plus one per 10 min, never deleted
        os.makedirs(self.keep, exist_ok=True)
        self.claude = cfg.get("claude") or shutil.which("claude") or os.path.expanduser(r"~\.local\bin\claude.exe")
        self.state = {"enabled": bool(cfg.get("watch", False)) and bool(backend(cfg)), "last": None, "fails": 0, "event": None, "file": None,
                      "errors": 0, "blind": None}
        self.tally, self.kept_at = {}, 0
        self.boot = time.time()

    def set_enabled(self, on):
        if on and not backend(self.cfg):
            raise ValueError("the watcher is not set up: open Setup - Watcher and choose an AI")
        self.state["enabled"] = bool(on)

    def ask(self, img, context, model, view, zoom=None, system=None, strong=False):
        """model: the Claude model name. strong: the re-check (OpenAI-compatible: the strong model, or the fast one
        when no strong model is set)."""
        b = backend(self.cfg)
        if b == "openai":
            c = self.cfg
            name = (c.get("openai_strong_model") if strong else "") or c.get("openai_model")
            return ask_openai(img, c["openai_base_url"], c.get("openai_api_key") or "", name, context,
                              float(c.get("openai_timeout_s", 120)), view, system, zoom)
        key = self.cfg.get("anthropic_api_key") if b == "api" else None
        return ask_claude(img, self.claude, self.dir, context, model, view=view, system=system, zoom=zoom, api_key=key)

    def notify(self, msg, priority="high"):
        self.log(f"push: {msg}")
        topic = self.cfg.get("ntfy_topic")
        if topic:  # optional phone push via ntfy.sh, off unless a topic is set in config.json
            try:
                urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{topic}", data=msg.encode(),
                                                              headers={"Title": "C5 Print Hub", "Priority": priority}), timeout=10)
            except Exception as e:
                self.log(f"notify failed: {e}")

    def record(self, s, v):
        """Evidence for the watcher review: one line per check, the frames worth keeping, a total per print."""
        keep = v.get("image") and (v["verdict"] != "ok" or time.time() - self.kept_at >= 600)
        if keep and os.path.exists(os.path.join(self.dir, v["image"])):
            shutil.copy(os.path.join(self.dir, v["image"]), os.path.join(self.keep, v["image"]))
            zoom = v["image"][:-4] + "-zoom.jpg"
            if v.get("zoom") and os.path.exists(os.path.join(self.dir, zoom)):
                shutil.copy(os.path.join(self.dir, zoom), os.path.join(self.keep, zoom))
            self.kept_at = time.time() if v["verdict"] == "ok" else self.kept_at
        line = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "file": s.get("file"), "layer": s.get("layer"),
                "layers": s.get("layers"), "verdict": v["verdict"], "part": v.get("part"), "reason": v.get("reason"), "cost": v.get("cost"),
                "image": v.get("image"), "zoom": v.get("zoom"), "kept": bool(keep)}
        with open(os.path.join(self.dir, "checks.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        t = self.tally
        t[v["verdict"]] = t.get(v["verdict"], 0) + 1
        t["cost"] = t.get("cost", 0) + (v.get("cost") or 0)

    def close_print(self, end):
        if self.tally and self.state["file"]:
            t = self.tally
            line = (f"{time.strftime('%Y-%m-%d %H:%M')} {self.state['file']} ({end}): "
                    f"{sum(n for k, n in t.items() if k != 'cost')} checks, ok {t.get('ok', 0)}, "
                    f"suspect {t.get('suspect', 0)}, fail {t.get('fail', 0)}, error {t.get('error', 0)}, "
                    f"${t.get('cost', 0):.4f}")
            with open(os.path.join(self.dir, "prints.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self.log("watch summary " + line)
        self.tally, self.kept_at = {}, 0

    def check_once(self, s=None):
        for view, cam, wake in self.cams:
            frame = cam.snapshot(wake)
            if frame:
                break
        else:
            return {"verdict": "error", "reason": "camera not answering"}
        img = os.path.join(self.dir, time.strftime("%Y%m%d-%H%M%S") + ".jpg")
        open(img, "wb").write(frame)
        shots = sorted(f for f in os.listdir(self.dir) if f.endswith(".jpg"))
        for old in shots[:-120]:  # frame + zoom per check
            os.remove(os.path.join(self.dir, old))
        context = describe(self.scene, s, self.colour_name) if s else ""
        if view == "phone":  # the top-down phone view shows dark parts clearly (2026-11-02)
            context = context.replace(" The part is a dark colour on a dark plate, so it may be very hard to see.", "")
        zoom = box = None
        if view == "printer" and s and self.cfg.get("watch_zoom", True) and self.scene and self.scene["z"] and self.scene["part"]:
            z = s.get("z") or self.scene["z"][max(0, min(s.get("layer") or 1, len(self.scene["z"])) - 1)]
            box = zoom_box(self.scene, z)
            try:
                zoom = box and zoom_image(img, box, img[:-4] + "-zoom.jpg")
            except Exception as e:
                self.log(f"watch zoom failed: {e}")
        v = self.ask(img, context, self.cfg.get("watch_model", "haiku"), view, zoom)
        self.last_frame = (img, context, view, zoom)
        v.update(time=time.strftime("%H:%M"), image=os.path.basename(img), cam=view)
        if zoom:
            v["zoom"] = list(box)
        return v

    def judge_last(self, v):
        """Second opinion on the frame that made an action due. Haiku loses a black part on the black plate a quarter
        of the time (test 2026-10-26: 7 of 28 good frames); Sonnet 3 of 28. No second model set = Haiku's verdict."""
        second = self.cfg.get("watch_confirm_model", "sonnet")
        if backend(self.cfg) == "openai":
            second = second or "strong"  # the OpenAI-compatible re-check always runs (strong model, or the fast one)
        if not second or not getattr(self, "last_frame", None):
            return dict(v)
        img, context, view, zoom = self.last_frame
        try:
            return self.ask(img, context, second, view, zoom, strong=True)
        except Exception as e:
            return {"verdict": "error", "reason": str(e)[:200]}

    def act(self, s, msg_ok, pause):
        if pause and self.cfg.get("watch_action", "pause") == "pause":
            r = self.pr.job("pause")
            msg = (f"Paused {s.get('file')} at layer {s.get('layer')}: {msg_ok}" if self.pr.ok(r)
                   else f"Tried to pause {s.get('file')} but the printer refused: {r}")
        elif pause:
            msg = f"Looks like a failure on {s.get('file')} at layer {s.get('layer')}: {msg_ok}"
        else:
            msg = f"Watcher unsure about {s.get('file')} at layer {s.get('layer')} (3 checks): {msg_ok}"
        self.state["event"] = {"time": time.strftime("%H:%M"), "at": time.time(), "message": msg,
                               "image": self.state["last"].get("image") if self.state.get("last") else None}
        self.log("WATCH " + msg)
        self.notify(msg)

    def decide(self, s, v):
        """Act on one Haiku verdict; returns seconds to the next check."""
        st, every = self.state, 60 * float(self.cfg.get("watch_every_min", 5))
        if v["verdict"] == "error":
            return every
        if v["verdict"] == "ok":
            st.update(fails=0, flags=0, unsure_told=False)
            return every
        st["flags"] = st.get("flags", 0) + 1
        if v["verdict"] == "fail":
            st["fails"] = st.get("fails", 0) + 1
        due_fail = st.get("fails", 0) >= 2
        due_unsure = st["flags"] >= 3 and not st.get("unsure_told")
        if not (due_fail or due_unsure):
            return 45 if v["verdict"] == "fail" else 90  # quick fresh frame before anything else
        j = self.judge_last(v)
        v["judge"] = {k: j.get(k) for k in ("verdict", "part", "reason")}
        if j is not v and self.cfg.get("watch_confirm_model", "sonnet"):
            v["cost"] = (v.get("cost") or 0) + (j.get("cost") or 0)
        st.update(fails=0, flags=0)
        if j["verdict"] == "fail" or (j["verdict"] == "error" and due_fail):
            why = j.get("reason") if j["verdict"] == "fail" else f"{v.get('reason')} (second check failed: {j.get('reason')})"
            self.act(s, why, pause=True)
            return every
        if j["verdict"] in ("suspect", "unsure", "error") and not st.get("unsure_told"):
            st["unsure_told"] = True
            self.act(s, j.get("reason") if j["verdict"] in ("suspect", "unsure") else v.get("reason"), pause=False)
            return 90
        return every  # second opinion says ok: false alarm

    def run(self):
        next_check = 0
        while True:
            time.sleep(15)
            s = self.summary()
            st = (s.get("status") or "").lower()
            if st != "printing":
                self.state["fails"] = 0
                if s.get("online") and self.state["file"] and st in ("ready", "completed", "cancel"):
                    self.close_print(st)
                if s.get("online") and self.state["file"] and st in ("ready", "completed"):
                    self.state.update(file=None, event=None)
                continue
            if s.get("file") != self.state["file"]:  # new print: start fresh
                self.close_print("replaced")
                # a print already running when the hub starts (restart mid-print) is live at once; a new print waits
                # until the printer reports layer 3 or lower (leveling and the first status carry junk layers)
                self.state.update(file=s.get("file"), fails=0, flags=0, unsure_told=False, event=None, last=None, errors=0,
                                  blind=None, started=time.time() - self.boot < 90)
                next_check = 0
                try:
                    self.scene = self.scene_of(s.get("file")) if self.scene_of else None
                except Exception as e:
                    self.scene = None
                    self.log(f"watch scene for {s.get('file')} failed: {e}")
            layer = s.get("layer") or 0
            if 1 <= layer <= 3:
                self.state["started"] = True
            if not self.state["enabled"] or layer < 2 or not self.state.get("started") or time.time() < next_check:
                continue
            try:
                v = self.check_once(s)
            except Exception as e:
                v = {"verdict": "error", "reason": str(e)[:200], "time": time.strftime("%H:%M")}
            self.state["last"] = v
            try:
                delay = self.decide(s, v)
            except Exception as e:
                self.log(f"watch decide failed: {e}")
                delay = 60 * float(self.cfg.get("watch_every_min", 5))
            next_check = time.time() + delay
            self.log(f"watch {s.get('file')} layer {s.get('layer')}: {v}")
            try:
                self.record(s, v)
            except Exception as e:
                self.log(f"watch record failed: {e}")
            if v["verdict"] == "error":  # camera down, claude logged out, plan limit: say so instead of going quiet
                self.state["errors"] += 1
                if self.state["errors"] == 3:
                    self.state["blind"] = {"since": v.get("time"), "reason": v.get("reason")}
                    self.notify(f"Watcher can't check {s.get('file')} (3 tries since {v.get('time')}): {v.get('reason')}")
            else:
                self.state.update(errors=0, blind=None)
