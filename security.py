# Security helpers for C5 Print Hub, standard library only: hashed secrets, TOTP (RFC 6238), per-device sessions,
# login rate limits, address checks for what the hub fetches, and a urllib opener that never follows redirects.
import base64, hashlib, hmac, ipaddress, json, os, re, secrets, socket, struct, threading, time, urllib.parse, \
    urllib.request

# ---------- secrets at rest ----------
# scrypt, so a copied config.json does not hand over the PIN or passphrase. About 50 ms and 16 MB per check.
SCRYPT = (2 ** 14, 8, 1)


def hash_secret(text):
    n, r, p = SCRYPT
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(text.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt${n}${r}${p}${salt.hex()}${h.hex()}"


def check_secret(text, stored):
    """True when text matches a hash_secret() value. Always does the scrypt work, so a missing hash takes as long."""
    try:
        _, n, r, p, salt, want = (stored or "").split("$")
        n, r, p, salt, want = int(n), int(r), int(p), bytes.fromhex(salt), bytes.fromhex(want)
    except ValueError:
        n, r, p = SCRYPT
        salt, want = b"\0" * 16, b"\1" * 32
        stored = None
    got = hashlib.scrypt((text or "").encode(), salt=salt, n=n, r=r, p=p, dklen=len(want), maxmem=64 * 1024 * 1024)
    return bool(stored) and hmac.compare_digest(got, want)


# ---------- TOTP (RFC 6238, the 6-digit codes of any authenticator app) ----------

def totp_new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _b32(secret):
    s = re.sub(r"[\s-]", "", secret or "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(key, counter, digits=6, algo="sha1"):
    mac = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, algo)).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{code:0{digits}d}"


