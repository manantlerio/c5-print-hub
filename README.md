# C5 Print Hub

**A phone page and print hub for the Flashforge Creator 5 Pro, unofficial.**

C5 Print Hub runs on a Windows PC on the same network as your printer. From your phone you upload a model, slice it
with your own Flash Studio settings, send it, and then watch, pause or cancel the print. It talks to the printer
directly over your own network (LAN Only mode, stock firmware). No cloud account is needed.

Not made, endorsed or supported by Flashforge. Flashforge, Creator 5 Pro and Flash Studio are names of their owners.
Use it at your own risk: it sends real commands to a real printer.

## Features

- **Jobs**: upload an STL and slice it here, or upload a G-code or sliced 3MF from Flash Studio. 2D preview and a 3D
  spin viewer. Every upload and slice is kept in `jobs\`.
- **Slicing with your own settings**: the hub runs the Flash Studio slicer with the exact settings of a plate you
  sliced yourself in Flash Studio (set up once on the setup page). Up to 4 colours, one STL per tool.
- **Checks before every send**: each slice is compared with your Flash Studio export (settings, start and end
  G-code, tool changes, temperatures, bed bounds, extrusion). The design check reads the toolpath for a thin first
  layer, floating parts, long spans and pieces fused together. A file that fails is not sent unless you tap Send anyway.
- **Live status**: percent, layer, time left (worked out from the G-code and the real pace), temperatures, the
  filament in each slot.
- **Controls**: pause, resume, cancel, light, nozzle and bed temperature, speed. A changed temperature or speed is
  held through tool changes.
- **Cameras**: the printer's own camera, or an old phone above the bed running an IP camera app. Full screen with
  pinch zoom.
- **Drying mode**: holds bed and chamber heat for a set time with spools on the bed, and locks printing until the bed
  is checked empty again (by camera with the watcher, or by you, by eye).
- **Detail mode**: finer layers only where slopes and curves would show steps.
- **Failure watcher (optional, off by default)**: an AI looks at a camera frame every few minutes and pauses the
  print on a clear failure. Claude is the tested choice; other OpenAI-compatible AIs work as an experimental option.
- **Push alerts (optional)**: through ntfy (ntfy.sh or your own server) when a print finishes, pauses or errors, and
  on every new sign-in, remote start and remote cancel.
- **Remote access (optional)**: Tailscale, a Cloudflare Tunnel, or the hub's own HTTPS port, with a passphrase and an
  authenticator code. Full control from away, the same as at home. See [Security and remote access](#security-and-remote-access).

## What you need

- Windows 10 or 11 PC that stays on while you print, on the same network as the printer.
- Python 3.10 or newer from [python.org](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
  The hub uses only the standard library. Pillow is optional (`pip install --user pillow`): it adds a zoomed
  close-up to watcher checks.
- [Flash Studio](https://www.flashforge.com) installed on the same PC, for slicing STLs. Without it you can still
  upload files you sliced elsewhere.
- The printer on stock firmware, in **LAN Only mode**.

## Setup on Windows

1. Download this folder (Code - Download ZIP, then unzip) to somewhere that stays, for example `Documents\c5-print-hub`.
2. On the printer screen, turn on **LAN Only** mode in the network settings. The hub can only control the printer in
   LAN Only mode. The screen then shows the printer's check code.
3. In the folder, right-click an empty spot, Open in Terminal, and run:

   ```
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```

   It finds Python, starts the hub now and at every login, adds a firewall rule for your home network only (Windows
   asks once for administrator rights), and opens the one-time setup link. Windows may block a downloaded script: if
   so, right-click `setup.ps1`, Properties, tick Unblock, and run it again.
4. If Windows asks whether Python may use networks, allow **Private networks** only.
5. On the setup page (the link is also in `setup-url.txt` in the folder; it works on this PC only, until setup is
   done):
   1. **Printer**: Find my printer (or type its address), then the serial number and check code from the printer
      screen. Connect and test.
   2. **PIN**: the PIN your phone asks for once.
   3. **Cameras**: check the printer camera. Optional: an old phone with an IP camera app (for example IP Webcam on
      Android). Type the address the app shows and tap Test for a live picture.
   4. **Slicing settings**: in Flash Studio, set up all 4 filaments with the presets you print with, put any model on
      the plate and slice it. Back on the setup page tap Look for my latest slice, then Use this. Do this again
      whenever you change a preset. Tick "Only allow these filament presets" to block files sliced on any other preset.
   5. **Watcher**: optional, see below.
   6. **Done**: the page shows the address to open on your phone, like `http://192.168.1.20:8765`. Add it to your home
      screen.
   7. Optional, any time later from the setup page: **Phone alerts** (ntfy) and **Remote access**.

