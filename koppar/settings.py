"""Where the plugin keeps state.

User settings (API key, last options) are one JSON file per user, in
the OS's config folder, readable only by the user. A project's session (the
job in flight for that board, and its downloaded result) is in
`<project>/.koppar/`, so it survives KiCad restarts and follows the project.
"""

import json
import os
import sys
import time

from .config import PROJECT_DIR_NAME


def _config_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Koppar")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Koppar")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "koppar")


def _settings_path():
    return os.path.join(_config_dir(), "kicad-plugin.json")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, data, private=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    # the settings file holds the API key: create it 0600 rather than chmod after
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


DEFAULTS = {
    "api_key": "",
    "preserve_existing": True,
    "vias_in_pads": False,
    "import_zones": True,
}


def load_settings():
    data = dict(DEFAULTS)
    data.update({k: v for k, v in _read_json(_settings_path()).items() if k in DEFAULTS})
    return data


def cli_login():
    """The key `koppar login` saved for the Koppar CLI ({url, key}), or None."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    data = _read_json(os.path.join(base, "koppar", "config.json"))
    return data if str(data.get("key", "")).startswith("sc_") else None


def api_key_of(settings):
    """The plugin's own key, else the CLI's."""
    if settings.get("api_key"):
        return settings["api_key"]
    login = cli_login()
    return login["key"] if login else ""


def save_settings(settings):
    _write_json(_settings_path(), {k: settings.get(k, DEFAULTS[k]) for k in DEFAULTS}, private=True)


# -- per-project session ------------------------------------------------------


def project_work_dir(board_path):
    return os.path.join(os.path.dirname(os.path.abspath(board_path)), PROJECT_DIR_NAME)


def _session_path(board_path):
    stem = os.path.splitext(os.path.basename(board_path))[0]
    return os.path.join(project_work_dir(board_path), f"{stem}.session.json")


def load_session(board_path):
    """The job recorded for this board, or None. Jobs expire on the server
    after 24 h, so an older record is dropped."""
    if not board_path:
        return None
    data = _read_json(_session_path(board_path))
    if not data.get("job_id"):
        return None
    if time.time() - float(data.get("submitted_at", 0)) > 24 * 3600:
        clear_session(board_path)
        return None
    return data


def save_session(board_path, session):
    path = _session_path(board_path)
    _write_project_record(path, session)


def _write_project_record(path, data):
    _write_json(path, data)
    # keep the folder out of the user's repository without touching theirs
    ignore = os.path.join(os.path.dirname(path), ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as f:
            f.write("*\n")


def _record_path(board_path, kind):
    stem = os.path.splitext(os.path.basename(board_path))[0]
    return os.path.join(project_work_dir(board_path), f"{stem}.{kind}.json")


def load_workspace(board_path):
    """The account workspace this board is synced to: {design_id, board_file, server, web_url}."""
    data = _read_json(_record_path(board_path, "workspace"))
    return data if data.get("design_id") else None


def save_workspace(board_path, record):
    _write_project_record(_record_path(board_path, "workspace"), record)


def clear_workspace(board_path):
    try:
        os.remove(_record_path(board_path, "workspace"))
    except OSError:
        pass


def load_designs(board_path):
    """New designs the chat started from this board: {design_id: {title, web_url, project}}."""
    return _read_json(_record_path(board_path, "designs"))


def save_designs(board_path, designs):
    _write_project_record(_record_path(board_path, "designs"), designs)


def load_pending(board_path):
    """The last result waiting to be applied (either window's): {mode, result_path, nets, ...}."""
    data = _read_json(_record_path(board_path, "pending"))
    return data if data.get("result_path") else None


def save_pending(board_path, record):
    _write_project_record(_record_path(board_path, "pending"), record)


def clear_session(board_path):
    try:
        os.remove(_session_path(board_path))
    except OSError:
        pass
