# Security checks, one per fix from the 2026-11-07 audit: login brute force and lockout, bad Content-Length, slow
# and idle clients, the connection cap, Host / Origin / Content-Type rules, the one-time setup link, camera and AI
# fetches (redirects, internal replies, key forwarding), the G-code hard check, TOTP, the remote ports, step-up,
# sessions and revoke, safe remote start, zip bombs, page escaping and security headers.
# Run: python -I emulator/test_security.py [--port 8772] [--printer-ip 127.0.0.4]
# Starts its own sim hub (fresh, then set up through the API) and fake printer, plus small stand-in servers on this
# PC, and stops them all after. No printer, no internet, no AI. Takes about two minutes (the lockout waits).
import argparse, base64, http.client, io, json, os, re, shutil, socket, ssl, subprocess, sys, tempfile, threading, \
    time, urllib.parse, urllib.request, zipfile, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.dirname(HERE)
SIM = os.path.join(HERE, "sim-hub")
sys.path.insert(0, HERE)
sys.path.insert(1, HUB)
import mock_openai, security, slicer
from test_public import gcode

results = []
A = {}  # ports and names, set in __main__
REMOTE_HOST = "hub.example.test"


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({str(detail)[:300]})" if detail and not ok else ""), flush=True)


def req(port, method, path, body=None, headers=None, src=None, tls=False, host=None, timeout=20, ctype=None):
    """One request with http.client: (status, headers lower-case dict, body bytes, Set-Cookie list)."""
    h = dict(headers or {})
    if body is not None and not isinstance(body, bytes):
        body = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    if ctype:
        h["Content-Type"] = ctype
    if method == "POST" and body is None:
        body = b"{}"
        h.setdefault("Content-Type", "application/json")
    sa = (src, 0) if src else None
    if tls:
        c = http.client.HTTPSConnection("127.0.0.1", port, timeout=timeout, source_address=sa,
                                        context=ssl._create_unverified_context())
    else:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout, source_address=sa)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Host", host or f"127.0.0.1:{port}")
    for k, v in h.items():
        c.putheader(k, v)
    if body is not None:
        c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    r = c.getresponse()
    data = r.read()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, data, r.headers.get_all("Set-Cookie") or [])
    c.close()
    return out


def js(r):
    try:
        return json.loads(r[2])
    except ValueError:
        return {}


def lan(method, path, body=None, cookie=None, src=None, **k):
    h = dict(k.pop("headers", {}) or {})
    if cookie:
        h["Cookie"] = cookie
    if method == "POST":
        h.setdefault("Origin", f"http://127.0.0.1:{A['lan']}")
    return req(A["lan"], method, path, body, h, src=src, **k)


def rem(method, path, body=None, cookie=None, src=None, tls=False, **k):
    h = dict(k.pop("headers", {}) or {})
    if cookie:
        h["Cookie"] = cookie
    if method == "POST":
        h.setdefault("Origin", f"https://{REMOTE_HOST}")
    return req(A["https"] if tls else A["tunnel"], method, path, body, h, src=src, tls=tls, host=REMOTE_HOST, **k)


def cookie_of(setc, name):
    for c in setc:
        if c.startswith(name + "="):
            return c.split(";")[0]
    return None