To run it by hand instead of at login: `python -I hub.py`. To remove the login task:
`powershell -ExecutionPolicy Bypass -File setup.ps1 -Uninstall`.

## Security and remote access

The hub controls heaters and motors, so it is built to keep strangers out while you keep full control. At home you
use a PIN. Away from home you use a passphrase and a 6-digit code from an authenticator app, and then you can do
everything you can do at home: start a print, pause, cancel, temperatures, speed, uploads, drying.

### What is protected

- **The home port** (8765) is plain http for your own network. It refuses internet addresses, and anything that came
  through a proxy or tunnel, so a forwarded router port gets nothing. `setup.ps1` limits it in the Windows Firewall to
  the Private profile and your local subnet.
- **First run**: the setup page opens only with the one-time link from `setup-url.txt`, and until setup is done the
  hub listens on this PC only.
- **Login**: a few wrong PINs from one address make it wait, doubling each time; many wrong tries from anywhere slow
  every try down and then pause home login for 15 minutes. Every miss is logged in `hub.log`. Right and wrong answers
  take the same time.
- **Devices**: every phone or browser gets its own random session (the hub keeps only a hash of it), 30 days at home
  and 7 days remote, renewed as you use it. The setup page lists them with a sign-out button. A new PIN signs out
  every other home device; a new passphrase or authenticator signs out every remote one.
- **Remote login**: a passphrase of 12 or more characters plus the authenticator code, each code once. After 20
  wrong tries in an hour remote login locks until you log in once at home (or tap Open remote login again on the
  setup page), and your phone gets a push. A phone already signed in remotely keeps working.
- **Alerts**: a push on every new sign-in, every remote start and every remote cancel. Every remote action is logged
  with the device name.
- **Changes that move secrets or traffic** (printer address, camera address, AI address or key, alerts, PIN,
  passphrase) ask for the passphrase again (the PIN when no passphrase is set). One yes covers 10 minutes.
- **Safe remote start**: the Send sheet shows a fresh camera frame and the time it was taken. Tap **Mark bed cleared**
  after you look at the bed (at home or on the camera). If a remote start comes with no mark since the last print
  ended, the hub asks once more before it starts. It never refuses: you decide.
- **Hard limits nothing overrides**: a file is never sent if it heats the nozzle over 300 C, the bed over 110 C or the
  chamber over 65 C (set lower with `hard_caps`), or if it holds a firmware-level command such as `SAVE_CONFIG`,
  `FIRMWARE_RESTART`, `M500`, `M502`, `M997`, `M302`, `M211` or a forced move. The check reads every spelling the
  firmware would (lower case, leading spaces, line numbers, Klipper commands and macros, the R parameter). Send
  anyway only skips the soft checks (material ranges, presets, design). The printer's own firmware limits were not
  tested by this project.
- **Requests**: only this hub's own page can make changes (Host check, Origin check, JSON or file bodies only).
  Sizes, time and connections are capped, so a flood or a slow client cannot lock your phone out. Uploads go
  straight to disk; a 3MF that unpacks to more than 500 MB is refused.
