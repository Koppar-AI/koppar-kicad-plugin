"""New KiCad projects from Koppar: a design the assistant started, unpacked and opened.

The project zip holds the schematic, the board, the project file, its rules,
the libraries the design uses (with their tables) and the BOM. It is unpacked
into a folder of its own and opened in a KiCad of its own: the PCB editor the
plugin runs in has one project, the user's.
"""

import io
import os
import re
import shutil
import subprocess
import sys
import zipfile


def folder_name(title):
    """A folder name from the design's title: letters, digits, dashes."""
    name = re.sub(r"[^\w-]+", "-", (title or "").strip()).strip("-_")
    return (name or "koppar-design")[:60]


def free_folder(parent, name):
    """`parent/name`, or `parent/name-2`, -3... when that exists already."""
    path, n = os.path.join(parent, name), 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(parent, f"{name}-{n}")
    return path


def unpack(zip_bytes, folder):
    """The project into `folder` (made here); the path of its .kicad_pro."""
    os.makedirs(folder, exist_ok=True)
    root = os.path.realpath(folder)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for entry in z.infolist():
            dest = os.path.realpath(os.path.join(root, entry.filename))
            # nothing outside the folder, whatever a name says
            if dest != root and not dest.startswith(root + os.sep):
                continue
            if entry.is_dir():
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(entry) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    pros = sorted(f for f in os.listdir(root) if f.endswith(".kicad_pro"))
    return os.path.join(root, pros[0]) if pros else None


def kicad_program():
    """KiCad's project manager, from the KiCad this plugin runs in, or None."""
    exe = sys.executable or ""
    if sys.platform == "win32":
        candidates = [os.path.join(os.path.dirname(exe), "kicad.exe")]
    elif sys.platform == "darwin":
        app = exe[: exe.find("KiCad.app") + len("KiCad.app")] if "KiCad.app" in exe else "/Applications/KiCad/KiCad.app"
        candidates = [os.path.join(app, "Contents", "MacOS", "kicad")]
    else:
        candidates = [shutil.which("kicad") or ""]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def open_in_kicad(pro, launch_default=None):
    """Open the project `pro` in KiCad; False when no KiCad could be started."""
    program = kicad_program()
    if program:
        try:
            subprocess.Popen([program, pro], cwd=os.path.dirname(pro), close_fds=True)  # noqa: S603 - KiCad itself
            return True
        except OSError:
            pass
    # the system's handler for .kicad_pro files, which the KiCad installers register
    return bool(launch_default and launch_default(pro))
