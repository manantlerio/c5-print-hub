# Build a project config for an N-colour print from the config Flash Studio itself used on this printer.
# Usage: python build_from_gui.py <gui-export.3mf> <model.3mf> <out-dir> <layer_height> <#colour> [<#colour> ...]
#   gui-export.3mf: a real GUI-sliced file, e.g. %LOCALAPPDATA%\Temp\orca-flashforge\slice\<pid>.3mf
#   model.3mf:      model with parts preassigned to extruders (make_3mf.py)
# Writes <out-dir>/project_settings.config and <out-dir>/slice-input.3mf (model + config) for the CLI.
import json, os, sys, zipfile

TEMPLATE, SRC, OUTDIR, LAYER = sys.argv[1:5]
COLOURS = sys.argv[5:]
N = len(COLOURS)
HERE = os.path.dirname(os.path.abspath(__file__))

g = json.loads(zipfile.ZipFile(TEMPLATE).read("Metadata/project_settings.config"))

# Per-filament settings are one-entry lists in a 1-colour export; repeat them once per colour.
fk = set(json.load(open(os.path.join(HERE, "filament_keys.json")))) | {
    "activate_air_filtration_during_print", "activate_air_filtration_on_completion", "additional_fan_full_speed_layer",
    "chamber_minimal_temperature", "close_additional_fan_first_x_layers", "ironing_fan_speed"}
for k, v in list(g.items()):
    if isinstance(v, list) and len(v) == 1 and (k in fk or k.startswith("filament_")):
        g[k] = v * N

g["filament_colour"] = COLOURS
g["filament_multi_colour"] = COLOURS
g["filament_self_index"] = [str(i + 1) for i in range(N)]
g["filament_map"] = [str(i + 1) for i in range(N)]
# The export holds a 1-colour purge table. Drop it so the engine computes the full table for these colours;
# a short table with the 4-entry flush_multiplier makes the CLI fail with "found slicing or export error".
g.pop("flush_volumes_matrix", None)
g.pop("flush_volumes_vector", None)
g["layer_height"] = LAYER
# VoxelShare builds its description from this name, so make it match the real layer height.
g["print_settings_id"] = f"{float(LAYER):.2f}mm Standard @FF C5"

os.makedirs(OUTDIR, exist_ok=True)
ps = os.path.join(OUTDIR, "project_settings.config")
json.dump(g, open(ps, "w"), indent=1)
with zipfile.ZipFile(SRC) as a, zipfile.ZipFile(os.path.join(OUTDIR, "slice-input.3mf"), "w", zipfile.ZIP_DEFLATED) as b:
    for name in a.namelist():
        b.writestr(name, a.read(name))
    b.write(ps, "Metadata/project_settings.config")
print("built", ps)
