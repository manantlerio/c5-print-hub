# Run a throwaway copy of the hub against the fake printer. The real hub, its config.json and its jobs are never
# touched: the code is copied fresh into emulator/sim-hub/ on every start, with its own config, jobs and log.
# Run: python -I emulator/sim_hub.py [--port 8766] [--speed 60] [--watch] [--fresh] [--pin 1234] [--copy-job <job-id> ...]
# Then open http://127.0.0.1:8766 on this PC (PIN 1234 unless --pin). --fresh starts at the first-run setup page: open
# the one-time link in emulator/sim-hub/setup-url.txt.
import argparse, json, os, shutil, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.dirname(HERE)
SIM = os.path.join(HERE, "sim-hub")
CODE = ["hub.py", "printer.py", "security.py", "slicer.py", "toolpath.py", "watcher.py", "hubctl.py", "dryer.py",
        "design_check.py", "detail.py"]
sys.path.insert(0, HERE)
sys.path.insert(1, HUB)
import fake_printer, security


def printer_up(ip="127.0.0.1"):
    try:
        req = urllib.request.Request(f"http://{ip}:8898/sim/state")
        urllib.request.urlopen(req, timeout=2).read()
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--speed", type=float, default=60)
    ap.add_argument("--watch", action="store_true", help="run the print watcher (uses Claude on every check)")
    ap.add_argument("--copy-job", nargs="*", default=[], help="real job ids to copy into the sim hub")
    ap.add_argument("--dry-judge", default="file", help="fixed drying bed-check verdict (empty / not_empty / unsure)")
    ap.add_argument("--engine", choices=["flashstudio", "forkaslicer"], help="slicer (default: the real hub's)")
    ap.add_argument("--pin", default="1234", help="PIN of the sim hub")
    ap.add_argument("--fresh", action="store_true", help="start before setup (the first-run setup page)")
    ap.add_argument("--printer-ip", default="127.0.0.1", help="loopback address of the fake printer (127.0.0.3 keeps clear of another one)")
    ap.add_argument("--set", action="append", default=[], help="extra config key=JSON value, e.g. remote_tunnel_port=8779")
    a = ap.parse_args()

    os.makedirs(os.path.join(SIM, "jobs"), exist_ok=True)
    for f in CODE:
        shutil.copy(os.path.join(HUB, f), os.path.join(SIM, f))
    for d in ("static", "slicer_scripts", "template"):
        shutil.rmtree(os.path.join(SIM, d), ignore_errors=True)
        if os.path.isdir(os.path.join(HUB, d)):
            shutil.copytree(os.path.join(HUB, d), os.path.join(SIM, d))
    for jid in a.copy_job:
        dst = os.path.join(SIM, "jobs", jid)
        if not os.path.exists(dst):
            shutil.copytree(os.path.join(HUB, "jobs", jid), dst, ignore=shutil.ignore_patterns("slice"))

    path = os.path.join(HUB, "config.json")
    real = security.cfg_from_disk(json.load(open(path, encoding="utf-8"))) if os.path.exists(path) else {}
    cfg = {"printer_ip": a.printer_ip, "serial": fake_printer.SERIAL, "check_code": fake_printer.CHECK,
           "port": a.port, "host": "127.0.0.1", "colours": real.get("colours") or ["#FFFFFF", "#1E5BD8", "#F5D000", "#FF69B4"],
           "leveling": real.get("leveling", False),
           "watch": a.watch, "watch_backend": real.get("watch_backend", "") if a.watch else "",
           "anthropic_api_key": real.get("anthropic_api_key", "") if a.watch else "",
           "watch_every_min": real.get("watch_every_min", 2), "watch_action": "pause",
           "ntfy_topic": "",  # never push simulator alerts to your phone
           "eta_window_s": 30, "eta_min_span_s": 6, "eta_prior_s": 0, "eta_clamp": 0,  # the sim runs many times faster than a real print
           "engine": a.engine or real.get("engine", "flashstudio"), "pin_chosen": True,
           "pin_hash": security.hash_secret(a.pin), "setup_done": not a.fresh,
           "camera_allow_loopback": True,  # the fake printer's camera and the test servers live on this PC
           "dry_judge": a.dry_judge, "dry_hours_min": 0.005, "dry_check_delay_s": 0}  # drying bed check verdict without calling Claude
    if a.fresh:  # setup page test: no printer known yet
        cfg.update(printer_ip="", serial="", check_code="", pin_chosen=False, pin_hash="")
    for kv in a.set:
        k, _, v = kv.partition("=")
        cfg[k] = json.loads(v)
    json.dump(cfg, open(os.path.join(SIM, "config.json"), "w"), indent=1)

    kids = []
    if not printer_up(a.printer_ip):
        kids.append(subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "fake_printer.py"),
                                      "--speed", str(a.speed), "--host", a.printer_ip]))
        for _ in range(20):
            if printer_up(a.printer_ip):
                break
            time.sleep(0.5)
    kids.append(subprocess.Popen([sys.executable, "-I", os.path.join(SIM, "hub.py")], cwd=SIM))
    print(f"sim hub on http://127.0.0.1:{a.port} against the fake printer (Ctrl+C stops both)", flush=True)
    try:
        kids[-1].wait()
    except KeyboardInterrupt:
        pass
    finally:
        for k in kids:
            k.terminate()


if __name__ == "__main__":
    main()
