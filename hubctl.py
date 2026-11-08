# Command line for C5 Print Hub on this PC (for scripts, or an assistant such as Claude Code): queue a model, slice it,
# look at the camera, check status.
# Usage:
#   python -I hubctl.py status
#   python -I hubctl.py snapshot out.jpg
#   python -I hubctl.py add "Job name" base.stl:1 text.stl:4 [--layer 0.2] [--detail]   (queues and slices; you tap Print)
#   python -I hubctl.py jobs
#   python -I hubctl.py print <job-id> --plate-clear [--level]          (only once you have checked the plate yourself)
#   python -I hubctl.py control pause|resume|cancel|light_on|light_off
import json, os, sys, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))
BASE = f"http://127.0.0.1:{CFG['port']}"


def call(path, data=None, raw=False, method=None):
    body = data if isinstance(data, (bytes, type(None))) else json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body, method=method or ("POST" if body is not None else "GET"),
                                 headers={"X-Hub-Key": CFG["password"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = r.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"hub error {e.code}: {e.read().decode(errors='ignore')}")
    return out if raw else json.loads(out)


def show(job):
    st = job.get("stats") or {}
    print(f"{job['id']}  {job['name']}  [{job['state']}]  {st.get('time', '')}  {st.get('grams', '')} g")
    for p in job.get("parts", []):
        print(f"   {p['name']} -> T{p['tool']}")
    chk = job.get("check") or {}
    if chk:
        print("   check:", "PASSED" if chk.get("passed") else "BLOCKED " + "; ".join(chk.get("reasons", [])))
    if job.get("error"):
        print("   error:", job["error"])


a = sys.argv[1:]
cmd = a[0] if a else "status"
if cmd == "status":
    print(json.dumps(call("/api/status")["printer"], indent=1))
elif cmd == "snapshot":
    open(a[1], "wb").write(call("/cam.jpg", raw=True))
    print("saved", a[1])
elif cmd == "jobs":
    for j in call("/api/jobs")[:10]:
        show(j)
elif cmd == "add":
    name, rest = a[1], a[2:]
    layer = rest[rest.index("--layer") + 1] if "--layer" in rest else "0.2"
    parts = [x for x in rest if ":" in x[2:] and not x.startswith("--")]
    job = None
    for i, spec in enumerate(parts):
        path, tool = spec.rsplit(":", 1)
        q = {"name": os.path.basename(path), "source": "claude", "title": name}
        if job:
            q["job"] = job["id"]
        job = call("/api/upload?" + urllib.parse.urlencode(q), open(path, "rb").read())
        call(f"/api/jobs/{job['id']}/update", {"tools": {str(i): int(tool)}, "layer": layer, "detail": "--detail" in rest})
    call(f"/api/jobs/{job['id']}/slice", {})
    while True:
        time.sleep(2)
        job = call(f"/api/jobs/{job['id']}")
        if job["state"] not in ("queued", "slicing", "new"):
            break
    show(job)
    print("verify:", f"{BASE}/api/jobs/{job['id']}/verify.txt", "preview:", os.path.join(HERE, "jobs", job["id"], "preview.png"))
elif cmd == "print":
    if "--plate-clear" not in a:
        sys.exit("refusing: pass --plate-clear only once someone has looked at the plate and it is clear")
    show(call(f"/api/jobs/{a[1]}/print", {"plate_clear": True, "leveling": "--level" in a}))
elif cmd == "control":
    print(call("/api/control", {"action": a[1]}))
else:
    sys.exit(__doc__ or "see the usage at the top of hubctl.py")
