"""Build the Plugin and Content Manager archive.

    python3 build_package.py [-o dist/]

Writes dist/koppar-kicad-<version>.zip:

    metadata.json            the PCM listing (version from koppar/config.py)
    plugins/                 koppar/ with its assets, installed as 3rdparty/plugins/ai_koppar_kicad/
    resources/icon.png       64 px icon shown in the PCM

Install it with Plugin and Content Manager > Install from File. The package
talks to Koppar's servers only: config.py's PACKAGED is set in it, which turns
off the KOPPAR_SERVER override a source checkout has for development.
"""

import argparse
import json
import os
import re
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(ROOT, "koppar")


def version():
    with open(os.path.join(PKG, "config.py"), encoding="utf-8") as f:
        return re.search(r'^VERSION = "([^"]+)"', f.read(), re.M).group(1)


def metadata(ver):
    return {
        "$schema": "https://go.kicad.org/pcm/schemas/v1",
        "name": "Koppar",
        "description": "Autoroute, place, finish, polish and check your board with Koppar, from the PCB editor",
        "description_full": (
            "Koppar inside the PCB editor: autoroute the board you have open, or with a Koppar account place "
            "it, finish it, cut its vias, couple its pairs, check it and ask Koppar about it. Whatever a task "
            "changes comes back as one step on KiCad's undo stack. Small boards autoroute without an account."
        ),
        "identifier": "ai.koppar.kicad",
        "type": "plugin",
        "author": {"name": "Koppar", "contact": {"web": "https://koppar.ai"}},
        "license": "Apache-2.0",
        "resources": {"homepage": "https://koppar.ai"},
        "tags": ["pcbnew", "routing", "autorouter", "ai"],
        "versions": [
            {
                "version": ver,
                "status": "stable",
                "kicad_version": "7.0",
                "platforms": ["linux", "macos", "windows"],
            }
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out-dir", default=os.path.join(ROOT, "dist"))
    a = ap.parse_args()

    ver = version()
    os.makedirs(a.out_dir, exist_ok=True)
    out = os.path.join(a.out_dir, f"koppar-kicad-{ver}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps(metadata(ver), indent=2))
        z.write(os.path.join(ROOT, "resources", "icon.png"), "resources/icon.png")
        # the license and its notice travel with every copy (Apache-2.0, section 4)
        for name in ("LICENSE", "NOTICE"):
            z.write(os.path.join(ROOT, name), f"plugins/{name}")
        for folder, dirs, files in os.walk(PKG):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if not name.endswith((".py", ".png", ".svg", ".ttf", ".txt")):
                    continue
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, PKG).replace(os.sep, "/")
                if rel == "config.py":
                    # a release talks to Koppar's servers only: KOPPAR_SERVER is for a source checkout
                    with open(path, encoding="utf-8") as f:
                        text, n = re.subn(r"^PACKAGED = False$", "PACKAGED = True", f.read(), flags=re.M)
                    if n != 1:
                        raise SystemExit("config.py has no PACKAGED = False line to set")
                    z.writestr("plugins/config.py", text)
                else:
                    z.write(path, f"plugins/{rel}")
    print(out)


if __name__ == "__main__":
    main()
