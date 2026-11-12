"""Feedback to Koppar, sent only when the user sends it.

The plugin collects nothing on its own: no telemetry, no crash reports. The Feedback box (the
window's header, and the link under a run that failed) sends a message, a rating, or both, and
only when "Include the details of the last run" is ticked, the details below, shown in the box
exactly as they go out. The board itself is never part of it.

No wx and no pcbnew: the window passes in what it knows.
"""

import os
import platform

from .config import VERSION

MESSAGE_MAX_CHARS = 5000
TRACEBACK_MAX_CHARS = 8000


def _scrub(text_):
    """The user's home folder as ~, so a traceback does not carry their name."""
    if not text_:
        return text_
    home = os.path.expanduser("~")
    return text_.replace(home, "~") if home and home != "~" else text_


def report(kicad_version=None, task=None, error=None, job_id=None, design_id=None):
    """What "the details of the last run" are: versions, the task, its error (with the Python
    traceback when the plugin itself failed), and the server's id for the run."""
    out = {
        "plugin": VERSION,
        "kicad": kicad_version,
        "os": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": platform.python_version(),
    }
    if task:
        out["task"] = task
    if job_id:
        out["job_id"] = job_id
    if design_id:
        out["design_id"] = design_id
    if error is not None:
        out["error"] = _scrub(str(getattr(error, "detail", error)))[:2000]
        status = getattr(error, "status", None)
        if status:
            out["status"] = status
        tb = (getattr(error, "body", None) or {}).get("traceback")
        if tb:
            out["traceback"] = _scrub(tb)[-TRACEBACK_MAX_CHARS:]
    return out


def payload(message, rating=None, details=None):
    """The body of POST /api/tools/feedback."""
    body = {"message": (message or "").strip()[:MESSAGE_MAX_CHARS]}
    if rating:
        body["rating"] = int(rating)
    if details:
        body["details"] = details
        if details.get("job_id"):
            body["job_id"] = details["job_id"]
        if details.get("design_id"):
            body["design_id"] = details["design_id"]
    return body
