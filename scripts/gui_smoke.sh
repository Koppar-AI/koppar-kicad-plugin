#!/usr/bin/env bash
# Route -> Apply -> Undo inside the real KiCad PCB editor, on a private X display.
#
#   scripts/gui_smoke.sh board.kicad_pcb [server] [out-dir]
#   KOPPAR_SMOKE_ZIP=dist/koppar-kicad-<v>.zip scripts/gui_smoke.sh ...   (the PCM package)
#
# The server reaches the plugin as KOPPAR_SERVER, which only a source checkout
# reads: the package (KOPPAR_SMOKE_ZIP) talks to Koppar's servers whatever is given.
#   KOPPAR_SMOKE_TASK=auto_place KOPPAR_SMOKE_API_KEY=sc_... scripts/gui_smoke.sh ...   (an account task)
#
# Needs Xvfb (DISPLAY is left alone when already set) and pcbnew. KiCad runs on
# a throwaway config and documents folder; the user's KiCad setup is only read
# (its settings and library tables are copied, the start wizard's prompts marked
# answered, and the canvas set to software rendering).
set -euo pipefail
board=$(realpath "$1")
server=${2:-}
out=$(realpath -m "${3:-$(mktemp -d)/smoke}")
here=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
# another KiCad than the installed one: KOPPAR_SMOKE_KICAD=<unpacked .deb root> KOPPAR_SMOKE_SERIES=9.0
prefix=${KOPPAR_SMOKE_KICAD:-}
series=${KOPPAR_SMOKE_SERIES:-10.0}
if [ -n "$prefix" ]; then
  export LD_LIBRARY_PATH="${KOPPAR_SMOKE_LIBS:+$KOPPAR_SMOKE_LIBS:}$prefix/usr/lib/x86_64-linux-gnu"
  export PYTHONPATH="$prefix/usr/lib/python3/dist-packages${KOPPAR_SMOKE_PYTHONPATH:+:$KOPPAR_SMOKE_PYTHONPATH}"
fi

mkdir -p "$work/config/$series/scripting/plugins" "$out"
# the user's settings, copied (a config dir without them opens the KiCad Setup wizard)
cp "$HOME"/.config/kicad/10.0/*.json "$work/config/$series/" 2>/dev/null || true
# for screenshots: the grid off (KOPPAR_SMOKE_GRID=0) and the frame's size (KOPPAR_SMOKE_FRAME=1920x1200)
python3 - "$work/config/$series/pcbnew.json" <<'PY'
import json, os, sys
p = sys.argv[1]
if os.path.exists(p):
    d = json.load(open(p))
    if os.environ.get("KOPPAR_SMOKE_GRID") == "0":
        d.setdefault("window", {}).setdefault("grid", {})["show"] = False
    if os.environ.get("KOPPAR_SMOKE_FRAME"):
        # the editor's frame size, e.g. 1920x1200, at the top left
        w, h = (int(v) for v in os.environ["KOPPAR_SMOKE_FRAME"].split("x"))
        d.setdefault("window", {}).update(size_x=w, size_y=h, pos_x=0, pos_y=0, maximized=False)
    json.dump(d, open(p, "w"), indent=2)
PY
for f in fp-lib-table sym-lib-table design-block-lib-table; do
  [ -f "$HOME/.config/kicad/10.0/$f" ] && cp "$HOME/.config/kicad/10.0/$f" "$work/config/$series/"
done
# software rendering: a virtual display has no OpenGL for the canvas
python3 - "$work/config/$series/kicad_common.json" <<'PY'
import json, os, sys
p = sys.argv[1]
d = json.load(open(p)) if os.path.exists(p) else {}
d.setdefault("graphics", {})["canvas_type"] = int(os.environ.get("KOPPAR_SMOKE_CANVAS", "2"))
# answered, so the start wizard does not open over the editor
d.setdefault("system", {})["first_run_shown"] = True
d.setdefault("do_not_show_again", {}).update(data_collection_prompt=True, update_check_prompt=True)
json.dump(d, open(p, "w"), indent=2)
PY
if [ -n "${KOPPAR_SMOKE_ZIP:-}" ]; then
  # the shipped package, unpacked where the Plugin and Content Manager puts it
  # (3rdparty/plugins/<identifier with dots as underscores>/)
  python3 -c 'import sys, zipfile, os
z = zipfile.ZipFile(sys.argv[1])
for n in z.namelist():
    if n.startswith("plugins/") and not n.endswith("/"):
        dest = os.path.join(sys.argv[2], n[len("plugins/"):])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(z.read(n))' "$KOPPAR_SMOKE_ZIP" "$work/config/$series/scripting/plugins/ai_koppar_kicad"
else
  cp -r "$here/koppar" "$work/config/$series/scripting/plugins/"
fi
# the driver: this smoke test, or another plugin file that drives the window
cp "${KOPPAR_SMOKE_DRIVER:-$here/scripts/gui_smoke.py}" "$work/config/$series/scripting/plugins/koppar_gui_smoke.py"
mkdir -p "$work/board"
cp "$board" "$work/board/"
stem=$(basename "${board%.kicad_pcb}")
for ext in kicad_pro kicad_dru; do
  [ -f "${board%.kicad_pcb}.$ext" ] && cp "${board%.kicad_pcb}.$ext" "$work/board/"
done

# the plugin's settings, in the throwaway config: the key when one is given
mkdir -p "$work/xdg/koppar"
python3 -c 'import json, sys; json.dump({"api_key": sys.argv[1]}, open(sys.argv[2], "w"))' \
  "${KOPPAR_SMOKE_API_KEY:-}" "$work/xdg/koppar/kicad-plugin.json"
export KICAD_CONFIG_HOME="$work/config" KICAD_DOCUMENTS_HOME="$work/docs"
export KOPPAR_SMOKE_OUT="$out"
if [ -n "$server" ]; then export KOPPAR_SERVER="$server"; fi
# the settings file goes to the throwaway config too
export XDG_CONFIG_HOME="$work/xdg"
status=0
# a session bus of its own: wx waits on one at start, 25 s per try, when there is none
timeout 1200 dbus-run-session -- env ${KOPPAR_SMOKE_PYTHONHOME:+PYTHONHOME=$KOPPAR_SMOKE_PYTHONHOME} "${prefix:+$prefix/usr/bin/}pcbnew" "$work/board/$stem.kicad_pcb" > "$out/pcbnew.log" 2>&1 || status=$?
echo "pcbnew exit $status; report in $out/report.json"
exit $status
