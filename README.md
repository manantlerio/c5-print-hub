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
- **Push alerts (optional)**: through ntfy.sh when a print finishes, pauses or errors.

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

   It finds Python, starts the hub now and at every login, and opens the setup page. Windows may block a downloaded
   script: if so, right-click `setup.ps1`, Properties, tick Unblock, and run it again.
4. When Windows asks whether Python may use networks, allow **Private networks** only.
5. On the setup page (`http://127.0.0.1:8765/setup` on this PC):
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

To run it by hand instead of at login: `python -I hub.py`. To remove the login task:
`powershell -ExecutionPolicy Bypass -File setup.ps1 -Uninstall`.

## Keep it off the internet

The hub is made for your own home network. **Never forward its port on your router, and never put it on the open
internet.** Anyone who reaches it and guesses the PIN can heat the bed and nozzles and start a print on your printer.
The PIN only keeps out people on your own Wi-Fi.

For access away from home, use [Tailscale](https://tailscale.com) (free for personal use): install it on the PC and
your phone, sign in on both, then run `powershell -ExecutionPolicy Bypass -File setup.ps1 -Tailscale`. The hub then
listens on the PC only and `tailscale serve` passes your own devices through. Do not use `tailscale funnel`.

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

API keys are stored in plain text in `config.json` on this PC and never shown on the page again. Tap Test on the
setup page to try one check on the printer camera with the AI you chose; a model that cannot read images gives a
plain error there. Without the watcher, the drying bed check asks you to look at the bed yourself and unlock it.

## Config (`config.json`)

The setup page writes `config.json` for you. `config.example.json` shows every key. The ones you may want to change
by hand:

| Key | Default | What it does |
|---|---|---|
| `port` | 8765 | port of the phone page |
| `host` | 0.0.0.0 | 0.0.0.0 = phones on your network; 127.0.0.1 = this PC only (Tailscale) |
| `watch_every_min` | 2 | minutes between watcher checks |
| `watch_action` | pause | `pause`, or `alert` (push only) |
| `watch_model` / `watch_confirm_model` | haiku / sonnet | Claude: first look and second opinion; empty second = first decides |
| `openai_base_url`, `openai_model`, `openai_strong_model` | empty | OpenAI-compatible watcher (experimental), set on the setup page |
| `openai_timeout_s` | 120 | seconds to wait for an OpenAI-compatible answer (local models can be slow) |
| `webcam_url`, `webcam_fps` | empty, 8 | phone camera address |
| `ntfy_topic` | empty | push alerts through ntfy.sh (pick a long random topic name) |
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
```

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
`hubctl.py` command line, `static\` phone and setup pages, `emulator\` fake printer and tests.

Not in the repo (made on your PC): `config.json`, `template\` (your Flash Studio export), `jobs\`, `watch\`,
`drying\`, `hub.log`.

## Licence

Copyright (C) 2026 manantlerio.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
Public License as published by the Free Software Foundation, version 3. It is distributed WITHOUT ANY WARRANTY. See
[LICENSE](LICENSE) for the full text.

`static\vendor\three-0.169.0\` is three.js, MIT licence (its LICENSE file is alongside).
