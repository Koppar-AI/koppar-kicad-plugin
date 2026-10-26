"""The KiCad action plugins.

Koppar (toolbar button): the Koppar window, with Autoroute (small boards need
no account) and, with an API key, the account's board tools. Koppar: apply
routed result (menu only): bring the last result into the board. The apply is
its own action because KiCad records what an action plugin's Run() changes as
one undo step, and a change made later from a window's button would get no
undo at all; the window's Apply button runs this action through its menu
entry.
"""

import os
import time

import pcbnew
import wx

from . import board_io
from .settings import load_pending, load_session, load_settings, save_pending, save_session
from .ui import APPLY_ACTION_NAME, KopparWindow, pcb_frame

HERE = os.path.dirname(os.path.abspath(__file__))

# the open window per board file, so the button brings it back instead of a second one
_windows = {}


def _live_window(board_file):
    w = _windows.get(board_file)
    try:
        if w is not None and not w.closed:
            return w
    except RuntimeError:  # the C++ window is gone
        pass
    _windows.pop(board_file, None)
    return None


def _message(text, icon=wx.ICON_INFORMATION):
    wx.MessageBox(text, "Koppar", wx.OK | icon, pcb_frame())


class KopparAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Koppar"
        self.category = "Routing"
        self.description = "Autoroute, place, finish, polish and check the board with Koppar"
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(HERE, "icon.png")
        self.dark_icon_file_name = os.path.join(HERE, "icon_dark.png")

    def Run(self):
        board = pcbnew.GetBoard()
        board_file = board_io.board_path(board)
        if not board_file:
            _message("Save the board first: Koppar keeps its jobs and results beside the board file.")
            return
        window = _live_window(board_file)
        if window is not None:
            window.Raise()
            return
        with wx.BusyCursor():
            census = board_io.net_census(board)
        _windows[board_file] = KopparWindow(pcb_frame(), board_file, load_settings(), census)


class ApplyAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = APPLY_ACTION_NAME
        self.category = "Routing"
        self.description = "Bring the last Koppar result into the board (one undo step)"
        self.show_toolbar_button = False
        self.icon_file_name = os.path.join(HERE, "icon.png")

    def Run(self):
        board = pcbnew.GetBoard()
        board_file = board_io.board_path(board)
        pending = load_pending(board_file) if board_file else None
        path = (pending or {}).get("result_path")
        if not path or not os.path.exists(path):
            _message("No Koppar result is waiting for this board. Run a task in the Koppar window first.")
            return
        mode = pending.get("mode", "copper")
        try:
            with wx.BusyInfo("Bringing the result into the board..."):
                out = board_io.apply_result(board, path, mode, pending.get("nets"),
                                            import_zones=pending.get("import_zones", True))
        except Exception as e:
            _message(f"Could not apply the result: {e}", wx.ICON_ERROR)
            return
        pending["applied_at"] = time.time()
        save_pending(board_file, pending)
        if pending.get("source") == "route":
            session = load_session(board_file)
            if session:
                session["applied_at"] = pending["applied_at"]
                save_session(board_file, session)
        pcbnew.Refresh()

        text = _applied_text(board, out)
        window = _live_window(board_file)
        if window is not None:
            window.mark_applied(text)
        else:
            _message(text)


def _applied_text(board, out):
    stats = out["copper"]
    lines = []
    if "placement" in out:
        placed = out["placement"]
        lines.append(f"Moved {placed['moved']} footprints."
                     + (f" Locked, left where they were: {', '.join(placed['locked'][:12])}." if placed["locked"] else ""))
    lines.append(f"Added {stats['tracks']} tracks and {stats['vias']} vias"
                 + (f" and {stats['zones']} pours" if stats["zones"] else "")
                 + f", replacing {stats['removed']} items. Ctrl+Z undoes it.")
    census = board_io.net_census(board)
    lines.append(f"{sum(1 for n in census if n['routed'])} of {len(census)} nets are now connected.")
    if stats["moved_footprints"]:
        moved = ", ".join(stats["moved_footprints"][:12])
        lines.append(f"These parts moved since the board was sent, so their copper may not line up: {moved}"
                     + (" ..." if len(stats["moved_footprints"]) > 12 else ""))
    if stats["missing_nets"]:
        lines.append(f"{len(stats['missing_nets'])} nets are no longer on the board and were skipped.")
    if stats["skipped_layers"]:
        lines.append(f"{stats['skipped_layers']} items on layers this board does not have were skipped.")
    return "\n".join(lines)