def totp(secret, t=None, step=30, digits=6, algo="sha1", key=None):
    return hotp(key if key is not None else _b32(secret), int((time.time() if t is None else t) // step), digits, algo)


def totp_check(secret, code, last_step=0, t=None, window=1, step=30):
    """The time step a code belongs to (this step, or one either side for a phone clock that is a little off), or
    None. A code at or before last_step was already used and is refused."""
    code = re.sub(r"\s", "", str(code or ""))
    if not re.fullmatch(r"\d{6}", code) or not secret:
        return None
    key, now = _b32(secret), int((time.time() if t is None else t) // step)
    for s in range(now - window, now + window + 1):
        if s > last_step and hmac.compare_digest(hotp(key, s), code):
            return s
    return None


def otpauth_uri(secret, account="owner", issuer="C5 Print Hub"):
    label = urllib.parse.quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={urllib.parse.quote(issuer)}&algorithm=SHA1&digits=6&period=30"


# ---------- per-device sessions ----------
# The browser holds a random token; the hub keeps only its SHA-256, with the device name, kind (lan / remote),
# last seen time and a sliding expiry. Stored in sessions.json next to config.json.

TTL = {"lan": 30 * 86400, "remote": 7 * 86400}


def _h(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


class Sessions:
    def __init__(self, path, log=print):
        self.path, self.log, self.lock = path, log, threading.Lock()
        self.s, self.saved = {}, 0
        try:
            self.s = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            self.s = {}

    def _save(self, force=True):
        if not force and time.time() - self.saved < 60:
            return
        self.saved = time.time()
        tmp = self.path + ".tmp"
        json.dump(self.s, open(tmp, "w", encoding="utf-8"), indent=1)
        os.replace(tmp, self.path)

    def create(self, kind, device, ip):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.s = {k: v for k, v in self.s.items() if v["expires"] > now}
            self.s[_h(token)] = {"kind": kind, "device": (device or "device")[:60], "ip": ip, "created": now,
                                 "seen": now, "expires": now + TTL[kind], "cookie_at": now, "elevated": 0}
            self._save()
        return token

    def get(self, token, kind):
        """The session for this token on a listener of this kind, its expiry slid forward; None if unknown, expired,
        revoked or of the other kind."""
        if not token:
            return None
        k = _h(token)
        now = time.time()
        with self.lock:
            rec = self.s.get(k)
            if not rec or rec["kind"] != kind:
                return None
            if rec["expires"] <= now:
                self.s.pop(k, None)
                self._save()
                return None
            rec["seen"], rec["expires"] = now, now + TTL[kind]
            self._save(force=False)
            return dict(rec, id=k[:12])

    def touch_cookie(self, token):
        """True once a day: time to send the cookie again so the browser's copy slides too."""
        with self.lock:
            rec = self.s.get(_h(token))
            if rec and time.time() - rec.get("cookie_at", 0) > 86400:
                rec["cookie_at"] = time.time()
                return True
        return False

    def elevate(self, token, seconds):
        with self.lock:
            rec = self.s.get(_h(token))
            if rec:
                rec["elevated"] = time.time() + seconds
                self._save()

    def elevated(self, token):
        rec = self.s.get(_h(token))
        return bool(rec) and rec.get("elevated", 0) > time.time()

    def listing(self, current=None):
        cur = _h(current) if current else None
        with self.lock:
            return sorted(({"id": k[:12], "device": v["device"], "kind": v["kind"], "ip": v.get("ip"),
                            "created": round(v["created"]), "seen": round(v["seen"]), "current": k == cur}
                           for k, v in self.s.items() if v["expires"] > time.time()), key=lambda x: -x["seen"])

    def revoke(self, sid=None, token=None, kind=None, keep=None):
        """Revoke one session (by its id or token), or every session of a kind except keep. Returns how many."""
        keep = _h(keep) if keep else None
        with self.lock:
            if token:
                gone = [_h(token)]
            elif sid:
                gone = [k for k in self.s if k[:12] == sid]
            else:
                gone = [k for k, v in self.s.items() if (kind is None or v["kind"] == kind) and k != keep]
            n = sum(1 for k in gone if self.s.pop(k, None))
            if n:
                self._save()
            return n


# ---------- login rate limits ----------
# Per address: 5 free tries an hour, then a wait that doubles with every miss (2 s, 4 s ... 15 min). Every kind
# (lan, remote) also has a global count: from 10 misses an hour a 2 s gap between any two tries, and at the lock
# count login of that kind stops: remote until the owner logs in on the home network, lan for 15 minutes.

class Limiter:
    FREE, MAX_WAIT, GLOBAL_SLOW, GLOBAL_GAP = 5, 900, 10, 2.0
    LOCK_AT = {"lan": 50, "remote": 20}
    LAN_LOCK_S = 900

    def __init__(self, on_lock=None, remote_locked=False):
        self.lock = threading.Lock()
        self.ip, self.all, self.last_try = {}, {"lan": [], "remote": []}, {"lan": 0.0, "remote": 0.0}
        self.locked_until = {"lan": 0.0, "remote": float("inf") if remote_locked else 0.0}
        self.on_lock = on_lock or (lambda kind, n: None)

    def _recent(self, lst, now):
        lst[:] = [t for t in lst if now - t < 3600]
        return lst

    def wait(self, kind, ip):
        """Seconds before this address may try again (0 = now); inf = login of this kind is locked."""
        now = time.time()
        with self.lock:
            if self.locked_until[kind] > now:
                return float("inf")
            mine = self._recent(self.ip.setdefault((kind, ip), []), now)
            w = 0.0
            if len(mine) >= self.FREE:
                w = max(w, mine[-1] + min(2 ** (len(mine) - self.FREE + 1), self.MAX_WAIT) - now)
            if len(self._recent(self.all[kind], now)) >= self.GLOBAL_SLOW:
                w = max(w, self.last_try[kind] + self.GLOBAL_GAP - now)
            if w <= 0:
                self.last_try[kind] = now
            return max(0.0, w)

    def fail(self, kind, ip):
        now = time.time()
        with self.lock:
            self.ip.setdefault((kind, ip), []).append(now)
            n = len(self._recent(self.all[kind], now)) + 1
            self.all[kind].append(now)
            locked = n >= self.LOCK_AT[kind] and self.locked_until[kind] <= now
            if locked:
                self.locked_until[kind] = float("inf") if kind == "remote" else now + self.LAN_LOCK_S
        if locked:
            self.on_lock(kind, n)
        return locked

    def success(self, kind, ip):
        with self.lock:
            self.ip.pop((kind, ip), None)
            if kind == "lan":  # the owner at home: remote login opens again
                self.unlock("remote")

    def unlock(self, kind):
        self.locked_until[kind] = 0.0
        self.all[kind] = []
        for k in [k for k in self.ip if k[0] == kind]:
            self.ip.pop(k, None)

    def remote_locked(self):
        return self.locked_until["remote"] > time.time()


# ---------- who may connect, and to what ----------

PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip",
                 "cf-connecting-ip", "cf-ray", "cf-access-jwt-assertion", "true-client-ip", "tailscale-user-login",
                 "via", "x-original-forwarded-for")
RFC1918 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
TAILNET = [ipaddress.ip_network(n) for n in ("100.64.0.0/10", "fd7a:115c:a1e0::/48")]
HOME = RFC1918 + [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "169.254.0.0/16", "::1/128", "fc00::/7",
                                                    "fe80::/10")]


def ip_of(text):
    a = ipaddress.ip_address(str(text).split("%")[0])
    return a.ipv4_mapped if getattr(a, "ipv4_mapped", None) else a


def home_client(ip, tailscale=False):
    """A client the plain home-network port serves: private, link-local or this PC (and Tailscale's own range when
    the owner turned that on). Anything else is the internet, which only the remote ports serve."""
    try:
        a = ip_of(ip)
    except ValueError:
        return False
    return any(a in n for n in HOME + (TAILNET if tailscale else []))


def proxied(headers):
    return any(headers.get(h) is not None for h in PROXY_HEADERS)


def split_host(value):
    """'Name:port' or '[v6]:port' -> (lower-case name or address, port or None)."""
    v = (value or "").strip().lower()
    m = re.fullmatch(r"\[([0-9a-f:.]+)\](?::(\d+))?", v) or re.fullmatch(r"([^:\[\]]+)(?::(\d+))?", v)
    return (m.group(1), int(m.group(2)) if m.group(2) else None) if m else (None, None)


def host_ok(value, names):
    """The Host header names an address (DNS rebinding needs a name) or one of this hub's own names."""
    h, _ = split_host(value)
    if not h:
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return h.rstrip(".") in names


def origin_ok(headers, host, names=()):
    """A state-changing request comes from this hub's own page: Origin is this host (or one of the remote names);
    with no Origin header, Sec-Fetch-Site must not say cross-site (scripts like hubctl send neither)."""
    o = headers.get("Origin")
    if o is None:
        return (headers.get("Sec-Fetch-Site") or "same-origin") in ("same-origin", "none")
    u = urllib.parse.urlparse(o)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    default = 443 if u.scheme == "https" else 80
    oh, op = u.hostname.lower(), u.port or default
    hh, hp = split_host(host)
    if hh == oh and (hp or default) == op:
        return True
    return oh in names


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an error, never followed: a camera or AI address cannot send the hub (or a key) somewhere else."""
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def urlopen(req, timeout):
    return OPENER.open(req, timeout=timeout)


def lan_address(host, allow_loopback=False):
    """Resolve host once and return the address to connect to: a home-network (RFC 1918) address, or this PC when
    allow_loopback. Never 169.254 (cloud metadata), the internet or anything else."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except (OSError, UnicodeError):
        raise ValueError(f"cannot find {host} on this network")
    for info in infos:
        a = ipaddress.ip_address(info[4][0])
        if any(a in n for n in RFC1918) or (allow_loopback and a.is_loopback):
            return str(a)
    raise ValueError("the camera address must be on your own home network (192.168.x.x, 10.x.x.x or 172.16-31.x.x)")


def pinned(url, allow_loopback=False):
    """An http:// camera address with its name swapped for the checked address (resolved once, so the name cannot
    point somewhere else between the check and the fetch)."""
    u = urllib.parse.urlparse(url or "")
    if u.scheme != "http" or not u.hostname:
        raise ValueError("camera address must start with http:// and the camera's address")
    ip = lan_address(u.hostname, allow_loopback)
    netloc = ip + (f":{u.port}" if u.port else "")
    return urllib.parse.urlunparse(u._replace(netloc=netloc))


def device_name(ua):
    """A short device name from a User-Agent, for the signed-in devices list and the alerts."""
    ua = ua or ""
    m = re.search(r"Pixel[ \w]*?(?=[;)])", ua)
    dev = m.group(0).strip() if m else next((n for p, n in (("iPhone", "iPhone"), ("iPad", "iPad"),
                                                          ("Android", "Android"), ("Windows", "Windows PC"),
                                                          ("Macintosh", "Mac"), ("Linux", "Linux")) if p in ua), None)
    br = next((n for p, n in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("CriOS/", "Chrome"), ("Chrome/", "Chrome"),
                               ("Safari/", "Safari")) if p in ua), None)
    name = " ".join(x for x in (dev, br) if x) or (ua[:30] or "unknown device")
    return re.sub(r"[^\w .()+-]", "", name)[:60]