def until(fn, timeout=30, every=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


# ---------- stand-in servers ----------

class Rec(BaseHTTPRequestHandler):
    """ntfy, camera and AI stand-ins. self.server.mode picks the behaviour; every hit is recorded."""
    def log_message(self, *a):
        pass

    def hit(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.server.hits.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        return body

    def reply(self, code, body=b"", ctype="text/plain", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.hit()
        m = self.server.mode
        if m == "redirect":
            return self.reply(302, extra={"Location": self.server.target})
        if m == "internal":
            return self.reply(200, b"junk\xff\xd8INTERNAL-SECRET-1\xff\xd9junk", "text/html")
        if m == "fakejpeg":
            return self.reply(200, b"\xff\xd8INTERNAL-SECRET-2\xff\xd9", "image/jpeg")
        return self.reply(200, b"ok")

    def do_POST(self):
        self.hit()
        m = self.server.mode
        if m == "redirect":
            return self.reply(307, extra={"Location": self.server.target})
        if m == "secret500":
            return self.reply(500, b'{"error": {"message": "INTERNAL-SECRET-3 at /var/lib/thing"}}', "application/json")
        return self.reply(200, b"{}", "application/json")


def stand_in(mode, host="127.0.0.9", target=None):
    srv = ThreadingHTTPServer((host, 0), Rec)
    srv.daemon_threads, srv.mode, srv.hits, srv.target = True, mode, [], target
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://{host}:{srv.server_address[1]}"


# ---------- unit checks: no hub needed ----------

def unit():
    key = b"12345678901234567890"
    vec = {59: "94287082", 1111111109: "07081804", 1111111111: "14050471", 1234567890: "89005924",
           2000000000: "69279037", 20000000000: "65353130"}
    check("TOTP: RFC 6238 SHA-1 test vectors (8 digits)",
          all(security.totp(None, t=t, digits=8, key=key) == w for t, w in vec.items()))
    sec = base64.b32encode(key).decode()
    check("TOTP: 6-digit code at T=59 is 287082", security.totp(sec, t=59) == "287082")
    st = security.totp_check(sec, "287082", t=59)
    check("TOTP: code accepted, its step returned", st == 1, st)
    check("TOTP: the same code again is refused (replay)", security.totp_check(sec, "287082", last_step=st, t=59) is None)
    check("TOTP: one step of clock drift is fine, three is not",
          security.totp_check(sec, security.totp(sec, t=59 + 30), t=59) == 2
          and security.totp_check(sec, security.totp(sec, t=59 + 90), t=59) is None)
    check("TOTP: wrong or junk code refused", security.totp_check(sec, "000000", t=59) is None
          and security.totp_check(sec, "abc", t=59) is None and security.totp_check("", "287082", t=59) is None)
    check("otpauth URI for the authenticator app", security.otpauth_uri(sec).startswith("otpauth://totp/")
          and f"secret={sec}" in security.otpauth_uri(sec))
    h = security.hash_secret("correct horse battery")
    check("scrypt hash: right secret yes, wrong no, empty hash no", security.check_secret("correct horse battery", h)
          and not security.check_secret("wrong", h) and not security.check_secret("x", "") and "correct" not in h)

    L, locks = security.Limiter(lambda k, n: locks.append((k, n))), []
    for _ in range(5):
        assert L.wait("lan", "1.1.1.1") == 0
        L.fail("lan", "1.1.1.1")
    check("limiter: 5 free tries per address, then a wait", L.wait("lan", "1.1.1.1") > 0 and L.wait("lan", "2.2.2.2") == 0)
    for i in range(20):
        L.fail("remote", f"10.0.0.{i}")
    check("limiter: 20 remote misses in an hour lock remote login, with an alert",
          L.wait("remote", "9.9.9.9") == float("inf") and locks == [("remote", 20)], locks)
    L.success("lan", "192.168.1.2")
    check("limiter: a home-network login opens remote login again", L.wait("remote", "9.9.9.9") < float("inf"))

    def hard(*lines):
        return slicer.hard_check("\n".join(["M104 S210", "G1 X1 Y1 E1", *lines]))
    check("hard check: a normal file passes", hard("M140 S60", "M109 S215", "G92 E0", "SET_VELOCITY_LIMIT ACCEL=5000",
                                                   "EXCLUDE_OBJECT_START NAME=a", "T1", "M220 S100") == [])
    for name, line in (("lowercase", "m104 s350"), ("leading space", "    M104 S350"), ("R parameter", "M109 R320"),
                       ("no space", "M104S305"), ("line number + checksum", "N12 M104 S330*45"),
                       ("Klipper heater command", "SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=330"),
                       ("Klipper lowercase", "set_heater_temperature heater=extruder target=320"),
                       ("macro parameter", "PRINT_START EXTRUDER_TEMP=320 BED_TEMP=60"),
                       ("RepRapFirmware G10", "G10 P0 S320 R200"), ("tool parameter", "M104 T2 S310")):
        r = hard(line)
        check(f"hard check: nozzle over 300 caught ({name}: {line.strip()})", any("nozzle" in x for x in r), r)
    check("hard check: bed over 110 caught (M190, Klipper heater_bed)", any("bed" in x for x in hard("M190 S120"))
          and any("bed" in x for x in hard("SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=130")))
    check("hard check: chamber over 65 caught (M141, macro)", any("chamber" in x for x in hard("M141 S80"))
          and any("chamber" in x for x in hard("START CHAMBER_TEMP=90")))
    for line in ("SAVE_CONFIG", "save_config", "  Save_Config ; tuned", "FIRMWARE_RESTART", "M500", "m500", "M 500",
                 "M0500", "N3 M502*7", "M997", "M302 P1", "M211 S0", "SET_KINEMATIC_POSITION Z=10", "FORCE_MOVE STEPPER=x",
                 "RUN_SHELL_COMMAND CMD=x", "SET_GCODE_OFFSET Z=-3", "G92 Z5", "M92 E400", "M851 Z-2"):
        check(f"hard check: '{line}' is never sent", hard(line) != [], hard(line))
    check("hard check: a comment that names a command is fine", hard("; SAVE_CONFIG", "M117 hi ; M500") == [])
    check("hard check: config hard_caps can only lower the caps", slicer.hard_caps({"nozzle": 400, "bed": 90}) ==
          {"nozzle": 300, "bed": 90, "chamber": 65})

    try:
        security.pinned("http://169.254.169.254/latest/", True)
        meta = False
    except ValueError:
        meta = True
    bad = []
    for u, lo in (("http://8.8.8.8:8080/", False), ("http://127.0.0.1:8080/", False), ("ftp://192.168.1.5/", False),
                  ("file:///C:/Windows/win.ini", True)):
        try:
            security.pinned(u, lo)
            bad.append(u)
        except ValueError:
            pass
    check("camera address: cloud metadata (169.254) refused", meta)
    check("camera address: internet, this PC (unless allowed), ftp and file refused", not bad, bad)
    check("camera address: home network resolved and pinned",
          security.pinned("http://192.168.1.60:8080/video", False) == "http://192.168.1.60:8080/video")
    check("home port clients: private and Tailscale (when on) yes, internet no",
          security.home_client("192.168.1.4") and security.home_client("::ffff:10.1.2.3")
          and not security.home_client("8.8.8.8") and not security.home_client("100.101.1.2")
          and security.home_client("100.101.1.2", tailscale=True))
    check("Host allowlist: addresses and own names yes, other names no",
          security.host_ok("192.168.1.20:8765", set()) and security.host_ok("localhost:1", {"localhost"})
          and not security.host_ok("evil.example:8765", {"localhost"}))
    H = lambda **h: h
    check("Origin: same host yes, other site no, null no, missing + cross-site no",
          security.origin_ok(H(Origin="http://10.0.0.5:8765"), "10.0.0.5:8765")
          and not security.origin_ok(H(Origin="http://evil.example"), "10.0.0.5:8765")
          and not security.origin_ok(H(Origin="null"), "10.0.0.5:8765")
          and not security.origin_ok({"Sec-Fetch-Site": "cross-site"}, "10.0.0.5:8765")
          and security.origin_ok({}, "10.0.0.5:8765")
          and security.origin_ok(H(Origin="https://hub.example.test"), "localhost:8773", {"hub.example.test"}))


# ---------- live checks against a sim hub ----------

def raw(port, data, src=None, wait=3.0):
    """Send raw bytes; return what came back before the server closed or `wait` passed."""
    s = socket.create_connection(("127.0.0.1", port), timeout=wait, source_address=(src, 0) if src else None)
    s.sendall(data)
    out, t0 = b"", time.time()
    try:
        while time.time() - t0 < wait:
            b = s.recv(4096)
            if not b:
                break
            out += b
    except (socket.timeout, OSError):
        pass
    s.close()
    return out, time.time() - t0


def up(port, host=None):
    try:
        return req(port, "GET", "/manifest.json", host=host, timeout=2)[0] == 200
    except OSError:
        return False


def live(ntfy):
    pushes = lambda: [h["body"].decode(errors="ignore") for h in ntfy.hits]

    # --- before setup: the one-time link, CSRF and DNS rebinding ---
    link = until(lambda: os.path.exists(os.path.join(SIM, "setup-url.txt"))
                 and open(os.path.join(SIM, "setup-url.txt")).read().strip())
    tok = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)["token"][0]
    r = lan("POST", "/api/setup/pin", {"pin": "1357"}, headers={"Origin": "http://evil.example"})
    check("pre-setup: a cross-site page cannot choose the PIN (no setup cookie)", r[0] in (401, 403), r[:3])
    r = req(A["lan"], "POST", "/api/setup/pin", b'{"pin": "1357"}', {"Origin": "http://evil.example"}, ctype="text/plain")
    check("pre-setup: a text/plain 'simple' POST is refused", r[0] == 415, r[:3])
    r = req(A["lan"], "GET", "/setup?token=" + tok, host="evil.example:%d" % A["lan"])
    check("pre-setup: DNS rebinding (foreign Host) refused", r[0] == 421, r[:3])
    r = lan("GET", "/setup?token=" + tok)
    sc = cookie_of(r[3], "hubsetup")
    check("pre-setup: the one-time link gives a strict HttpOnly cookie", r[0] == 303 and sc
          and "HttpOnly" in r[3][0] and "SameSite=Strict" in r[3][0], r[3])
    r = lan("POST", "/api/setup/pin", {"pin": "1357"}, cookie=sc, headers={"Origin": "http://evil.example"})
    check("pre-setup: with the cookie but from another site: refused (Origin)", r[0] == 403, r[:3])
    r = lan("POST", "/api/setup/pin", {"pin": "1357"}, cookie=sc, headers={"X-Forwarded-For": "1.2.3.4"})
    check("pre-setup: through a proxy on this PC: refused (proxy headers on the home port)", r[0] == 403, r[:3])
    r = lan("GET", "/api/setup/camera.jpg?url=" + urllib.parse.quote("http://169.254.169.254/latest/meta-data/"), cookie=sc)
    check("SSRF: camera preview refuses cloud metadata", r[0] == 400, r[:3])
    r = lan("POST", "/api/setup/printer", {"ip": A["printer_ip"], "serial": "SIMULATOR0001", "check_code": "simcheck"}, cookie=sc)
    check("setup: printer", r[0] == 200, r[:3])
    r = lan("POST", "/api/setup/pin", {"pin": "1357"}, cookie=sc)
    lan_a = cookie_of(r[3], "hub")
    check("setup: PIN saved, per-device session cookie (HttpOnly, Strict, no Secure on plain http)", r[0] == 200 and lan_a
          and "HttpOnly" in r[3][0] and "SameSite=Strict" in r[3][0] and "Secure" not in r[3][0], r[3])
    cfg = json.load(open(os.path.join(SIM, "config.json")))
    check("PIN stored hashed only (scrypt), no plain PIN in config.json", cfg.get("pin_hash", "").startswith("scrypt$")
          and "1357" not in json.dumps(cfg) and "password" not in cfg)
    if os.name == "nt":
        check("check code saved encrypted for this Windows account (DPAPI), never in plain text",
              cfg.get("check_code", "").startswith("dpapi:") and "simcheck" not in json.dumps(cfg)
              and security.cfg_from_disk(dict(cfg))["check_code"] == "simcheck", cfg.get("check_code", "")[:12])
        bad = security.cfg_from_disk({"openai_api_key": "dpapi:" + base64.b64encode(b"x" * 40).decode(),
                                      "ntfy_token": "typed-by-hand"}, log=lambda m: None)
        check("a key saved on another PC is cleared (asked again), a hand-typed plain key is kept",
              bad["openai_api_key"] == "" and bad["ntfy_token"] == "typed-by-hand", bad)
        check("plain key typed into config.json gets encrypted on save",
              security.cfg_for_disk({"ntfy_token": "typed-by-hand"})["ntfy_token"].startswith("dpapi:"))
    r = lan("POST", "/api/setup/done", {}, cookie=sc)
    check("setup: done", r[0] == 200, r[:3])
    check("setup link removed, its cookie dead", not os.path.exists(os.path.join(SIM, "setup-url.txt"))
          and lan("GET", "/api/setup", cookie=sc)[0] == 401)
    check("remote port starts once setup is done", until(lambda: up(A["tunnel"], REMOTE_HOST), 10))

    # --- headers and error text ---
    r = lan("GET", "/", cookie=lan_a)
    hd = r[1]
    check("security headers: CSP with frame-ancestors none, X-Frame-Options, nosniff, Referrer-Policy",
          "frame-ancestors 'none'" in hd.get("content-security-policy", "") and hd.get("x-frame-options") == "DENY"
          and hd.get("x-content-type-options") == "nosniff" and hd.get("referrer-policy") == "no-referrer", hd)
    check("server banner has no Python version", "python" not in hd.get("server", "").lower(), hd.get("server"))
    r = lan("GET", "/api/jobs/20260101-000000-abcd", cookie=lan_a)
    check("missing job: plain 'not found', no file paths", r[0] == 404 and b"\\" not in r[2] and b"Users" not in r[2], r[:3])
    r = lan("GET", "/api/raw", cookie=lan_a)
    check("/api/raw leaves out the cloud registration codes and MAC address", r[0] == 200
          and b"RegisterCode" not in r[2] and b"macAddr" not in r[2], r[2][:200])
    r = lan("GET", "/api/status", headers={"X-Hub-Key": security.cfg_from_disk(dict(cfg))["api_token"], "X-Forwarded-For": "8.8.8.8"})
    check("API token through a proxy: refused", r[0] == 403, r[:3])
    r = lan("GET", "/api/status", headers={"X-Hub-Key": security.cfg_from_disk(dict(cfg))["api_token"]})
    check("API token (hubctl) from this PC works", r[0] == 200, r[:3])
    r = lan("GET", "/api/status", headers={"X-Hub-Key": "1357"})
    check("the PIN is not an API key", r[0] == 401, r[:3])

    # --- request hardening ---
    for label, head, want in (("negative", b"Content-Length: -1", b" 400 "), ("not a number", b"Content-Length: abc", b" 400 "),
                              ("huge", b"Content-Length: 999999999999", b" 413 "),
                              ("chunked", b"Transfer-Encoding: chunked", b" 411 ")):
        out, took = raw(A["lan"], b"POST /login HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n" + head +
                        b"\r\n\r\nAAAA", wait=4)
        check(f"bad Content-Length ({label}) answered at once, nothing read", want in out[:20] and took < 3.5, (out[:60], took))
    out, took = raw(A["lan"], ("POST /api/upload?name=x.stl HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/octet-stream"
                               f"\r\nCookie: {lan_a}\r\nContent-Length: 400000000\r\n\r\n").encode(), wait=4)
    check("upload over the 300 MB cap refused before any of it is read", b" 413 " in out[:20], out[:60])
    s = socket.create_connection(("127.0.0.1", A["lan"]))
    s.sendall(b"GET / HTTP/1.1\r\n")
    t0, closed = time.time(), False
    try:
        while time.time() - t0 < 12:
            s.sendall(b"X")  # one header byte a second, never finishing
            s.settimeout(1)
            try:
                if s.recv(100) == b"":
                    closed = True
                    break
            except socket.timeout:
                pass
    except OSError:
        closed = True
    s.close()
    check("slow-loris client (headers one byte a second) cut off at the header deadline", closed and time.time() - t0 < 9,
          time.time() - t0)
    out, took = raw(A["lan"], b"", wait=10)
    check("idle connection closed by the socket timeout", took < 8, took)
    held = []
    for _ in range(9):
        held.append(socket.create_connection(("127.0.0.1", A["lan"]), source_address=("127.0.0.8", 0)))
    time.sleep(0.5)
    held[-1].settimeout(3)
    try:
        busy = held[-1].recv(100)
    except socket.timeout:
        busy = b""
    check("connection cap per address: the extra connection gets 503 at once", b" 503 " in busy[:20], busy[:40])
    r = lan("GET", "/api/status", cookie=lan_a)
    check("the owner's phone still gets through while one address holds its whole share", r[0] == 200, r[:2])
    for x in held:
        x.close()

    # --- Host, Origin, Content-Type on every POST ---
    r = req(A["lan"], "GET", "/api/status", headers={"Cookie": lan_a}, host="rebind.evil.example")
    check("Host: a foreign name is refused (DNS rebinding)", r[0] == 421, r[:2])
    r = lan("POST", "/api/control", {"action": "light_on"}, cookie=lan_a, headers={"Origin": "http://evil.example"})
    check("CSRF: a POST from another site is refused even with the cookie", r[0] == 403, r[:3])
    r = req(A["lan"], "POST", "/api/control", b'{"action":"light_on"}', {"Cookie": lan_a, "Sec-Fetch-Site": "cross-site"},
            ctype="application/json")
    check("CSRF: cross-site with no Origin header refused", r[0] == 403, r[:3])
    r = req(A["lan"], "POST", "/api/control", b'{"action":"light_on"}', {"Cookie": lan_a}, ctype="text/plain")
    check("CSRF: text/plain body refused (JSON only)", r[0] == 415, r[:3])
    r = req(A["lan"], "POST", "/api/upload?name=a.stl", b"solid x\nendsolid x\n", {"Cookie": lan_a}, ctype="application/json")
    check("uploads must be application/octet-stream", r[0] == 415, r[:3])

    # --- login brute force on the home port ---
    for i in range(5):
        r = lan("POST", "/login", {"password": f"99{i:02d}"}, src="127.0.0.6")
    t0 = time.time()
    r = lan("POST", "/login", {"password": "9999"}, src="127.0.0.6")
    check("PIN brute force: after 5 misses the address must wait (429 + Retry-After, answered at once)",
          r[0] == 429 and "retry-after" in r[1] and time.time() - t0 < 0.4, r[:3])
    r = lan("POST", "/login", {"password": "1357"}, src="127.0.0.6")
    check("PIN brute force: even the right PIN waits during the backoff", r[0] == 429, r[:3])
    r = lan("POST", "/login", {"password": "1357"}, src="127.0.0.7")
    lan_b = cookie_of(r[3], "hub")
    check("another device logs in normally", r[0] == 200 and lan_b, r[:3])
    t0 = time.time()
    lan("POST", "/login", {"password": "0000"}, src="127.0.0.11")
    t1 = time.time()
    lan("POST", "/login", {"password": "1357"}, src="127.0.0.12")
    t2 = time.time()
    check("wrong and right PIN take the same time (about 0.5 s)", abs((t1 - t0) - (t2 - t1)) < 0.25, (t1 - t0, t2 - t1))
    log = open(os.path.join(SIM, "hub.log"), encoding="utf-8").read()
    check("wrong PINs are logged", "SECURITY wrong secret from 127.0.0.6" in log)
    check("new sign-in pushed to the phone", until(lambda: any("New sign-in" in p for p in pushes()), 10), pushes())

    # --- sessions: list, revoke, logout ---
    r = lan("GET", "/api/sessions", cookie=lan_a)
    ss = js(r).get("sessions", [])
    other = [x for x in ss if not x["current"]]
    check("signed-in devices listed (device, kind, last seen, this one marked)", r[0] == 200 and len(ss) >= 2
          and any(x["current"] for x in ss) and all("seen" in x and "kind" in x for x in ss), ss)
    sess_file = open(os.path.join(SIM, "sessions.json"), encoding="utf-8").read()
    check("session tokens stored hashed only", lan_a.split("=", 1)[1] not in sess_file)
    b_id = [x["id"] for x in other]
    for sid in b_id:
        lan("POST", "/api/sessions/revoke", {"id": sid}, cookie=lan_a)
    check("revoked device is signed out at once", lan("GET", "/api/status", cookie=lan_b)[0] == 401)
    r = lan("POST", "/login", {"password": "1357"}, src="127.0.0.13")
    lan_c = cookie_of(r[3], "hub")
    r = lan("POST", "/logout", {}, cookie=lan_c)
    check("logout ends that session", r[0] == 200 and lan("GET", "/api/status", cookie=lan_c)[0] == 401)

    # --- remote port: passphrase + TOTP, step-up ---
    r = rem("GET", "/")
    check("remote port shows the passphrase + code login", r[0] == 200 and b'data-mode="remote"' in r[2], r[:2])
    r = req(A["tunnel"], "GET", "/", host="evil.example")
    check("remote port: unknown Host refused", r[0] == 421, r[:2])
    r = rem("POST", "/login", {"password": "1357"})
    check("remote port: the PIN alone never logs in", r[0] in (403, 409), r[:3])
    r = rem("GET", "/api/status", headers={"X-Hub-Key": security.cfg_from_disk(dict(cfg))["api_token"]})
    check("remote port: the API token is not accepted", r[0] == 401, r[:3])
    r = rem("GET", "/api/status", cookie="__Host-hub=" + lan_a.split("=", 1)[1])
    check("remote port: a home-network session is not accepted", r[0] == 401, r[:3])
    r = lan("POST", "/api/setup/remote", {"passphrase": "short", "passphrase2": "short"}, cookie=lan_a)
    check("step-up: a secret-moving change asks for the PIN again first", r[0] == 403 and js(r).get("step_up"), r[:3])
    r = lan("POST", "/api/setup/remote", {"passphrase": "short", "passphrase2": "short", "confirm_secret": "0000"}, cookie=lan_a)
    check("step-up: the wrong PIN is refused", r[0] == 403 and js(r).get("step_up"), r[:3])
    r = lan("POST", "/api/setup/remote", {"passphrase": "short", "passphrase2": "short", "confirm_secret": "1357"}, cookie=lan_a)
    check("remote passphrase under 12 characters refused", r[0] == 400 and "12" in js(r).get("error", ""), r[:3])
    PASS = "tower crane hums at dawn"
    r = lan("POST", "/api/setup/remote", {"passphrase": PASS, "passphrase2": PASS}, cookie=lan_a)
    check("remote passphrase saved (step-up good for 10 min)", r[0] == 200 and js(r).get("pass_set"), r[:3])
    r = rem("POST", "/login", {"passphrase": PASS, "code": "123456"})
    check("remote login refused until the authenticator is set", r[0] == 409, r[:3])
    r = lan("POST", "/api/setup/remote/totp", {}, cookie=lan_a)
    secret = js(r).get("secret")
    check("authenticator: secret and otpauth URI shown", r[0] == 200 and secret and js(r).get("uri", "").startswith("otpauth://"), r[:3])
    r = lan("POST", "/api/setup/remote", {"code": "000000"}, cookie=lan_a)
    check("authenticator: a wrong first code is refused", r[0] == 400, r[:3])
    r = lan("POST", "/api/setup/remote", {"code": security.totp(secret)}, cookie=lan_a)
    check("authenticator saved after a right code", r[0] == 200 and js(r).get("totp_set"), r[:3])
    r = rem("POST", "/login", {"passphrase": PASS, "code": "000000"}, src="127.0.0.21")
    check("remote login: right passphrase, wrong code refused", r[0] == 403, r[:3])
    r = rem("POST", "/login", {"passphrase": "1357", "code": security.totp(secret, t=time.time() + 30)}, src="127.0.0.22")
    check("remote login: right code, wrong passphrase refused", r[0] == 403, r[:3])
    code = security.totp(secret, t=time.time() + 30)
    r = rem("POST", "/login", {"passphrase": PASS, "code": code}, src="127.0.0.23")
    rc = cookie_of(r[3], "__Host-hub")
    check("remote login: passphrase + code works, __Host- cookie with Secure, HttpOnly, SameSite=Lax",
          r[0] == 200 and rc and all(x in r[3][0] for x in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/")), r[3])
    r = rem("POST", "/login", {"passphrase": PASS, "code": code}, src="127.0.0.24")
    check("remote login: the same code cannot be used twice", r[0] == 403, r[:3])
    check("remote sign-in pushed to the phone", until(lambda: any("New sign-in" in p and "remote" in p for p in pushes()), 10))
    r = rem("GET", "/api/status", cookie=rc)
    check("remote session works, page told it is remote", r[0] == 200 and js(r).get("remote") is True, r[:2])
    r = lan("GET", "/api/status", cookie="hub=" + rc.split("=", 1)[1])
    check("a remote session does not work on the home port", r[0] == 401, r[:2])
    r = rem("POST", "/api/setup/camera", {"url": "http://127.0.0.1:9"}, cookie=rc)
    check("remote step-up: a new camera address asks for the passphrase", r[0] == 403 and js(r).get("what") == "passphrase", r[:3])
    r = rem("POST", "/api/setup/camera", {"url": "http://127.0.0.1:9", "confirm_secret": "1357"}, cookie=rc)
    check("remote step-up: the PIN is not enough", r[0] == 403, r[:3])
    r = rem("POST", "/api/setup/camera", {"url": "http://127.0.0.1:9", "confirm_secret": PASS}, cookie=rc)
    check("remote step-up: with the passphrase the change goes through (then the dead camera is reported)",
          r[0] == 400 and "no picture" in js(r).get("error", ""), r[:3])
    r = rem("GET", "/api/snapshot", cookie=rc)
    check("send sheet camera frame: fresh JPEG with its time", r[0] == 200 and r[2][:2] == b"\xff\xd8"
          and abs(float(r[1].get("x-frame-time", 0)) - time.time()) < 30, (r[0], r[1].get("x-frame-time")))

    # --- G-code: the hard check cannot be overridden ---
    for label, extra in (("SAVE_CONFIG", ["  save_config"]), ("lowercase nozzle 350", ["m104 s350"]),
                         ("macro EXTRUDER_TEMP=330", ["PRINT_START EXTRUDER_TEMP=330"]), ("M109 R320", ["M109 R320"])):
        r = lan("POST", "/api/upload?name=bad.gcode", gcode(extra=extra), cookie=lan_a, ctype="application/octet-stream")
        job = js(r)
        r2 = lan("POST", f"/api/jobs/{job.get('id')}/print", {"plate_clear": True, "allow_unchecked": True}, cookie=lan_a)
        check(f"G-code {label}: blocked, and Send anyway still refuses", job.get("state") == "blocked" and job.get("hard")
              and r2[0] == 400 and "never sent" in js(r2).get("error", ""), (job.get("hard"), r2[:3]))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        with z.open("Metadata/plate_1.gcode", "w", force_zip64=True) as f:
            chunk = b" " * (1 << 20)
            for _ in range(520):
                f.write(chunk)
    r = lan("POST", "/api/upload?name=bomb.3mf", buf.getvalue(), cookie=lan_a, ctype="application/octet-stream", timeout=60)
    check(f"zip bomb ({len(buf.getvalue()) // 1024} KB unpacking to 520 MB) refused before it is unpacked",
          r[0] == 400 and "unpacks" in js(r).get("error", ""), r[:3])

    # --- camera fetch: no redirects, no internal replies ---
    inner, iurl = stand_in("internal")
    redir, rurl = stand_in("redirect", target=iurl + "/admin?reboot=1")
    fake, furl = stand_in("fakejpeg")
    r = lan("GET", "/api/setup/camera.jpg?url=" + urllib.parse.quote(rurl + "/"), cookie=lan_a, timeout=30)
    check("SSRF: a camera redirect is not followed", r[0] == 503 and not inner.hits and redir.hits, (r[:3], inner.hits))
    r = lan("GET", "/api/setup/camera.jpg?url=" + urllib.parse.quote(iurl + "/"), cookie=lan_a, timeout=30)
    check("SSRF: a non-camera reply is never passed back", r[0] == 503 and b"INTERNAL-SECRET" not in r[2], r[:3])
    r = lan("GET", "/api/setup/camera.jpg?url=" + urllib.parse.quote(furl + "/"), cookie=lan_a, timeout=30)
    check("SSRF: bytes that only look like a JPEG are not passed back", r[0] == 503 and b"INTERNAL-SECRET" not in r[2], r[:3])

    # --- AI address: key never follows a new address, no internal replies, forget works ---
    srv1, u1 = mock_openai.start()
    srv2, u2 = mock_openai.start()
    body = {"backend": "openai", "preset": "custom", "model": "good", "local_server": True}
    r = lan("POST", "/api/setup/watcher", dict(body, base_url=u1, openai_key="sk-owner-SECRET-1"), cookie=lan_a)
    check("AI: key saved for the first server", r[0] == 200 and js(r)["openai"]["key_end"] == "ET-1", r[:3])
    r = lan("POST", "/api/setup/watcher", dict(body, base_url=u2), cookie=lan_a)
    check("AI: changing only the address clears the saved key, and says so", r[0] == 200
          and js(r)["openai"]["key_end"] == "" and "cleared" in (js(r).get("note") or ""), r[:3])
    lan("POST", "/api/setup/watcher/test", {}, cookie=lan_a)
    check("AI: the new server never sees the old key", "Authorization" not in mock_openai.LAST.get("headers", {}),
          mock_openai.LAST.get("headers"))
    r = lan("POST", "/api/setup/watcher", dict(body, base_url="http://8.8.8.8/v1", openai_key="k"), cookie=lan_a)
    check("AI: plain http to the internet refused (the key would go in clear text)", r[0] == 400 and "https" in js(r).get("error", ""), r[:3])
    sec5, s5 = stand_in("secret500")
    lan("POST", "/api/setup/watcher", dict(body, base_url=s5 + "/v1", openai_key="sk-two"), cookie=lan_a)
    r = lan("POST", "/api/setup/watcher/test", {}, cookie=lan_a)
    check("AI: an internal server's reply text is never echoed", r[0] == 200 and b"INTERNAL-SECRET" not in r[2]
          and b"/var/lib" not in r[2], r[2][:200])
    tgt, turl = stand_in("ok")
    rd, rdu = stand_in("redirect", target=turl + "/v1/chat/completions")
    lan("POST", "/api/setup/watcher", dict(body, base_url=rdu + "/v1", openai_key="sk-three"), cookie=lan_a)
    r = lan("POST", "/api/setup/watcher/test", {}, cookie=lan_a)
    check("AI: a redirect is not followed, so the key does not travel on", not tgt.hits and b"redirect" in r[2], (tgt.hits, r[2][:200]))
    r = lan("POST", "/api/setup/watcher", {"backend": "", "forget_openai_key": True}, cookie=lan_a)
    check("AI: 'forget key' works with any backend chosen", r[0] == 200
          and json.load(open(os.path.join(SIM, "config.json"))).get("openai_api_key") == "", r[:3])
    srv1.shutdown(), srv2.shutdown()

    # --- printer text cannot reach the page as HTML ---
    urllib.request.urlopen(f"http://{A['printer_ip']}:8898/sim/load?slot=2&material=PLA&colour="
                           + urllib.parse.quote("red;background:url(//x)\"><img src=x onerror=alert(1)>"), timeout=5).read()
    st = until(lambda: next((x for x in js(lan("GET", "/api/status", cookie=lan_a))["printer"].get("slots", []) if x["slot"] == 2
                             and x["colour"] != "#000000"), None), 15)
    check("printer colour that is not #RRGGBB never reaches the page", st and st["colour"] is None, st)
    urllib.request.urlopen(f"http://{A['printer_ip']}:8898/sim/load?slot=2&material=PLA&colour=%23FFFFFF", timeout=5).read()
    page = lan("GET", "/", cookie=lan_a)[2].decode()
    check("page escaping covers ' and colours are checked before they go in a style",
          "'\"':\"&quot;\",\"'\":\"&#39;\"" in page and "const col = c =>" in page)

    # --- safe remote start: frame, optional bed mark, one extra confirm, pushes ---
    urllib.request.urlopen(f"http://{A['printer_ip']}:8898/sim/load?slot=1&colour=%23FFFFFF&material=PLA", timeout=5).read()
    rh = {"Cookie": rc, "Origin": f"https://{REMOTE_HOST}"}
    r = req(A["tunnel"], "POST", "/api/upload?name=remote-square.gcode", gcode(), rh, host=REMOTE_HOST,
            ctype="application/octet-stream")
    jid = js(r).get("id")
    check("remote upload works", r[0] == 200 and jid, r[:3])
    r = rem("POST", f"/api/jobs/{jid}/print", {"plate_clear": True, "allow_unchecked": True}, cookie=rc)
    check("remote start works (no print ended yet, so no extra question)", r[0] == 200 and js(r).get("state") == "sent", r[:3])
    check("remote start pushed to the phone", until(lambda: any("started remotely" in p for p in pushes()), 10), pushes())
    until(lambda: js(rem("GET", "/api/status", cookie=rc))["printer"].get("status") == "printing", 30)
    r = rem("POST", "/api/control", {"action": "cancel"}, cookie=rc)
    check("remote cancel works", r[0] == 200, r[:3])
    check("remote cancel pushed to the phone", until(lambda: any("cancelled remotely" in p for p in pushes()), 10), pushes())
    hub_cancel_at = time.time()
    time.sleep(6)
    check("a cancel from the hub is not also pushed as 'Cancelled on the printer'",
          not any("Cancelled on the printer" in p for p in pushes()), pushes()[-3:])
    bed = until(lambda: js(rem("GET", "/api/status", cookie=rc)).get("bed", {}).get("needs_mark") and True, 20)
    check("after a print ends the bed counts as not marked cleared", bed)
    r = rem("POST", f"/api/jobs/{jid}/print", {"plate_clear": True, "allow_unchecked": True}, cookie=rc)
    check("remote start with no bed mark since the last print: one extra confirm, not a refusal",
          r[0] == 409 and js(r).get("confirm") == "bed", r[:3])
    r = rem("POST", "/api/bed_cleared", {}, cookie=rc)
    check("bed marked cleared from away, with time and device", r[0] == 200 and js(r).get("cleared_at")
          and not js(r).get("needs_mark") and js(r).get("cleared_by"), r[:3])
    r = rem("POST", f"/api/jobs/{jid}/print", {"plate_clear": True, "allow_unchecked": True}, cookie=rc)
    check("after the mark the remote start goes straight through", r[0] == 200, r[:3])
    until(lambda: js(rem("GET", "/api/status", cookie=rc))["printer"].get("status") == "printing", 30)
    rem("POST", "/api/control", {"action": "cancel"}, cookie=rc)
    until(lambda: js(rem("GET", "/api/status", cookie=rc)).get("bed", {}).get("needs_mark"), 20)
    r = rem("POST", f"/api/jobs/{jid}/print", {"plate_clear": True, "allow_unchecked": True, "no_mark_ok": True}, cookie=rc)
    check("the extra confirm lets the start through", r[0] == 200, r[:3])
    until(lambda: js(rem("GET", "/api/status", cookie=rc))["printer"].get("status") == "printing", 30)
    rem("POST", "/api/control", {"action": "cancel"}, cookie=rc)
    log = open(os.path.join(SIM, "hub.log"), encoding="utf-8").read()
    check("audit trail: remote actions logged with the device", re.search(r"remote .+ \(127\.0\.0\.1\): POST /api/jobs/.+/print", log))

    # --- remote lockout: 20 misses an hour, an alert, opened by a home-network login ---
    n, code, t0 = 0, 0, time.time()
    while code != 423 and time.time() - t0 < 90:
        r = rem("POST", "/login", {"passphrase": "guess number %d" % n, "code": "123456"}, src=f"127.0.1.{1 + n % 200}")
        code = r[0]
        if code == 429:
            time.sleep(float(r[1].get("retry-after", 1)))
        n += 1
    check("remote brute force: login locks after the misses (423)", code == 423, (code, n))
    check("lockout pushed to the phone and logged", until(lambda: any("LOCKED" in p for p in pushes()), 10)
          and "SECURITY Remote login LOCKED" in open(os.path.join(SIM, "hub.log"), encoding="utf-8").read(), pushes()[-3:])
    r = rem("POST", "/login", {"passphrase": PASS, "code": security.totp(secret, t=time.time() + 30)}, src="127.0.2.1")
    check("while locked, even the right passphrase and code are refused", r[0] == 423, r[:3])
    check("while locked, a device already signed in remotely keeps working", rem("GET", "/api/status", cookie=rc)[0] == 200)
    r = lan("POST", "/login", {"password": "1357"}, src="127.0.0.31")
    check("a home-network login opens remote login again", r[0] == 200
          and not json.load(open(os.path.join(SIM, "config.json"))).get("remote_locked"), r[:3])
    last = int(json.load(open(os.path.join(SIM, "config.json"))).get("totp_last_step") or 0)
    while int((time.time() + 30) // 30) <= last:  # each code works once: wait for a newer one
        time.sleep(1)
    time.sleep(2.1)
    r = rem("POST", "/login", {"passphrase": PASS, "code": security.totp(secret, t=time.time() + 30)}, src="127.0.2.2")
    check("then remote login works again", r[0] == 200, r[:3])

    # --- a cancel on the printer screen (past the 90 s in which a cancel counts as the hub's own) ---
    cancels = re.findall(r"^(\S+ \S+) control cancel", open(os.path.join(SIM, "hub.log"), encoding="utf-8").read(), re.M)
    last = time.mktime(time.strptime(cancels[-1], "%Y-%m-%d %H:%M:%S")) if cancels else hub_cancel_at
    time.sleep(max(0, 95 - (time.time() - last)))
    sim = lambda q: urllib.request.urlopen(f"http://{A['printer_ip']}:8898/sim/status?s={q}", timeout=5).read()
    sim("printing")
    until(lambda: js(rem("GET", "/api/status", cookie=rc))["printer"].get("status") == "printing", 20)
    time.sleep(3)
    sim("cancel")
    check("a cancel on the printer screen is pushed to the phone",
          until(lambda: any("Cancelled on the printer" in p for p in pushes()), 15), pushes()[-3:])

    # --- HTTPS port with your own certificate ---
    if A.get("https"):
        r = rem("GET", "/", tls=True)
        check("HTTPS port: serves the remote login over TLS with HSTS", r[0] == 200 and b'data-mode="remote"' in r[2]
              and "max-age" in r[1].get("strict-transport-security", ""), r[:2])
        out, took = raw(A["https"], b"GET / HTTP/1.1\r\nHost: x\r\n\r\n", wait=3)
        check("HTTPS port: plain http on it gets nothing useful", b"data-mode" not in out, out[:80])
    else:
        print("SKIP HTTPS port checks (no openssl to make a test certificate)", flush=True)


def make_cert(d):
    exe = shutil.which("openssl") or next((p for p in (r"C:\Program Files\Git\usr\bin\openssl.exe",
                                                       r"C:\Program Files\Git\mingw64\bin\openssl.exe") if os.path.exists(p)), None)
    if not exe:
        return None
    c, k = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
    p = subprocess.run([exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", k, "-out", c, "-days", "2",
                        "-subj", f"/CN={REMOTE_HOST}"], capture_output=True)
    return (c, k) if p.returncode == 0 and os.path.exists(c) else None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8772)
    ap.add_argument("--printer-ip", default="127.0.0.4")
    a = ap.parse_args()
    A.update(lan=a.port, tunnel=a.port + 1, printer_ip=a.printer_ip)
    unit()
    for d in ("jobs", "template", "drying", "watch"):
        shutil.rmtree(os.path.join(SIM, d), ignore_errors=True)
    for f in ("config.json", "hub.log", "sessions.json", "setup-url.txt"):
        if os.path.exists(os.path.join(SIM, f)):
            os.remove(os.path.join(SIM, f))
    tmp = tempfile.mkdtemp()
    cert = make_cert(tmp)
    ntfy, nurl = stand_in("ok", "127.0.0.1")  # stand-in ntfy server: every push the hub sends lands here
    sets = {"remote_tunnel_port": a.port + 1, "remote_hosts": [REMOTE_HOST], "ntfy_server": nurl,
            "ntfy_topic": "test-topic-0123456789", "http_header_timeout_s": 3, "http_timeout_s": 4,
            "http_max_conns": 16, "http_max_conns_per_ip": 8}
    if cert:
        A["https"] = a.port + 2
        sets.update(remote_https_port=a.port + 2, remote_https_host="127.0.0.1", tls_cert=cert[0], tls_key=cert[1])
    hub = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "sim_hub.py"), "--port", str(a.port), "--fresh",
                            "--speed", "10", "--printer-ip", a.printer_ip]
                           + [x for k, v in sets.items() for x in ("--set", f"{k}={json.dumps(v)}")],
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    rc = 1
    try:
        until(lambda: up(a.port), 40)
        live(ntfy)
        rc = 0 if all(results) else 1
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(hub.pid)], capture_output=True)
    print(f"\n{sum(results)}/{len(results)} passed", flush=True)
    sys.exit(rc)