- **What the hub fetches**: camera addresses must be on your home network, redirects are never followed and only
  pictures pass through. An AI address must be https unless it is your own server on this PC or network; a saved key
  is cleared when the address changes, and the server's own reply text is never shown.
- **Pages**: printer names and colours are checked before they reach the page; the pages cannot be framed by
  another site and load nothing from anywhere else.
- **Secrets on disk**: the PIN and passphrase are stored as scrypt hashes. API keys, the printer check code, the
  authenticator secret, the ntfy topic and token and the camera address are encrypted in `config.json` with Windows
  DPAPI, locked to the Windows account that runs the hub. Nothing to set up and no extra password: the same login
  opens them, a copy of the file on another PC or account does not. Moved the hub to a new PC? The setup page asks
  for those values again. You can still type a key into `config.json` by hand; it is encrypted at the next start.
- **hubctl.py** uses a random API token from `config.json`, accepted from this PC only. The PIN is never an API key.

### Away from home: pick one

**Never forward the home port (8765) on your router.** Pick one of these instead. All three keep the full controls.

**A. Tailscale (most private, nothing open to the internet).** Free for personal use; it needs the Tailscale app on
every phone that uses the hub.

1. Install [Tailscale](https://tailscale.com/download) on the hub PC and your phone, signed in to the same account.
2. Run `powershell -ExecutionPolicy Bypass -File setup.ps1 -Tailscale`. The home port then also answers Tailscale
   addresses (`"tailscale": true`), and the firewall rule includes them.
3. On the phone open `http://<the PC's Tailscale address>:8765` (the 100.x address from the Tailscale app) and log in
   with your PIN, as at home. To use the PC's full Tailscale name instead, add it to `lan_hosts` in `config.json`,
   for example `"lan_hosts": ["printpc.tail1234.ts.net"]`.

Never use `tailscale funnel` (it puts the hub on the open internet). If an older version set up `tailscale serve`
for you, remove it with `tailscale serve reset`: the home port now refuses proxied requests.

**B. Cloudflare Tunnel with Cloudflare Access (no open port, works in any browser).** Needs a domain on Cloudflare
(about US$10 a year) and a free Cloudflare account. Cloudflare carries the traffic and can see it.

1. At home, on the setup page: **Remote access**, set a passphrase and an authenticator.
2. In `config.json` set `"remote_tunnel_port": 8766` and `"remote_hosts": ["hub.example.com"]` (your own name), then
   restart the hub. Port 8766 listens on this PC only (127.0.0.1); the hub treats everything on it as remote.
3. Install cloudflared on the hub PC: `winget install --id Cloudflare.cloudflared`.
4. In the Cloudflare dashboard, Zero Trust, Networks, Tunnels: **Create a tunnel** (Cloudflared), give it a name, and
   run the install command it shows (`cloudflared.exe service install ...`) in an administrator terminal.
5. In the tunnel, add a **public hostname**: subdomain `hub`, your domain, service type **HTTP**, URL
   `127.0.0.1:8766`. Never point it at 8765.
6. Zero Trust, Access, Applications: **Add an application**, Self-hosted, domain `hub.example.com`. Add a policy:
   action Allow, include Emails, your own address. Keep the one-time PIN login method. Save.
7. On the phone open `https://hub.example.com`. Cloudflare asks for your email and a code first, then the hub asks
   for your passphrase and authenticator code. Add it to the home screen.

**C. The hub's own HTTPS port, with a router port forward.** For when you have no domain on Cloudflare and do not
want an app on every phone. The hub then faces the internet directly: it is built for that (the lockout, caps and
timeouts above), but A and B keep strangers from reaching it at all.

1. At home, on the setup page: **Remote access**, set a passphrase and an authenticator.
2. Get a name that points at your home: a free [DuckDNS](https://www.duckdns.org) name, or one on your own domain.
3. Get a certificate for that name as PEM files, with [win-acme](https://www.win-acme.com) (choose the PEM files
   store) or [Caddy](https://caddyserver.com). DNS validation (DuckDNS has a token for it) needs no extra open port;
   HTTP validation needs port 80 forwarded while it renews. Python cannot get certificates by itself.
4. In `config.json` set `"remote_https_port": 8443`, `"tls_cert"` and `"tls_key"` to the two PEM files (in
   JSON, write `\\` for each `\` in a path), and `"remote_hosts": ["myhub.duckdns.org"]`. Restart the hub.
5. Run `powershell -ExecutionPolicy Bypass -File setup.ps1 -HttpsPort 8443` for the firewall rule.
6. On the router forward TCP 8443 to this PC, port 8443. Only that port.
7. On the phone open `https://myhub.duckdns.org:8443` and log in with your passphrase and code.

The hub reads the certificate when it starts: restart it after each renewal (win-acme can run
`schtasks /End /TN "C5 Print Hub"` and `schtasks /Run /TN "C5 Print Hub"` after it renews). A self-signed
certificate makes phones warn every time; use it for testing only.

### Upgrading from an older version

Every phone logs in once more (sessions replaced the shared cookie). `hubctl.py` now uses `api_token` from
`config.json`. A PIN stored in plain text by an older version is hashed at the first start.

## Failure watcher (optional)

Off until you set it up. During a print, a fast model looks at one camera frame every 2 minutes, told what colours
should be on the plate and whether there is a prime tower. A flag gets a quick re-check on a fresh frame. Only when an
action is due does a stronger model look: it pauses the print on a clear failure (spaghetti, a part knocked loose, a
big blob), or sends one alert when unsure. A pause can be resumed; the watcher never cancels.

Ways to run it:

- **Claude, your own Anthropic API key (tested)**: make a key at
  [console.anthropic.com](https://console.anthropic.com) and paste it on the setup page. Haiku looks, Sonnet
  re-checks. Each check sends one camera frame to Anthropic and is billed to your account, a small fraction of a cent
  per check.
- **Claude Code on this PC (tested)**: if you already use the Claude Code CLI and are signed in, the hub can call it
  instead.
- **Other AI, OpenAI-compatible (experimental - not tuned, may miss failures or pause good prints)**: any service or
  local server with an OpenAI-style Chat Completions API and a model that can read images. The setup page has presets
  for OpenAI, Google Gemini, OpenRouter, Ollama (local, `http://localhost:11434/v1`) and LM Studio (local,
  `http://localhost:1234/v1`). Type the server address, an API key (blank for a local server), a fast model and
  optionally a strong model for the re-check (blank = the fast model again). The model names the presets fill in are
  examples only. The prompt and the pause rule were tuned on Claude, so watch the first prints closely.

API keys are stored encrypted on this PC (see Security) and never shown on the page again. Tap Test on the
setup page to try one check on the printer camera with the AI you chose; a model that cannot read images gives a
plain error there. Without the watcher, the drying bed check asks you to look at the bed yourself and unlock it.

## Config (`config.json`)

The setup page writes `config.json` for you. `config.example.json` shows every key. The ones you may want to change
by hand:

| Key | Default | What it does |
|---|---|---|
| `port` | 8765 | port of the phone page (home network only) |
| `host` | 0.0.0.0 | 0.0.0.0 = phones on your network; 127.0.0.1 = this PC only |
| `tailscale` | false | the home port also answers Tailscale addresses (set by `setup.ps1 -Tailscale`) |
| `lan_hosts` | none | extra names the home port answers to, e.g. the PC's Tailscale name |
| `remote_tunnel_port` | 0 (off) | remote port on 127.0.0.1 for a Cloudflare Tunnel |
| `remote_https_port`, `tls_cert`, `tls_key` | 0 (off), empty | remote HTTPS port and your certificate (PEM files) |
| `remote_hosts` | none | the names your remote address uses, e.g. `hub.example.com` (needed for remote ports) |
| `hard_caps` | nozzle 300, bed 110, chamber 65 | temperatures no file may go over; can only be set lower |
| `watch_every_min` | 2 | minutes between watcher checks |
| `watch_action` | pause | `pause`, or `alert` (push only) |
| `watch_model` / `watch_confirm_model` | haiku / sonnet | Claude: first look and second opinion; empty second = first decides |
| `openai_base_url`, `openai_model`, `openai_strong_model` | empty | OpenAI-compatible watcher (experimental), set on the setup page |
| `openai_timeout_s` | 120 | seconds to wait for an OpenAI-compatible answer (local models can be slow) |
| `webcam_url`, `webcam_fps` | empty, 8 | phone camera address |
| `ntfy_server`, `ntfy_topic`, `ntfy_token` | ntfy.sh, empty, empty | push alerts, set on the setup page |
| `leveling` | false | bed leveling before each print |
| `material_ranges` | PLA, PETG, TPU | nozzle and bed ranges a sliced file must stay inside |
| `z_offset_by_material` | none | extra Z per material at send, mm, + = higher, e.g. `{"PETG": 0.05}` |
| `house_presets` | none | filament presets a file must use (set by the setup page tick box) |
| `flash_studio` | Program Files path | where `flash studio.exe` is, if not in the usual place |
| `engine` | flashstudio | `forkaslicer` (an OrcaSlicer fork run in WSL, with `forkaslicer_path`) is experimental and off |

Restart the hub after editing `config.json` by hand (Task Scheduler, C5 Print Hub, End then Run).

## Tests

Offline, no printer, Flash Studio or AI needed:

```
python -I emulator\test_public.py
python -I emulator\test_watcher.py
python -I emulator\test_openai.py
python -I emulator\test_security.py
```

`test_security.py` checks each security fix against a fresh hub: the setup link, brute force and lockout, bad
Content-Length, slow and idle clients, the connection cap, Host and Origin rules, sessions and sign-out, the remote
port with passphrase and authenticator code (RFC 6238 test vectors), step-up, safe remote start, the G-code hard
check (lower case, macros, leading spaces, the R parameter), camera and AI redirects, zip bombs, page escaping and
security headers. With `openssl` on the PATH (Git for Windows has it) it also tries the HTTPS port.

`test_openai.py` runs the OpenAI-compatible watcher against `emulator\mock_openai.py`, a stand-in server on this PC:
a good verdict, a garbled reply, HTTP errors, a timeout and a model that cannot read images.

`test_public.py` runs a fresh hub against `emulator\fake_printer.py` (a stand-in Creator 5 Pro on this PC) through
the whole first run: setup page, PIN, cameras, template, watcher, phone page, send, pause, cancel. Add
`--template <your Flash Studio export.3mf>` to also check your own export. `emulator\sim_hub.py` runs a throwaway
copy of the hub against the fake printer to try things by hand (`--fresh` starts at the setup page).

## Files

`hub.py` server and page API, `printer.py` printer LAN client and camera, `slicer.py` slicing, checks and stats,
`slicer_scripts\` model and settings builders for the Flash Studio slicer, `watcher.py` failure watcher,
`dryer.py` drying mode, `design_check.py` design check, `detail.py` Detail mode, `toolpath.py` 3D viewer data,
`hubctl.py` command line, `security.py` sessions, login limits, TOTP and address checks, `static\` phone and setup
pages, `emulator\` fake printer and tests.

Not in the repo (made on your PC): `config.json`, `sessions.json`, `setup-url.txt`, `template\` (your Flash
Studio export), `jobs\`, `watch\`, `drying\`, `hub.log`.

## Licence

Copyright (C) 2026 manantlerio.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
Public License as published by the Free Software Foundation, version 3. It is distributed WITHOUT ANY WARRANTY. See
[LICENSE](LICENSE) for the full text.

`static\vendor\three-0.169.0\` is three.js, MIT licence (its LICENSE file is alongside).
