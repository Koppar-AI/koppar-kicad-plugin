"""Updates: is there a newer Koppar for KiCad, and putting it in place.

Koppar publishes its latest release as a small manifest ({version, url, sha256, size, notes,
min_kicad}); the window checks it each time it opens, in the background, while "Check for
updates automatically" is on (the default). Update downloads the release, checks its size and SHA-256 against the manifest, and
writes the package's files over the installed ones, each one atomically, after a copy of the
installed folder is kept beside it (`.previous`) so a failed or unwanted update can be undone.
KiCad's record of installed packages is told the new version, so its Plugin and Content Manager
agrees. The new code runs from the next start of the PCB editor. A source checkout (not a
package) is never updated: git does that.

No wx and no pcbnew: the window passes in what it knows (KiCad's settings folder, its version).
"""

import hashlib
import io
import json
import os
import re
import shutil
import ssl
import urllib.request
import zipfile

from . import config

DOWNLOAD_TIMEOUT_S = 120
MAX_PACKAGE_BYTES = 20 * 1024 * 1024
PREVIOUS_SUFFIX = ".previous"
HERE = os.path.dirname(os.path.abspath(__file__))


class UpdateError(Exception):
    pass


def parse_version(v):
    """'0.1.12' -> (0, 1, 12); anything unreadable sorts lowest."""
    try:
        return tuple(int(p) for p in re.findall(r"\d+", str(v))[:4])
    except ValueError:
        return ()


def is_newer(candidate, current=config.VERSION):
    return parse_version(candidate) > parse_version(current)


def due(settings):
    """Whether the window should look for a newer release when it opens."""
    return bool(settings.get("check_updates", True))


def offer(manifest, kicad_major=None, current=config.VERSION):
    """The manifest when it is a release this plugin should move to, else None: newer than
    this one, for this KiCad, with what an update needs."""
    if not isinstance(manifest, dict):
        return None
    if not all(manifest.get(k) for k in ("version", "url", "sha256")):
        return None
    if not is_newer(manifest["version"], current):
        return None
    lowest = parse_version(manifest.get("min_kicad") or "")
    if kicad_major is not None and lowest and kicad_major < lowest[0]:
        return None
    if not str(manifest["url"]).startswith(("https://", "file://")):
        return None
    return manifest


def install_dir():
    """The folder this plugin runs from (the package's plugins/ folder once installed)."""
    return HERE


def can_update():
    """A package can update itself; a source checkout is git's to update."""
    return bool(config.PACKAGED)


def download(manifest, ssl_context=None, user_agent=None):
    """The release zip, checked against the manifest's size and SHA-256."""
    req = urllib.request.Request(manifest["url"], headers={"User-Agent": user_agent or f"KopparKiCadPlugin/{config.VERSION}"})
    ctx = ssl_context if manifest["url"].startswith("https:") else None
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S, context=ctx) as resp:
            data = resp.read(MAX_PACKAGE_BYTES + 1)
    except Exception as e:
        raise UpdateError(f"The update could not be downloaded: {getattr(e, 'reason', e)}")
    if len(data) > MAX_PACKAGE_BYTES:
        raise UpdateError("The update is larger than a Koppar package can be; nothing was changed.")
    if manifest.get("size") and int(manifest["size"]) != len(data):
        raise UpdateError("The update did not arrive whole (its size is wrong); nothing was changed.")
    if hashlib.sha256(data).hexdigest().lower() != str(manifest["sha256"]).lower():
        raise UpdateError("The update does not match its published checksum; nothing was changed.")
    return data


def _package_files(zip_bytes):
    """{relative path: bytes} of the package's plugins/ folder, and its metadata.json."""
    files, meta = {}, None
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if info.is_dir():
                continue
            if name == "metadata.json":
                meta = json.loads(z.read(info).decode("utf-8"))
                continue
            if not name.startswith("plugins/"):
                continue
            rel = name[len("plugins/"):]
            parts = rel.split("/")
            if not rel or rel.startswith("/") or ".." in parts or ":" in parts[0]:
                raise UpdateError(f"The update holds a file it may not write ({name}); nothing was changed.")
            files[rel] = z.read(info)
    if "__init__.py" not in files or "config.py" not in files:
        raise UpdateError("The update is not a Koppar package; nothing was changed.")
    return files, meta


def _version_in(files):
    m = re.search(r'^VERSION = "([^"]+)"', files["config.py"].decode("utf-8"), re.M)
    return m.group(1) if m else None


def _write_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.koppar-new"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def install(zip_bytes, manifest, target=None, kicad_settings_dir=None):
    """Put the release in place of the installed plugin. Returns the version installed.

    The installed folder is copied to `<folder>.previous` first; files are then written one by
    one (only those that changed) and files the new release no longer has are removed. If any
    write fails the copy is put back and UpdateError says why."""
    target = target or install_dir()
    files, meta = _package_files(zip_bytes)
    version = _version_in(files)
    if version != manifest.get("version"):
        raise UpdateError(f"The update says it is {version}, not {manifest.get('version')}; nothing was changed.")
    previous = target + PREVIOUS_SUFFIX
    shutil.rmtree(previous, ignore_errors=True)
    shutil.copytree(target, previous, ignore=shutil.ignore_patterns("__pycache__"))
    try:
        for rel, data in files.items():
            path = os.path.join(target, *rel.split("/"))
            try:
                with open(path, "rb") as f:
                    if f.read() == data:
                        continue
            except OSError:
                pass
            _write_atomic(path, data)
        keep = {os.path.join(target, *rel.split("/")) for rel in files}
        for folder, dirs, names in os.walk(target):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in names:
                path = os.path.join(folder, name)
                if path not in keep and name.endswith((".py", ".png", ".svg", ".ttf", ".txt")):
                    os.remove(path)
    except OSError as e:
        restore(target)
        raise UpdateError(f"The update could not be written ({e}); the installed version was put back.")
    if kicad_settings_dir:
        record_version(kicad_settings_dir, version, meta)
    return version


def restore(target=None):
    """Put back the version the last update replaced. Returns True when there was one."""
    target = target or install_dir()
    previous = target + PREVIOUS_SUFFIX
    if not os.path.isdir(previous):
        return False
    for folder, _dirs, names in os.walk(previous):
        for name in names:
            src = os.path.join(folder, name)
            rel = os.path.relpath(src, previous)
            with open(src, "rb") as f:
                _write_atomic(os.path.join(target, rel), f.read())
    return True


def record_version(kicad_settings_dir, version, meta=None):
    """Tell KiCad's installed-packages record the version now installed, so its Plugin and Content
    Manager shows it. Only this package's entry changes; a missing record (a hand install) is left alone."""
    path = os.path.join(kicad_settings_dir, "installed_packages.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    changed = False
    for entry in data.get("packages") or []:
        pkg = entry.get("package") or {}
        if pkg.get("identifier") != config.PACKAGE_ID:
            continue
        entry["current_version"] = version
        if meta and meta.get("versions"):
            pkg["versions"] = meta["versions"]
        changed = True
    if changed:
        _write_atomic(path, (json.dumps(data, indent=4) + "\n").encode("utf-8"))
    return changed


def default_context():
    return ssl.create_default_context()
