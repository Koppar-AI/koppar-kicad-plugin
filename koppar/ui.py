"""The Koppar window.

A header (the wordmark, the board, the runs left, the account), the sidebar
(the assistant when the account has runs left, then the tasks), and one page
at a time on the paper: a task, its run, its result, or the conversation.
Everything is drawn with the brand's faces and icons (style.py, widgets.py).
Network calls run on worker threads and report back with wx.CallAfter, so the
PCB editor never freezes; pcbnew is only touched on the UI thread.
"""

import json
import os
import threading
import time
import webbrowser
from collections import namedtuple

import pcbnew
import wx
from wx.lib.scrolledpanel import ScrolledPanel

from . import board_io, catalog, projects, rich, summary
from .api import ApiError, KopparClient
from .config import ACCOUNT_SERVER, API_KEYS_URL, MAX_BOARD_BYTES, POLL_INTERVAL_S, ROUTED_ARTIFACT, VERSION, servers
from .settings import (
    api_key_of,
    clear_session,
    cli_login,
    load_designs,
    load_session,
    project_work_dir,
    save_designs,
    save_pending,
    save_session,
    save_settings,
)
from .style import Theme, paint_paper
from .widgets import (
    KEYBOARD,
    AccountButton,
    Button,
    Card,
    Chip,
    Composer,
    MinutesStepper,
    Painted,
    RichView,
    Segmented,
    StatTile,
    Switch,
    TaskList,
    TraceProgress,
    _extent,
    _gc,
)
from .workspace import Workspace

APPLY_ACTION_NAME = "Koppar: apply result"
HERE = os.path.dirname(os.path.abspath(__file__))
COLUMN = 640  # the content column, DIP
NavItem = namedtuple("NavItem", "id title icon subtitle")
CHAT = NavItem("chat", "Ask Koppar", "message-square", "Ask about this board, change it, or design a new one")


def pcb_frame():
    """The PCB editor's top window, the parent every plugin window sits on."""
    return wx.FindWindowByName("PcbFrame")


def _fmt_s(seconds):
    seconds = int(max(0, seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _find_menu_item(menu, label):
    for item in menu.GetMenuItems():
        sub = item.GetSubMenu()
        if sub is not None:
            found = _find_menu_item(sub, label)
            if found is not None:
                return found
        elif item.GetItemLabelText().strip() == label:
            return item
    return None


def trigger_action(label):
    """Run an action plugin as if its menu entry were clicked, so KiCad wraps
    it in the commit an action plugin run gets. False when the entry is not
    in the PCB editor's menus."""
    frame = pcb_frame()
    bar = frame.GetMenuBar() if frame else None
    if bar is None:
        return False
    for i in range(bar.GetMenuCount()):
        item = _find_menu_item(bar.GetMenu(i), label)
        if item is not None:
            evt = wx.CommandEvent(wx.wxEVT_COMMAND_MENU_SELECTED, item.GetId())
            evt.SetEventObject(frame)
            wx.PostEvent(frame, evt)
            return True
    return False


def float_over(dlg):
    """A dialog of the Koppar window, kept above it. The window floats on the PCB editor, which
    on macOS puts it on the floating window level: a plain dialog sits below that level, and
    went behind the window when KiCad came back to the front. The style is set once the dialog
    is built (its buttons are made from the same style bits, where this one reads as No)."""
    if wx.Platform == "__WXMAC__":
        dlg.SetWindowStyleFlag(dlg.GetWindowStyleFlag() | wx.FRAME_FLOAT_ON_PARENT)
    return dlg


def open_path(path):
    if wx.Platform == "__WXMSW__":
        os.startfile(path)  # noqa: S606 - the user's own file
    else:
        webbrowser.open("file://" + path)


# -- page furniture ------------------------------------------------------------


class Paper(ScrolledPanel):
    """A page: the dotted paper, scrolling, with a left-aligned column of cards."""

    paper = True

    def __init__(self, parent, theme):
        super().__init__(parent, style=wx.BORDER_NONE)
        self.theme = theme
        self.SetBackgroundColour(theme["paper"])
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SCROLLWIN, lambda e: (self.Refresh(), e.Skip()))
        self.column = wx.BoxSizer(wx.VERTICAL)
        outer = wx.BoxSizer(wx.HORIZONTAL)
        outer.Add(self.column, 0, wx.LEFT | wx.TOP | wx.BOTTOM, self.FromDIP(28))
        outer.AddStretchSpacer()
        self.SetSizer(outer)
        self.column.SetMinSize((self.FromDIP(COLUMN), -1))

    def _paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        gc = _gc(dc)
        w, h = self.GetClientSize()
        x, y = self.CalcUnscrolledPosition(0, 0)
        paint_paper(gc, w, h, self.theme, self, (x, y))

    def reset(self):
        for child in list(self.GetChildren()):
            child.Destroy()
        self.column.Clear()

    def add(self, item, gap=16):
        self.column.Add(item, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(gap))

    def done(self):
        self.Layout()
        self.SetupScrolling(scroll_x=False, scrollToTop=True)
        self.Refresh()


def text(parent, theme, label, size=10, weight=400, colour="ink", wrap=None, family="text"):
    st = wx.StaticText(parent, label=label)
    st.SetFont(theme.display(size, weight) if family == "display" else theme.mono(size, weight) if family == "mono"
               else theme.text(size, weight))
    st.SetForegroundColour(theme[colour])
    if wrap:
        st.Wrap(parent.FromDIP(wrap))
    return st


class IconTile(Painted):
    """A task's icon on a soft square: the page's quiet anchor."""

    def __init__(self, parent, theme, icon, tone=None, size=40):
        super().__init__(parent, theme)
        self.icon, self.tone = icon, tone
        self.SetMinSize((self.FromDIP(size), self.FromDIP(size)))

    def draw(self, gc, w, h):
        t = self.theme
        bg = {"good": t["good_bg"], "warn": t["warn_bg"], "bad": t["bad_bg"]}.get(self.tone, t["sunken"])
        fg = t.tone(self.tone) if self.tone in ("good", "warn", "bad") else t["ink"]
        gc.SetBrush(wx.Brush(bg))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(0, 0, w, h, self.FromDIP(10))
        s = int(w * 0.5)
        bmp = t.icon(self.icon, s, fg, self)
        if bmp.IsOk():
            gc.DrawBitmap(bmp, (w - s) / 2, (h - s) / 2, s, s)


class Banner(Painted):
    """A line that says something happened: applied, changed, refused."""

    def __init__(self, parent, theme, message, tone="good", icon="circle-check", action=None):
        super().__init__(parent, theme)
        self.message, self.tone, self.icon = message, tone, icon
        self.lines = []
        self.SetMinSize((-1, self.FromDIP(44)))
        self.Bind(wx.EVT_SIZE, self._size)

    def _size(self, event):
        self._wrap()
        event.Skip()

    def _wrap(self):
        w = self.GetClientSize().width - self.FromDIP(56)
        if w <= 0:
            return
        dc = wx.ClientDC(self)
        gc = _gc(dc)
        gc.SetFont(self.theme.text(10, 500), self.theme["ink"])
        words, lines, line = self.message.split(), [], ""
        for word in words:
            trial = f"{line} {word}".strip()
            if _extent(gc, trial)[0] > w and line:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
        self.lines = lines
        h = self.FromDIP(24) + len(lines) * self.FromDIP(18)
        if self.GetMinSize().height != h:
            self.SetMinSize((-1, h))
            self.GetParent().Layout()
        self.Refresh()

    def draw(self, gc, w, h):
        t = self.theme
        bg = {"good": t["good_bg"], "warn": t["warn_bg"], "bad": t["bad_bg"]}.get(self.tone, t["sunken"])
        gc.SetBrush(wx.Brush(bg))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(0, 0, w, h, self.FromDIP(10))
        s = self.FromDIP(18)
        bmp = t.icon(self.icon, s, t.tone(self.tone), self)
        if bmp.IsOk():
            gc.DrawBitmap(bmp, self.FromDIP(14), self.FromDIP(13), s, s)
        gc.SetFont(t.text(10, 500), t["ink"])
        for i, line in enumerate(self.lines or [self.message]):
            gc.DrawText(line, self.FromDIP(42), self.FromDIP(13) + i * self.FromDIP(18))


class StepRow(Painted):
    """One thing the assistant did on the board: what, and how it went."""

    def __init__(self, parent, theme, label):
        super().__init__(parent, theme)
        self.label, self.status, self.detail = label, "running", ""
        self.angle = 0.0
        self.SetMinSize((-1, self.FromDIP(28)))
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._spin, self.timer)
        self.timer.Start(60)

    def _spin(self, event):
        self.angle = (self.angle + 0.35) % 6.283
        self.Refresh()

    def finish(self, status, detail=""):
        self.status, self.detail = status, detail
        self.timer.Stop()
        self.Refresh()

    def draw(self, gc, w, h):
        t = self.theme
        s = self.FromDIP(15)
        icon, colour = {"running": ("loader-circle", t["muted"]), "done": ("check", t["good"]),
                        "failed": ("x", t["bad"])}[self.status]
        bmp = t.icon(icon, s, colour, self)
        if bmp.IsOk():
            if self.status == "running":
                gc.PushState()
                gc.Translate(s / 2, h / 2)
                gc.Rotate(self.angle)
                gc.DrawBitmap(bmp, -s / 2, -s / 2, s, s)
                gc.PopState()
            else:
                gc.DrawBitmap(bmp, 0, (h - s) / 2, s, s)
        gc.SetFont(t.text(9.5, 500), t["ink2"] if self.status != "failed" else t["bad"])
        tw, th = _extent(gc, self.label)
        gc.DrawText(self.label, s + self.FromDIP(10), (h - th) / 2)
        if self.detail:
            gc.SetFont(t.text(9), t["muted"])
            room = w - s - tw - self.FromDIP(24)
            d = self.detail
            while d and _extent(gc, d)[0] > room:
                d = d[:-2]
            gc.DrawText(d, s + tw + self.FromDIP(20), (h - th) / 2)


class Bubble(wx.Panel):
    """The person's message: a white rounded block on the paper, right of the column."""

    def __init__(self, parent, theme, message):
        super().__init__(parent)
        self.theme = theme
        self.SetBackgroundColour(theme["surface"])
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        box = wx.BoxSizer(wx.VERTICAL)
        self.view = RichView(self, theme, size=10.5)
        box.Add(self.view, 0, wx.EXPAND | wx.ALL, self.FromDIP(12))
        self.SetSizer(box)
        self.view.set_blocks(rich.markdown(message))

    def _paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        gc = _gc(dc)
        w, h = self.GetClientSize()
        paint_paper(gc, w, h, self.theme, self, self.GetPosition())
        # the person's words: a white sheet like the replies, smaller, with a firmer edge
        gc.SetBrush(wx.Brush(self.theme["surface"]))
        gc.SetPen(wx.Pen(self.theme["line2"], 1))
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - 1, self.FromDIP(14))


class DesignCard(wx.Panel):
    """A new design the assistant started: where it stands, and the way to open it."""

    def __init__(self, parent, theme, title, on_open, on_follow, on_folder):
        super().__init__(parent)
        self.theme = theme
        self.SetBackgroundColour(theme["sunken"])
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.tile = IconTile(self, theme, "cpu", size=36)
        self.tile.SetBackgroundColour(theme["sunken"])
        row.Add(self.tile, 0, wx.RIGHT, self.FromDIP(14))
        col = wx.BoxSizer(wx.VERTICAL)
        col.Add(text(self, theme, "New design", 8.5, 600, colour="muted"), 0)
        col.Add(text(self, theme, title or "Untitled design", 11.5, 600, family="display"), 0, wx.TOP, self.FromDIP(1))
        self.status = text(self, theme, "Starting", 9.5, colour="ink2")
        col.Add(self.status, 0, wx.TOP, self.FromDIP(4))
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.open_btn = Button(self, theme, "Open in KiCad", on_open, kind="primary", icon="folder-open", small=True)
        self.open_btn.Hide()
        btns.Add(self.open_btn, 0, wx.RIGHT, self.FromDIP(8))
        self.folder_btn = Button(self, theme, "Show folder", on_folder, kind="ghost", small=True)
        self.folder_btn.Hide()
        btns.Add(self.folder_btn, 0, wx.RIGHT, self.FromDIP(4))
        btns.Add(Button(self, theme, "Follow in Koppar", on_follow, kind="ghost", icon="external-link", small=True), 0)
        col.Add(btns, 0, wx.TOP, self.FromDIP(12))
        row.Add(col, 1)
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(row, 0, wx.EXPAND | wx.ALL, self.FromDIP(16))
        self.SetSizer(box)

    def show(self, status, tone=None, can_open=False, open_label="Open in KiCad", folder=False):
        self.status.SetLabel(status)
        self.status.SetForegroundColour(self.theme.tone(tone) if tone in ("good", "bad", "warn") else self.theme["ink2"])
        self.status.Wrap(self.FromDIP(COLUMN - 150))
        self.tile.tone = tone if tone in ("good", "bad") else None
        self.tile.Refresh()
        self.open_btn.set_label(open_label)
        self.open_btn.Show(can_open)
        self.folder_btn.Show(folder)
        self.Layout()
        self.GetParent().Layout()

    def _paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        gc = _gc(dc)
        w, h = self.GetClientSize()
        gc.SetBrush(wx.Brush(self.theme["surface"]))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRectangle(0, 0, w, h)
        gc.SetBrush(wx.Brush(self.theme["sunken"]))
        gc.SetPen(wx.Pen(self.theme["line"], 1))
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - 1, self.FromDIP(12))


# -- options -----------------------------------------------------------------


class NetScope(wx.Panel):
    """Which nets: all, the ones not connected, the ones selected in KiCad, power nets, or picked."""

    LABELS = {"all": "All nets", "open": "Not connected", "selected": "Selected in KiCad",
              "power": "Power nets", "pick": "Pick nets"}

    def __init__(self, parent, theme, census, default, with_power=False):
        super().__init__(parent)
        self.theme = theme
        self.census = census
        self.picked = []
        self.SetBackgroundColour(parent.GetBackgroundColour())
        keys = ["all", "open", "selected"] + (["power"] if with_power else []) + ["pick"]
        self.scope = default if default in keys else "all"
        box = wx.BoxSizer(wx.VERTICAL)
        self.choice = Segmented(self, theme, [(k, self.LABELS[k]) for k in keys], self.scope, self._chose)
        box.Add(self.choice, 0)
        self.count = text(self, theme, "", 9.5, colour="muted")
        box.Add(self.count, 0, wx.TOP, self.FromDIP(8))
        self.SetSizer(box)
        self._update()

    def set_scope(self, key):
        self.choice.set(key)
        self.scope = key
        self._update()

    def _selected_in_kicad(self):
        nets = set()
        try:
            for item in pcbnew.GetCurrentSelection():
                if item.GetClass() == "FOOTPRINT":
                    nets.update(p.GetNetname() for p in item.Cast().Pads() if p.GetNetCode() > 0)
                elif hasattr(item, "GetNetCode") and item.GetNetCode() > 0:
                    nets.add(item.GetNetname())
        except Exception:
            pass
        return sorted(nets)

    def _chose(self, key):
        if key == "pick":
            names = [n["name"] for n in self.census]
            dlg = float_over(wx.MultiChoiceDialog(self, "The nets to work on", "Pick nets", names))
            current = set(self.picked) or set(self.nets_for(self.scope) or [])
            dlg.SetSelections([i for i, n in enumerate(names) if n in current])
            if dlg.ShowModal() == wx.ID_OK:
                self.picked = [names[i] for i in dlg.GetSelections()]
            dlg.Destroy()
        self.scope = key
        self._update()

    def nets_for(self, scope):
        """The net names of a scope; None for all nets."""
        if scope == "all":
            return None
        if scope == "open":
            return [n["name"] for n in self.census if not n["routed"]]
        if scope == "selected":
            return self._selected_in_kicad()
        if scope == "power":
            return [n["name"] for n in self.census if catalog.looks_like_power(n["name"])]
        return list(self.picked)

    def nets(self):
        return self.nets_for(self.scope)

    def _update(self):
        nets = self.nets()
        if nets is None:
            label = f"Every net on the board, {len(self.census)} in all."
        elif not nets:
            label = {"selected": "Nothing is selected in the PCB editor. Select parts, pads or tracks there first.",
                     "pick": "No net picked yet."}.get(self.scope, "No such net on this board.")
        else:
            shown = ", ".join(nets[:5]) + (f" and {len(nets) - 5} more" if len(nets) > 5 else "")
            label = f"{len(nets)} {'net' if len(nets) == 1 else 'nets'}: {shown}"
        self.count.SetLabel(label)
        self.count.Wrap(self.FromDIP(COLUMN - 60))
        self.Layout()
        self.GetParent().Layout()

    def refresh(self):
        self._update()


class SchemaFields(wx.Panel):
    """Every option the server publishes for a tool, from its JSON Schema. Blank
    means the server's default."""

    def __init__(self, parent, theme, schema, skip):
        super().__init__(parent)
        self.SetBackgroundColour(parent.GetBackgroundColour())
        self.fields = []
        required = set(schema.get("required") or [])
        grid = wx.FlexGridSizer(0, 2, self.FromDIP(10), self.FromDIP(16))
        grid.AddGrowableCol(1)
        for name, prop in (schema.get("properties") or {}).items():
            if name in skip or name in catalog.HIDDEN_ARGS:
                continue
            widget, kind = self._widget(theme, prop)
            cell = wx.BoxSizer(wx.VERTICAL)
            cell.Add(widget, 0, wx.EXPAND)
            if prop.get("description"):
                cell.Add(text(self, theme, prop["description"], 9, colour="muted", wrap=400), 0, wx.TOP, self.FromDIP(3))
            grid.Add(text(self, theme, summary.humanize(name) + (" (required)" if name in required else ""), 9.5, 600, "ink2"),
                     0, wx.TOP, self.FromDIP(5))
            grid.Add(cell, 1, wx.EXPAND)
            self.fields.append((name, prop, kind, widget))
        if not self.fields:
            grid.Add(text(self, theme, "No other options.", 9.5, colour="muted"), 0)
        self.SetSizer(grid)

    def _widget(self, theme, prop):
        kind = prop.get("type")
        if kind == "boolean":
            w = wx.Choice(self, choices=["Server default", "Yes", "No"])
            w.SetSelection(0)
            return w, "boolean"
        if prop.get("enum"):
            w = wx.Choice(self, choices=["Server default"] + [str(v) for v in prop["enum"]])
            w.SetSelection(0)
            return w, "enum"
        if kind == "array" and (prop.get("items") or {}).get("type") == "string":
            w = wx.TextCtrl(self)
            w.SetHint("Comma-separated")
            return w, "strings"
        if kind in ("number", "integer", "string"):
            return wx.TextCtrl(self), kind
        w = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, self.FromDIP(54)))
        w.SetHint("JSON")
        w.SetFont(theme.mono(9))
        return w, "json"

    def values(self):
        out = {}
        for name, prop, kind, w in self.fields:
            if kind in ("boolean", "enum"):
                i = w.GetSelection()
                if i > 0:
                    out[name] = (i == 1) if kind == "boolean" else prop["enum"][i - 1]
                continue
            raw = w.GetValue().strip()
            if not raw:
                continue
            try:
                out[name] = {"integer": int, "number": float, "json": json.loads,
                             "strings": lambda s: [x.strip() for x in s.split(",") if x.strip()]}.get(kind, str)(raw)
            except ValueError:
                raise ValueError(f"{summary.humanize(name)}: " + {"integer": "a whole number", "number": "a number"}.get(kind, "JSON") + " is expected.")
        return out


# -- the window ----------------------------------------------------------------


class KopparWindow(wx.Frame):
    def __init__(self, parent, board_file, settings, census):
        super().__init__(parent, title=f"Koppar: {os.path.basename(board_file)}",
                         style=wx.DEFAULT_FRAME_STYLE | wx.FRAME_FLOAT_ON_PARENT, size=(1040, 740))
        self.theme = Theme()
        self.board_file = board_file
        self.settings = settings
        self.census = census
        self.closed = False
        self.busy = False
        self.listing = None
        self.account_info = None
        self.account_error = None
        self.task = None
        self.pending = None
        self.applied_text = None
        self.result_for = None
        self.result_summary = None
        self.show_all_details = False
        self.controls = {}
        self.scope = None
        self.advanced = None
        self.chat_busy = False
        self.chat_loaded_for = None
        # new designs the chat started: where each was opened, and the card that shows it
        self.designs = load_designs(board_file)
        self.design_cards = {}
        self.design_polls = set()
        self.chat_empty = True
        self.session = load_session(board_file) or {}
        self._make_clients()

        t = self.theme
        self.SetBackgroundColour(t["surface"])
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(self._header(), 0, wx.EXPAND)
        body = wx.BoxSizer(wx.HORIZONTAL)
        self.nav = TaskList(self, t, self._pick)
        body.Add(self.nav, 0, wx.EXPAND)
        self.book = wx.Simplebook(self)
        self.book.SetBackgroundColour(t["paper"])
        self.task_page = Paper(self.book, t)
        self.run_page = Paper(self.book, t)
        self.result_page = Paper(self.book, t)
        self.chat_page = wx.Panel(self.book)
        for p in (self.task_page, self.run_page, self.result_page, self.chat_page):
            self.book.AddPage(p, "")
        body.Add(self.book, 1, wx.EXPAND)
        root.Add(body, 1, wx.EXPAND)
        self.SetSizer(root)
        self._build_chat_page()

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self._tick(), self.timer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.SetMinSize(self.FromDIP(wx.Size(960, 620)))
        self._show_account()
        self._refresh_nav()
        self.CentreOnParent()
        self.Show()
        self._load_account()
        self._restore()

    # -- header and account ------------------------------------------------

    def _make_clients(self):
        server, account_server = servers()
        self.key = api_key_of(self.settings)
        install = board_io.install_source()
        self.tools_client = KopparClient(server, self.key, board_io.kicad_version(), install)
        self.account = KopparClient(account_server, self.key, board_io.kicad_version(), install) if self.key else None
        self.workspace = Workspace(self.account, self.board_file) if self.account else None

    def _header(self):
        t = self.theme
        self.header = wx.Panel(self)
        self.header.SetBackgroundColour(t["surface"])
        self.header.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.header.Bind(wx.EVT_PAINT, self._paint_header)
        row = wx.BoxSizer(wx.HORIZONTAL)
        mark = t.wordmark(self.FromDIP(26), self)
        if mark.IsOk():
            row.Add(wx.StaticBitmap(self.header, bitmap=mark), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(18))
        else:
            row.Add(text(self.header, t, "Koppar", 14, 600, family="display"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(18))
        row.AddSpacer(self.FromDIP(18))
        row.Add(text(self.header, t, os.path.basename(self.board_file), 9.5, colour="muted", family="mono"), 0, wx.ALIGN_CENTER_VERTICAL)
        row.AddStretchSpacer()
        self.runs_chip = Chip(self.header, t, "", "neutral", "circuit-board")
        row.Add(self.runs_chip, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(10))
        self.web_btn = Button(self.header, t, "Open in Koppar", lambda: webbrowser.open(self.workspace.record["web_url"]),
                              kind="ghost", icon="external-link", small=True)
        row.Add(self.web_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(6))
        self.key_btn = AccountButton(self.header, t, self._on_account)
        row.Add(self.key_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(16))
        self.header.SetSizer(row)
        self.header.SetMinSize((-1, self.FromDIP(60)))
        return self.header

    def _paint_header(self, event):
        dc = wx.AutoBufferedPaintDC(self.header)
        gc = _gc(dc)
        w, h = self.header.GetClientSize()
        gc.SetBrush(wx.Brush(self.theme["surface"]))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRectangle(0, 0, w, h)
        gc.SetPen(wx.Pen(self.theme["line"], 1))
        gc.StrokeLine(0, h - 0.5, w, h - 0.5)

    def _load_account(self):
        if not self.account:
            self._show_account()
            return

        def work():
            listing = self.account.agent_tools()
            try:
                info = self.account.account()
            except ApiError as e:
                # a server from before the account endpoint: the tools work, the chat is not offered
                info = None if e.status == 404 else {"error": e.detail}
            return listing, info

        def done(result, error):
            if error:
                self.account_error = "Koppar did not accept the API key." if error.status == 401 else f"Your account could not be reached: {error.detail}"
                self.listing = self.account_info = None
            else:
                (self.listing, self.account_info), self.account_error = result, None
            self._show_account()
            self._refresh_nav()
            if self.task and not self.busy and self.book.GetSelection() == 0:
                self._pick(self.task)

        self._bg(work, done)

    def _quota(self):
        return (self.account_info or {}).get("quota") or {}

    def _chat_available(self):
        info = self.account_info or {}
        return self.listing is not None and bool((info.get("chat") or {}).get("available"))

    def _show_account(self):
        q = self._quota()
        if self.account_error:
            # the account button says what went wrong
            self.runs_chip.Hide()
        elif q:
            left = "Unlimited runs" if q.get("unlimited") else f"{q.get('remaining', 0)} {'run' if q.get('remaining') == 1 else 'runs'} left"
            self.runs_chip.set(left, "neutral" if (q.get("unlimited") or q.get("remaining")) else "warn", "circuit-board")
            self.runs_chip.Show()
        elif self.account and self.listing is None:
            self.runs_chip.set("Connecting", "neutral", "loader-circle")
            self.runs_chip.Show()
        else:
            self.runs_chip.Hide()
        has_ws = bool(self.workspace and self.workspace.record and self.workspace.record.get("web_url"))
        self.web_btn.Show(has_ws)
        user = (self.account_info or {}).get("user") or {}
        if not self.account:
            self.key_btn.show_none()
        elif self.account_error:
            refused = self.account_error.startswith("Koppar did not accept")
            self.key_btn.show_error("Key not accepted" if refused else "Not connected", self.account_error)
        elif user.get("email") or user.get("name"):
            self.key_btn.show_user(user.get("email"), user.get("name"))
        else:
            self.key_btn.show_pending()
        self.header.Layout()
        self.header.Refresh()

    def _on_account(self):
        dlg = float_over(AccountDialog(self, self.theme, self.settings))
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            self.settings.update(dlg.values())
        finally:
            dlg.Destroy()
        save_settings(self.settings)
        self._make_clients()
        self.listing = self.account_info = self.account_error = None
        self._show_account()
        self._load_account()
        self._refresh_nav()

    # -- navigation --------------------------------------------------------

    def _available(self, task):
        return not task.account or self.listing is not None

    def _server_tool(self, task):
        if not task.tool or self.listing is None:
            return None
        return next((t for t in self.listing if t["name"] == task.tool), None)

    def _refresh_nav(self):
        groups = []
        if self._chat_available():
            groups.append((None, [CHAT]))
        for group, entries in catalog.offered(self.listing):
            groups.append((group, [NavItem(t.id, t.title, catalog.ICONS.get(t.id, "circle-dot"), t.subtitle) for t, _ in entries]))
        locked = {t.id for t in catalog.TASKS if not self._available(t)}
        self.nav.set_items(groups, locked)
        if self.task is None:
            self._pick(NavItem("autoroute", "", "", ""))

    def _pick(self, item):
        self.nav.select(item.id)
        if item.id == "chat":
            self.task = CHAT
            if not self.busy:
                self._open_chat()
            return
        task = catalog.task(item.id)
        self.task = task
        if not self.busy:
            self._build_task_page(task)
            self.book.SetSelection(0)

    # -- task page ---------------------------------------------------------

    def _build_task_page(self, task):
        page, t = self.task_page, self.theme
        page.Freeze()
        page.reset()
        self.controls, self.scope, self.advanced = {}, None, None

        hero = Card(page, t, pad=24)
        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(IconTile(hero, t, catalog.ICONS.get(task.id, "circle-dot")), 0, wx.RIGHT, self.FromDIP(16))
        heads = wx.BoxSizer(wx.VERTICAL)
        heads.Add(text(hero, t, task.group, 9.5, 500, "muted"), 0)
        heads.Add(text(hero, t, task.title, 18, 600, family="display"), 0, wx.TOP, self.FromDIP(1))
        top.Add(heads, 1, wx.ALIGN_CENTER_VERTICAL)
        hero.inner.Add(top, 0, wx.EXPAND)
        hero.inner.Add(text(hero, t, task.subtitle, 10.5, colour="ink2", wrap=COLUMN - 60), 0, wx.TOP, self.FromDIP(16))
        effect = {"copper": "The result comes back into your board with Apply, as one undo step.",
                  "placement": "The placement and the copper come back with Apply, as one undo step.",
                  "report": "Reads the board. Nothing on it changes."}.get(task.mode, "Saves a file in the project's .koppar folder.")
        if task.tool:
            effect += " Runs on a copy of the board in your Koppar account."
        row = wx.BoxSizer(wx.HORIZONTAL)
        icon = {"copper": "undo-2", "placement": "undo-2", "report": "search"}.get(task.mode, "folder-open")
        row.Add(wx.StaticBitmap(hero, bitmap=t.icon(icon, self.FromDIP(15), t["muted"], self)), 0, wx.TOP | wx.RIGHT, self.FromDIP(2))
        row.Add(text(hero, t, effect, 9.5, colour="muted", wrap=COLUMN - 90), 1, wx.LEFT, self.FromDIP(6))
        hero.inner.Add(row, 0, wx.TOP, self.FromDIP(12))
        page.add(hero)

        if not self._available(task):
            page.add(self._locked_card(page))
            page.done()
            page.Thaw()
            return

        form = Card(page, t, pad=24)
        if task.options:
            form.inner.Add(text(form, t, "Options", 12, 600, family="display"), 0, wx.BOTTOM, self.FromDIP(14))
        for opt in task.options:
            if opt.kind == "nets":
                form.inner.Add(text(form, t, opt.label, 9.5, 600, "ink2"), 0, wx.BOTTOM, self.FromDIP(8))
                self.scope = NetScope(form, t, self.census, opt.default, with_power=opt.default == "power")
                form.inner.Add(self.scope, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(18))
            elif opt.kind == "toggle":
                value = self.settings.get(opt.arg, opt.default) if task.id == "autoroute" else opt.default
                sw = Switch(form, t, opt.label, value)
                form.inner.Add(sw, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(12))
                self.controls[opt.arg] = (opt, sw)
            elif opt.kind == "minutes":
                r = wx.BoxSizer(wx.HORIZONTAL)
                r.Add(text(form, t, opt.label, 10, 500), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(14))
                lo, hi = opt.extra or (1, 30)
                stepper = MinutesStepper(form, t, opt.default, lo, hi)
                r.Add(stepper, 0, wx.ALIGN_CENTER_VERTICAL)
                form.inner.Add(r, 0, wx.BOTTOM, self.FromDIP(14))
                self.controls[opt.arg] = (opt, stepper)

        tool = self._server_tool(task)
        if tool is not None:
            curated = {o.arg for o in task.options if o.arg}
            holder = wx.Panel(form)
            holder.SetBackgroundColour(t["surface"])
            self.advanced = SchemaFields(holder, t, tool.get("input_schema") or {}, curated)
            hb = wx.BoxSizer(wx.VERTICAL)
            hb.Add(self.advanced, 0, wx.EXPAND | wx.TOP, self.FromDIP(10))
            holder.SetSizer(hb)
            holder.Hide()

            def toggle():
                holder.Show(not holder.IsShown())
                more.set_label("Fewer options" if holder.IsShown() else "More options")
                more.icon = "chevron-down" if holder.IsShown() else "chevron-right"
                page.done()

            more = Button(form, t, "More options", toggle, kind="ghost", icon="chevron-right", small=True)
            form.inner.Add(more, 0, wx.LEFT, -self.FromDIP(8))
            form.inner.Add(holder, 0, wx.EXPAND)

        foot = wx.BoxSizer(wx.HORIZONTAL)
        go = Button(form, t, self._verb(task), lambda: self._run(task), kind="primary",
                    icon=catalog.ICONS.get(task.id) if task.mode != "report" else None)
        foot.Add(go, 0)
        hint = ("The board, its .kicad_pro and .kicad_dru are uploaded and kept 24 hours."
                if task.id == "autoroute" else "Ctrl+Z after Apply puts the board back." if task.mode in ("copper", "placement") else "")
        if hint:
            foot.Add(text(form, t, hint, 9, colour="muted", wrap=360), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(16))
        form.inner.Add(foot, 0, wx.TOP, self.FromDIP(18 if task.options or tool else 0))
        page.add(form)
        page.done()
        page.Thaw()

    def _verb(self, task):
        return {"autoroute": "Route board", "route_nets": "Route nets", "auto_place": "Place parts",
                "polish_vias": "Remove vias", "drc": "Run check", "step": "Export STEP"}.get(task.id, task.verb)

    def _locked_card(self, page):
        t = self.theme
        card = Card(page, t, pad=24)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(IconTile(card, t, "key-round"), 0, wx.RIGHT, self.FromDIP(16))
        col = wx.BoxSizer(wx.VERTICAL)
        col.Add(text(card, t, "Part of your Koppar account", 13, 600, family="display"), 0)
        msg = self.account_error or ("This task runs on a copy of the board in your Koppar account. "
                                     "Add an API key from the Koppar app to use it.")
        col.Add(text(card, t, msg, 10, colour="ink2", wrap=COLUMN - 130), 0, wx.TOP, self.FromDIP(6))
        btns = wx.BoxSizer(wx.HORIZONTAL)
        btns.Add(Button(card, t, "Add API key", self._on_account, kind="primary", icon="key-round"), 0, wx.RIGHT, self.FromDIP(8))
        btns.Add(Button(card, t, "Create a key", lambda: webbrowser.open(API_KEYS_URL), icon="external-link"), 0)
        col.Add(btns, 0, wx.TOP, self.FromDIP(16))
        row.Add(col, 1)
        card.inner.Add(row, 0, wx.EXPAND)
        return card

    def _arguments(self, task):
        """The server arguments from the page, or raises ValueError with what is wrong."""
        args = {}
        for arg, (opt, ctrl) in self.controls.items():
            if opt.kind == "toggle":
                if ctrl.GetValue():
                    args[arg] = opt.extra if opt.extra is not None else True
            elif opt.kind == "minutes":
                args[arg] = int(ctrl.GetValue()) * 60
        nets_opt = next((o for o in task.options if o.kind == "nets"), None)
        if nets_opt is not None and self.scope is not None:
            nets = self.scope.nets()
            if nets is not None and not nets:
                raise ValueError(self.scope.count.GetLabel())
            if nets_opt.arg is not None:
                if nets is None:
                    if task.tool == "ripup_routing":
                        args["all"] = True
                else:
                    args[nets_opt.arg] = nets
        if self.advanced is not None:
            args.update(self.advanced.values())
        if task.tool == "widen_nets" and not args.get("nets"):
            raise ValueError("Pick the nets to widen.")
        return args

    # -- running -----------------------------------------------------------

    ROUTE_STAGES = ["Upload", "Queue", "Route", "Finish", "Check", "Download"]

    def _build_run_page(self, task):
        page, t = self.run_page, self.theme
        page.Freeze()
        page.reset()
        card = Card(page, t, pad=28)
        top = wx.BoxSizer(wx.HORIZONTAL)
        self.run_title = text(card, t, f"{task.running}", 18, 600, family="display")
        top.Add(self.run_title, 1, wx.ALIGN_CENTER_VERTICAL)
        self.run_elapsed = text(card, t, "0:00", 11, colour="muted", family="mono")
        top.Add(self.run_elapsed, 0, wx.ALIGN_CENTER_VERTICAL)
        card.inner.Add(top, 0, wx.EXPAND)
        card.inner.Add(text(card, t, task.title, 9.5, colour="muted"), 0, wx.TOP, self.FromDIP(2))
        self.trace = TraceProgress(card, t)
        card.inner.Add(self.trace, 0, wx.EXPAND | wx.TOP, self.FromDIP(24))
        self.run_detail = text(card, t, "", 10, colour="ink2", wrap=COLUMN - 60)
        card.inner.Add(self.run_detail, 0, wx.TOP, self.FromDIP(14))
        foot = wx.BoxSizer(wx.HORIZONTAL)
        self.stop_btn = Button(card, t, "Stop", self._on_stop, icon="square", small=True)
        foot.Add(self.stop_btn, 0, wx.RIGHT, self.FromDIP(14))
        self.stop_btn.Show(task.id == "autoroute")
        foot.Add(text(card, t, "Keep working on the board. Closing this window does not stop the run; "
                               "the Koppar button brings it back.", 9, colour="muted", wrap=440), 0, wx.ALIGN_CENTER_VERTICAL)
        card.inner.Add(foot, 0, wx.TOP, self.FromDIP(22))
        page.add(card)
        page.done()
        page.Thaw()

    def _start_run(self, task, stages):
        self.busy = True
        self.nav.enable(False)
        self.run_task = task
        self.run_started = time.time()
        self.run_stages = stages
        self.run_stage = 0
        self.run_text = ""
        self._build_run_page(task)
        self.trace.set(stages, 0)
        self.book.SetSelection(1)
        self.timer.Start(1000)
        self._ticks = 0
        self._tick()

    def _stage(self, index, text_=None):
        # called from worker threads too: plain attribute writes, painted by _tick
        self.run_stage = max(self.run_stage, index)
        if text_ is not None:
            self.run_text = text_

    def _tick(self):
        if not self.busy or self.closed:
            return
        self._ticks = getattr(self, "_ticks", 0) + 1
        self.trace.set(self.run_stages, self.run_stage)
        if self.run_detail.GetLabel() != self.run_text:
            self.run_detail.SetLabel(self.run_text)
            self.run_detail.Wrap(self.FromDIP(COLUMN - 60))
            self.run_page.Layout()
        self.run_elapsed.SetLabel(_fmt_s(time.time() - self.run_started))
        if self.run_task.id == "autoroute" and self._ticks % POLL_INTERVAL_S == 0:
            self._poll_route()

    def _end_run(self):
        self.busy = False
        self.timer.Stop()
        if getattr(self, "trace", None):
            self.trace.stop()
        self.nav.enable(True)

    def _run(self, task):
        if self.busy:
            return
        try:
            args = self._arguments(task)
        except ValueError as e:
            wx.MessageBox(str(e), task.title, wx.OK | wx.ICON_INFORMATION, self)
            return
        try:
            snapshot = self._snapshot()
        except Exception as e:
            wx.MessageBox(f"Could not read the board: {e}", task.title, wx.OK | wx.ICON_ERROR, self)
            return
        if len(snapshot.encode("utf-8")) > MAX_BOARD_BYTES:
            wx.MessageBox("The board file is over 20 MB, the most the server takes.", task.title, wx.OK | wx.ICON_WARNING, self)
            return
        if task.id == "autoroute":
            self._run_autoroute(task, snapshot)
        else:
            self._run_tool(task, args, board_io.project_zip(self.board_file, snapshot))

    def _snapshot(self):
        stem = os.path.splitext(os.path.basename(self.board_file))[0]
        return board_io.snapshot_board(pcbnew.GetBoard(), os.path.join(project_work_dir(self.board_file), f"{stem}.sent.kicad_pcb"))

    # -- Autoroute (the anonymous tools API) -----------------------------------

    def _run_autoroute(self, task, board_text):
        for arg, (opt, ctrl) in self.controls.items():
            self.settings[arg] = ctrl.GetValue()
        save_settings(self.settings)
        nets = self.scope.nets() if self.scope else None
        if nets is None:
            nets = [n["name"] for n in self.census]
        self._start_run(task, self.ROUTE_STAGES)
        self._stage(0, "Uploading the board")
        extra = board_io.project_files(self.board_file)

        def work():
            return self.tools_client.submit_route(board_text, extra.get("project"), extra.get("rules"),
                                                  preserve_existing=self.settings.get("preserve_existing", True),
                                                  vias_in_pads=self.settings.get("vias_in_pads", False))

        def done(result, error):
            if error:
                self._end_run()
                self._show_error(task, error)
                return
            self.session = {"job_id": result["job_id"], "submitted_at": time.time(), "server": self.tools_client.server,
                            "nets": nets, "import_zones": self.settings.get("import_zones", True),
                            "estimate": result.get("estimate")}
            save_session(self.board_file, self.session)
            self._stage(1, "Waiting for a router")
            self._poll_route()

        self._bg(work, done)

    def _poll_route(self):
        if getattr(self, "_polling", False) or not self.session.get("job_id"):
            return
        self._polling = True
        job_id = self.session["job_id"]

        def done(job, error):
            self._polling = False
            if error:
                if error.status == 404:
                    clear_session(self.board_file)
                    self._end_run()
                    self._show_error(self.run_task, ApiError(404, "The job is gone: the server keeps jobs for 24 hours. Route the board again."))
                else:
                    self.run_text = f"Waiting for the server: {error.detail}"
                return
            if self.busy:
                self._route_job(job)

        self._bg(lambda: self.tools_client.get_job(job_id), done)

    def _route_job(self, job):
        status, progress = job.get("status"), (job.get("progress") or "")
        p = progress.lower()
        if status == "queued":
            ahead = job.get("queue_position") or 0
            self._stage(1, f"{ahead} {'board' if ahead == 1 else 'boards'} ahead in the queue" if ahead else "Starting")
        elif status == "running":
            stage = 4 if ("drc" in p or "render" in p or "zone fill" in p) else 3 if any(
                k in p for k in ("finish", "tidy", "island", "polish", "fill")) else 2
            self._stage(stage, friendly_progress(progress))
        elif status == "completed":
            self._stage(5, "Bringing the result back")
            self._download_route(job)
        else:
            self._end_run()
            clear_session(self.board_file)
            if job.get("cancelled"):
                self._show_error(self.run_task, ApiError(0, "Nothing was changed on the board."), title="Stopped", tone="neutral")
            else:
                self._show_error(self.run_task, ApiError(0, job.get("error") or "The run failed."))

    def _download_route(self, job):
        if getattr(self, "_downloading", False):
            return
        self._downloading = True
        job_id = self.session["job_id"]
        stem = os.path.splitext(os.path.basename(self.board_file))[0]
        dest = os.path.join(project_work_dir(self.board_file), f"{stem}.routed.kicad_pcb")

        def work():
            data = self.tools_client.download_artifact(job_id, ROUTED_ARTIFACT)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            return dest

        def done(path, error):
            self._downloading = False
            self._end_run()
            if error:
                self._show_error(self.run_task, error)
                return
            self.session["result_path"] = path
            self.session["result"] = job.get("result")
            save_session(self.board_file, self.session)
            self._show_route_result()

        self._bg(work, done)

    def _route_pending(self):
        return {"source": "route", "mode": "copper", "result_path": self.session["result_path"],
                "nets": self.session.get("nets") or [], "import_zones": self.session.get("import_zones", True),
                "created_at": time.time()}

    def _show_route_result(self):
        task = catalog.task("autoroute")
        ctx = {}
        try:
            # how many nets the routed board connects, counted by KiCad on the result itself (not on a KiCad
            # older than the server's file: its merged copy has its pours unfilled until Apply refills them)
            if not board_io.is_newer_format(self.session["result_path"]):
                ctx["census_after"] = board_io.net_census(board_io.load_routed(self.session["result_path"]))
        except Exception:
            pass
        self.pending = None if self.session.get("applied_at") else self._route_pending()
        if self.pending:
            save_pending(self.board_file, self.pending)
        self._show_result(task, self.session.get("result"), ctx)

    def _on_stop(self):
        if not self.session.get("job_id"):
            return
        if wx.MessageBox("Stop this run? The server drops it and nothing on the board changes.",
                         "Koppar", wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
            return
        self.stop_btn.Enable(False)
        job_id = self.session["job_id"]

        def done(job, error):
            if error:
                self.stop_btn.Enable(True)
                wx.MessageBox(f"Could not stop the run: {error.detail}", "Koppar", wx.OK | wx.ICON_ERROR, self)
                return
            self._route_job(job)

        self._bg(lambda: self.tools_client.cancel_job(job_id), done)

    # -- account tools ---------------------------------------------------------

    def _run_tool(self, task, args, zip_bytes):
        mode = catalog.mode_of(task.tool, args)
        self._start_run(task, ["Send", task.running, "Bring back"])
        self._stage(0, "Sending the board as it is in the editor")

        def progress(text_, _elapsed):
            text_ = text_ or "running"
            self._stage(1, text_[:1].upper() + text_[1:])

        def work():
            self.workspace.push(zip_bytes)
            self._stage(1, "Working on the board")
            view = self.workspace.run(task.tool, args, on_progress=progress, is_cancelled=lambda: self.closed)
            out = {"view": view}
            if mode != "report":
                self._stage(2, "Bringing the result back")
                out["board"] = self.workspace.fetch("workspace.kicad_pcb")
            return out

        def done(out, error):
            self._end_run()
            self._show_account()
            if error:
                self._show_error(task, error)
                return
            result = out["view"].get("result")
            ctx = {"nets": args.get("nets")}
            self.pending = None
            if out.get("board"):
                self.pending = {"source": task.tool, "mode": mode, "result_path": out["board"], "nets": None,
                                "import_zones": True, "created_at": time.time()}
                save_pending(self.board_file, self.pending)
            self._show_result(task, result, ctx)

        self._bg(work, done)

    # -- result page -----------------------------------------------------------

    def _show_result(self, task, result, ctx):
        self.result_for = task
        self.applied_text = None
        self.result_summary = summary.summarize(task.id, result, ctx or {})
        self.show_all_details = False
        s = self.result_summary
        self._result_page(task, s["headline"], s["tone"], s["tiles"], rich.summary_blocks(s["sections"]))

    def _show_error(self, task, error, title=None, tone="bad"):
        self.pending = None
        self.result_for = task
        self.result_summary = None
        if error.status == 402:
            title, tone = "This board needs an API key", "warn"
        blocks = [{"kind": "p", "spans": rich.inline(error.detail)}]
        if error.status == 402:
            blocks.append({"kind": "p", "spans": [("Add an API key", "link", "action:account"), ("   ", "normal", None),
                                                  ("Create one in the Koppar app", "link", "url:" + API_KEYS_URL)]})
        self._result_page(task, title or f"{task.title} did not finish", tone, [], blocks)

    def _result_page(self, task, headline, tone, tiles, blocks):
        page, t = self.result_page, self.theme
        self.result_headline, self.result_tiles = headline, tiles
        page.Freeze()
        page.reset()
        card = Card(page, t, pad=24)
        top = wx.BoxSizer(wx.HORIZONTAL)
        icon = {"good": "circle-check", "warn": "triangle-alert", "bad": "circle-alert"}.get(tone, catalog.ICONS.get(task.id, "circle-dot"))
        top.Add(IconTile(card, t, icon, tone if tone in ("good", "warn", "bad") else None), 0, wx.RIGHT, self.FromDIP(16))
        heads = wx.BoxSizer(wx.VERTICAL)
        heads.Add(text(card, t, task.title, 9.5, 500, "muted"), 0)
        heads.Add(text(card, t, headline, 17, 600, family="display", wrap=COLUMN - 120), 0, wx.TOP, self.FromDIP(1))
        top.Add(heads, 1, wx.ALIGN_CENTER_VERTICAL)
        card.inner.Add(top, 0, wx.EXPAND)
        self.applied_holder = wx.BoxSizer(wx.VERTICAL)
        card.inner.Add(self.applied_holder, 0, wx.EXPAND)
        if tiles:
            # the numbers share the width, four to a row
            for start in range(0, len(tiles), 4):
                grid = wx.BoxSizer(wx.HORIZONTAL)
                chunk = tiles[start:start + 4]
                for i, d in enumerate(chunk + [None] * (4 - len(chunk) if len(tiles) > 4 else 0)):
                    if d is None:
                        grid.AddStretchSpacer(1)
                    else:
                        grid.Add(StatTile(card, t, d), 1, wx.EXPAND | (wx.RIGHT if i < 3 else 0), self.FromDIP(10))
                card.inner.Add(grid, 0, wx.EXPAND | wx.TOP, self.FromDIP(20 if start == 0 else 10))
        foot = wx.BoxSizer(wx.HORIZONTAL)
        self.apply_btn = Button(card, t, "Apply to board", self._on_apply, kind="primary", icon="check")
        foot.Add(self.apply_btn, 0, wx.RIGHT, self.FromDIP(8))
        self.apply_btn.Show(self.pending is not None)
        foot.Add(Button(card, t, "Back to the task", lambda: self._pick(NavItem(task.id, "", "", ""))), 0, wx.RIGHT, self.FromDIP(8))
        foot.Add(Button(card, t, "Results folder", self._open_folder, kind="ghost", icon="folder-open"), 0)
        card.inner.Add(foot, 0, wx.TOP, self.FromDIP(14 if tiles else 20))
        page.add(card)
        if blocks:
            details = Card(page, t, pad=24)
            self.details_view = RichView(details, t, self._on_link)
            details.inner.Add(self.details_view, 0, wx.EXPAND)
            page.add(details)
            details.Layout()
            self.details_view.SetSize((self.FromDIP(COLUMN - 48), -1))
            self.details_view.set_blocks(blocks)
        page.done()
        page.Thaw()
        self.book.SetSelection(2)
        self.nav.select(task.id)

    def _open_folder(self):
        os.makedirs(project_work_dir(self.board_file), exist_ok=True)
        open_path(project_work_dir(self.board_file))

    def _on_link(self, link):
        kind, _, value = link.partition(":")
        if kind == "action" and value == "details" and self.result_summary:
            self.show_all_details = not self.show_all_details
            self.details_view.set_blocks(rich.summary_blocks(self.result_summary["sections"], self.show_all_details))
            self.result_page.done()
        elif kind == "action" and value == "account":
            self._on_account()
        elif kind == "url":
            webbrowser.open(value)
        elif kind == "file":
            open_path(value)
        else:
            show_on_board(kind, value)

    def _on_apply(self):
        if not self.pending:
            return
        save_pending(self.board_file, self.pending)
        if not trigger_action(APPLY_ACTION_NAME):
            wx.MessageBox(f"Click Tools > External Plugins > {APPLY_ACTION_NAME} to bring the result into the board.",
                          "Koppar", wx.OK | wx.ICON_INFORMATION, self)

    def mark_applied(self, text_):
        """The apply action brought this window's result into the board."""
        if self.closed:
            return
        self.applied_text = text_
        self.pending = None
        message = "Applied. " + " ".join(text_.split())
        if self.book.GetSelection() == 3:
            self._chat_applied(message)
            return
        if getattr(self, "apply_btn", None):
            self.apply_btn.Hide()
        if getattr(self, "applied_holder", None) is not None:
            card = self.apply_btn.GetParent()
            self.applied_holder.Clear(True)
            self.applied_holder.Add(Banner(card, self.theme, message), 0, wx.EXPAND | wx.TOP, self.FromDIP(18))
            self.result_page.done()

    # -- chat ----------------------------------------------------------------

    def _build_chat_page(self):
        t = self.theme
        page = self.chat_page
        page.SetBackgroundColour(t["paper"])
        box = wx.BoxSizer(wx.VERTICAL)
        self.transcript = Paper(page, t)
        box.Add(self.transcript, 1, wx.EXPAND)
        bar = wx.Panel(page)
        bar.SetBackgroundColour(t["paper"])
        bar.paper = True
        bar.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        bar.Bind(wx.EVT_PAINT, lambda e: self._paint_paper(bar))
        bb = wx.BoxSizer(wx.HORIZONTAL)
        col = wx.BoxSizer(wx.VERTICAL)
        self.composer = Composer(bar, t, self._send_chat, "Ask about this board, or describe a new one")
        self.composer.SetMinSize((self.FromDIP(COLUMN), -1))
        col.Add(self.composer, 0, wx.EXPAND)
        self.chat_note = text(bar, t, "", 9, colour="muted")
        col.Add(self.chat_note, 0, wx.TOP | wx.LEFT, self.FromDIP(6))
        bb.Add(col, 0, wx.LEFT | wx.BOTTOM, self.FromDIP(28))
        bb.Prepend(0, self.FromDIP(4))
        bar.SetSizer(bb)
        box.Add(bar, 0, wx.EXPAND)
        page.SetSizer(box)
        self.chat_bar = bar

    def _paint_paper(self, win):
        dc = wx.AutoBufferedPaintDC(win)
        gc = _gc(dc)
        w, h = win.GetClientSize()
        paint_paper(gc, w, h, self.theme, win, (0, 0))

    def _open_chat(self):
        self.book.SetSelection(3)
        q = self._quota()
        left = "unlimited runs" if q.get("unlimited") else f"{q.get('remaining', 0)} runs left"
        self.chat_note.SetLabel(f"Koppar works on a copy of the board in your account ({left}). "
                                "Changes come back when you press Apply.")
        design = self.workspace.design_id if self.workspace else None
        if design and self.chat_loaded_for != design:
            self.chat_loaded_for = design
            self._bg(lambda: self.account.chat_history(design), self._chat_history)
        elif not design and self.chat_loaded_for is None:
            self.chat_loaded_for = ""
            self._chat_render([])
        self.composer.field.SetFocus()

    def _chat_history(self, history, error):
        self._chat_render([] if error else history.get("messages") or [])

    def _chat_render(self, messages):
        page, t = self.transcript, self.theme
        page.Freeze()
        page.reset()
        self.chat_empty = not messages
        if not messages:
            card = Card(page, t, pad=28)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(IconTile(card, t, "message-square"), 0, wx.RIGHT, self.FromDIP(16))
            col = wx.BoxSizer(wx.VERTICAL)
            col.Add(text(card, t, "Ask Koppar", 17, 600, family="display"), 0)
            col.Add(text(card, t, "Koppar reads the board you have open and can change it: route nets, move parts, "
                                  "remove vias, check it. What it changes comes back as one undo step. It can also "
                                  "design a new board from a description, schematic and layout, as a KiCad project of "
                                  "its own.",
                         10.5, colour="ink2", wrap=COLUMN - 130), 0, wx.TOP, self.FromDIP(6))
            ideas = wx.BoxSizer(wx.VERTICAL)
            for idea in ("Why are some nets not routed?", "Route the power nets with wide tracks",
                         "Can this board have fewer vias?",
                         "Design a new board: a USB-C LiPo charger with a 3.3 V output"):
                ideas.Add(Button(card, t, idea, lambda s=idea: self._send_chat(s), kind="secondary", small=True), 0,
                          wx.BOTTOM, self.FromDIP(8))
            col.Add(ideas, 0, wx.TOP, self.FromDIP(18))
            row.Add(col, 1)
            card.inner.Add(row, 0, wx.EXPAND)
            page.add(card)
        else:
            for m in messages:
                self._chat_add(m["role"], m.get("content") or "", m.get("steps") or [])
        page.done()
        page.Thaw()
        self._chat_scroll_end()

    def _chat_add(self, role, content, steps=()):
        """A message in the transcript; for the assistant, its steps and its text."""
        page, t = self.transcript, self.theme
        if role == "user":
            holder = wx.BoxSizer(wx.HORIZONTAL)
            holder.AddStretchSpacer()
            bubble = Bubble(page, t, content)
            bubble.SetMinSize((min(self.FromDIP(520), self._text_width(content) + self.FromDIP(40)), -1))
            holder.Add(bubble, 0)
            page.column.Add(holder, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(16))
            bubble.Layout()
            bubble.view.set_blocks(rich.markdown(content))
            return None
        card = Card(page, t, pad=20)
        head = wx.BoxSizer(wx.HORIZONTAL)
        mark = wx.Image(os.path.join(HERE, "logo.png"))
        s = self.FromDIP(20)
        head.Add(wx.StaticBitmap(card, bitmap=wx.Bitmap(mark.Scale(s, s, wx.IMAGE_QUALITY_HIGH))), 0, wx.RIGHT, self.FromDIP(8))
        head.Add(text(card, t, "Koppar", 10, 600), 0, wx.ALIGN_CENTER_VERTICAL)
        card.inner.Add(head, 0, wx.BOTTOM, self.FromDIP(10))
        steps_box = wx.BoxSizer(wx.VERTICAL)
        card.inner.Add(steps_box, 0, wx.EXPAND)
        view = RichView(card, t, self._on_link, size=10.5)
        card.inner.Add(view, 0, wx.EXPAND)
        page.add(card)
        card.Layout()
        view.SetSize((self.FromDIP(COLUMN - 40), -1))
        rows = {}
        for st in steps:
            if st.get("type") == "tool":
                row = StepRow(card, t, catalog.tool_label(st.get("name", "")))
                row.finish(st.get("status") if st.get("status") in ("done", "failed") else "done", st.get("detail") or "")
                steps_box.Add(row, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(2))
        if content:
            view.set_blocks(rich.markdown(content))
        turn = {"card": card, "view": view, "steps": steps_box, "rows": rows, "text": content}
        for st in steps:
            if st.get("design"):
                self._design_card(turn, st["design"])
        return turn

    def _text_width(self, s):
        dc = wx.ClientDC(self)
        dc.SetFont(self.theme.text(10.5))
        return max((dc.GetTextExtent(line)[0] for line in s.split("\n")), default=0)

    def _chat_scroll_end(self):
        page = self.transcript
        page.FitInside()
        _, vh = page.GetVirtualSize()
        page.Scroll(0, vh)

    def _send_chat(self, message):
        if self.chat_busy or not self.account:
            return
        try:
            zip_bytes = board_io.project_zip(self.board_file, self._snapshot())
        except Exception as e:
            wx.MessageBox(f"Could not read the board: {e}", "Koppar", wx.OK | wx.ICON_ERROR, self)
            return
        self.chat_busy = True
        self.composer.set_busy(True)
        self.composer.field.SetValue("")
        page = self.transcript
        page.Freeze()
        if self.chat_empty:
            page.reset()
            self.chat_empty = False
        self._chat_add("user", message)
        turn = self._chat_add("assistant", "")
        self.chat_turn = turn
        thinking = StepRow(turn["card"], self.theme, "Reading the board")
        turn["steps"].Add(thinking, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(2))
        turn["thinking"] = thinking
        page.done()
        page.Thaw()
        self._chat_scroll_end()
        self._chat_dirty = False
        self.chat_timer = wx.CallLater(120, self._chat_flush)

        def on_event(ev):
            wx.CallAfter(self._chat_event, turn, ev)

        def work():
            self.workspace.push(zip_bytes)
            self.chat_loaded_for = self.workspace.design_id
            self.account.chat(self.workspace.design_id, message, on_event, is_cancelled=lambda: self.closed)
            return True

        def done(_ok, error):
            self.chat_busy = False
            self.composer.set_busy(False)
            self._show_account()
            if error:
                self._chat_event(turn, {"type": "error", "detail": error.detail})
                self._chat_event(turn, {"type": "done", "board_changed": []})

        self._bg(work, done)

    def _chat_event(self, turn, ev):
        if self.closed:
            return
        kind = ev.get("type")
        if turn.get("thinking") is not None and kind in ("text", "tool", "error", "done"):
            turn["thinking"].Destroy()
            turn["thinking"] = None
            turn["card"].Layout()
        if kind == "text":
            turn["text"] += ev.get("delta", "")
            self._chat_dirty = True
        elif kind == "tool":
            rows = turn["rows"]
            if ev.get("status") == "running" or ev.get("id") not in rows:
                row = StepRow(turn["card"], self.theme, catalog.tool_label(ev.get("name", "")))
                turn["steps"].Add(row, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(2))
                rows[ev.get("id")] = row
                self.transcript.done()
                self._chat_scroll_end()
            if ev.get("status") in ("done", "failed"):
                rows[ev.get("id")].finish(ev["status"], ev.get("detail") or "")
        elif kind == "design":
            self._design_card(turn, ev)
        elif kind == "error":
            turn["text"] += ("\n\n" if turn["text"] else "") + f"**Stopped.** {ev.get('detail', '')}"
            self._chat_dirty = True
        elif kind == "done":
            self._chat_flush(final=True)
            changed = ev.get("board_changed") or []
            if changed:
                self._chat_fetch_change(turn, changed)

    def _chat_flush(self, final=False):
        """Redraw the reply being written, a few times a second rather than per word."""
        turn = getattr(self, "chat_turn", None)
        if turn is None or self.closed:
            return
        if self._chat_dirty:
            self._chat_dirty = False
            turn["view"].set_blocks(rich.markdown(turn["text"]))
            self.transcript.done()
            self._chat_scroll_end()
        if not final and self.chat_busy:
            self.chat_timer = wx.CallLater(120, self._chat_flush)

    def _chat_fetch_change(self, turn, changed):
        mode = "placement" if any(c in catalog.PLACEMENT_TOOLS for c in changed) else "copper"

        def done(path, error):
            if error:
                return
            self.pending = {"source": "chat", "mode": mode, "result_path": path, "nets": None,
                            "import_zones": True, "created_at": time.time()}
            save_pending(self.board_file, self.pending)
            card = turn["card"]
            row = wx.BoxSizer(wx.HORIZONTAL)
            banner = Banner(card, self.theme, "Koppar changed the board in your workspace. Apply brings it into KiCad as one undo step.",
                            tone="neutral", icon="circuit-board")
            row.Add(banner, 1, wx.EXPAND | wx.RIGHT, self.FromDIP(10))
            btn = Button(card, self.theme, "Apply to board", self._on_apply, kind="primary", icon="check")
            row.Add(btn, 0, wx.ALIGN_CENTER_VERTICAL)
            card.inner.Add(row, 0, wx.EXPAND | wx.TOP, self.FromDIP(14))
            turn["apply"] = (row, banner, btn)
            self.transcript.done()
            self._chat_scroll_end()

        self._bg(lambda: self.workspace.fetch("workspace.kicad_pcb"), done)

    def _chat_applied(self, message):
        turn = getattr(self, "chat_turn", None)
        if turn and turn.get("apply"):
            row, banner, btn = turn["apply"]
            banner.message, banner.tone, banner.icon = message, "good", "circle-check"
            banner._wrap()
            btn.Hide()
            turn["card"].Layout()
            self.transcript.done()
            self._chat_scroll_end()

    # -- new designs ------------------------------------------------------------

    def _design_card(self, turn, design):
        """The card of a design the chat started, under the reply; it follows the run until it ends."""
        design_id = design.get("design_id")
        if not design_id:
            return
        record = self.designs.setdefault(design_id, {})
        record.update({k: design[k] for k in ("title", "web_url") if design.get(k)})
        save_designs(self.board_file, self.designs)
        card = turn["card"]
        panel = DesignCard(card, self.theme, record.get("title"), lambda: self._open_design(design_id),
                           lambda: webbrowser.open(self.designs[design_id].get("web_url") or ACCOUNT_SERVER),
                           lambda: open_path(os.path.dirname(self.designs[design_id]["project"])))
        card.inner.Add(panel, 0, wx.EXPAND | wx.TOP, self.FromDIP(14))
        self.design_cards[design_id] = panel
        self._design_show(design_id, record.get("progress"))
        self.transcript.done()
        self._chat_scroll_end()
        self._poll_design(design_id)

    def _design_show(self, design_id, progress):
        panel = self.design_cards.get(design_id)
        if not panel:
            return
        record = self.designs.get(design_id, {})
        p = progress or {}
        board = p.get("board") or {}
        if record.get("project") and os.path.exists(record["project"]):
            folder = os.path.basename(os.path.dirname(record["project"]))
            panel.show(f"In KiCad, from the folder {folder}.", "good", True, "Open again", folder=True)
        elif not p:
            panel.show("Starting")
        elif not p.get("done"):
            where = f"Board: {board.get('stage')}" if board.get("status") == "running" else f"Schematic: {p.get('stage') or 'running'}"
            panel.show(where)
        elif p.get("has_project") and board.get("status") == "completed":
            panel.show("Ready: the schematic and the routed board.", "good", True)
        elif p.get("has_project") and not board:
            panel.show("Ready: the schematic.", "good", True)
        elif p.get("has_project"):
            why = f": {board['error']}" if board.get("error") else ""
            panel.show(f"The schematic is ready; the board did not finish{why}.", "warn", True)
        else:
            why = (p.get("schematic") or {}).get("error") or "see it in Koppar"
            panel.show(f"The design stopped: {why}", "bad")
        # the card's height may have changed: lay the transcript out again where it is scrolled to
        self.transcript.Layout()
        self.transcript.FitInside()
        self.transcript.Refresh()

    def _poll_design(self, design_id):
        """Ask where the design stands every so often, until it ends (one poll per design)."""
        record = self.designs.get(design_id, {})
        if design_id in self.design_polls or (record.get("progress") or {}).get("done") or not self.account:
            return
        self.design_polls.add(design_id)

        def done(progress, error):
            self.design_polls.discard(design_id)
            if error is None:
                self.designs[design_id]["progress"] = progress
                save_designs(self.board_file, self.designs)
                self._design_show(design_id, progress)
                if progress.get("done"):
                    return
            if not self.closed:
                wx.CallLater(15000 if error is None else 45000, lambda: None if self.closed else self._poll_design(design_id))

        self._bg(lambda: self.account.design_progress(design_id), done)

    def _open_design(self, design_id):
        record = self.designs.get(design_id, {})
        if record.get("project") and os.path.exists(record["project"]):
            self._launch_project(record["project"])
            return
        start = os.path.dirname(os.path.dirname(os.path.abspath(self.board_file)))
        with wx.DirDialog(self, "Where should the new project go?", start, wx.DD_DEFAULT_STYLE) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            parent = dlg.GetPath()
        folder = projects.free_folder(parent, projects.folder_name(record.get("title")))
        panel = self.design_cards.get(design_id)
        if panel:
            panel.show("Downloading the project")

        def work():
            return projects.unpack(self.account.download_result(design_id, "project.zip"), folder)

        def done(pro, error):
            if error or not pro:
                self._design_show(design_id, record.get("progress"))
                wx.MessageBox(f"Could not bring the project in: {error.detail if error else 'the download has no project file'}",
                              "Koppar", wx.OK | wx.ICON_ERROR, self)
                return
            record["project"] = pro
            save_designs(self.board_file, self.designs)
            self._design_show(design_id, record.get("progress"))
            self._launch_project(pro)

        self._bg(work, done)

    def _launch_project(self, pro):
        if not projects.open_in_kicad(pro, wx.LaunchDefaultApplication):
            open_path(os.path.dirname(pro))
            wx.MessageBox(f"The project is in {os.path.dirname(pro)}. Open {os.path.basename(pro)} in KiCad.",
                          "Koppar", wx.OK | wx.ICON_INFORMATION, self)

    # -- restore -----------------------------------------------------------

    def _restore(self):
        """Pick a routing job up again: its result when it was downloaded, the run otherwise."""
        s = self.session
        if not s.get("job_id") or s.get("applied_at"):
            return
        self.task = catalog.task("autoroute")
        self.nav.select("autoroute")
        if s.get("result_path") and os.path.exists(s["result_path"]):
            self._show_route_result()
        else:
            self._start_run(self.task, self.ROUTE_STAGES)
            self.run_started = float(s.get("submitted_at", time.time()))
            self._stage(1, "Reconnecting to the run")
            self._poll_route()

    # -- plumbing ----------------------------------------------------------

    def _bg(self, work, done):
        """Run `work()` on a thread; `done(result, error)` back on the UI thread."""
        def run():
            try:
                result, error = work(), None
            except ApiError as e:
                result, error = None, e
            except Exception as e:  # a bug must still reach the window
                result, error = None, ApiError(0, f"{type(e).__name__}: {e}")
            wx.CallAfter(lambda: None if self.closed else done(result, error))
        threading.Thread(target=run, daemon=True).start()

    def _on_key(self, event):
        if event.GetKeyCode() == wx.WXK_TAB:
            KEYBOARD["on"] = True
            wx.CallAfter(self.Refresh)
        event.Skip()

    def _on_close(self, event):
        self.closed = True
        self.timer.Stop()
        event.Skip()


PROGRESS = [
    (r"ML router: (\d+) nets? to route, (\d+) kept", lambda m: f"Routing {m[1]} nets, keeping the {m[2]} already connected"),
    (r"ML router: (\d+) nets? to route", lambda m: f"Routing {m[1]} nets"),
    (r"^route$|^starting$", lambda m: "Routing"),
    (r"^fill", lambda m: "Filling the copper pours"),
    (r"island", lambda m: "Joining a pour island to its net"),
    (r"^finish|repair", lambda m: "Finishing the last connections"),
    (r"tidy|copper finishing|polish", lambda m: "Polishing the copper"),
    (r"zone fill \+ DRC|drc", lambda m: "Checking the board with KiCad's DRC"),
    (r"render", lambda m: "Drawing the result"),
]


def friendly_progress(text_):
    """The server's progress line, as a person reads it."""
    import re

    for pattern, say in PROGRESS:
        m = re.search(pattern, text_, re.IGNORECASE)
        if m:
            return say(m)
    return text_[:1].upper() + text_[1:]


def _focus(item):
    """Zoom to an item; KiCad 7 has no FocusOnItem, so there it is selected instead."""
    if hasattr(pcbnew, "FocusOnItem"):
        pcbnew.FocusOnItem(item)
    else:
        item.SetSelected()


def show_on_board(kind, value):
    """A result link: zoom to a part, light a net up, or go to a spot."""
    board = pcbnew.GetBoard()
    try:
        if kind == "ref":
            fp = board.FindFootprintByReference(value)
            if fp:
                _focus(fp)
        elif kind == "net":
            nets = board.GetNetsByName()
            if nets.has_key(value):
                code = nets[value].GetNetCode()
                board.SetHighLightNet(code)
                board.HighLightON(True)
                pad = next((p for p in board.GetPads() if p.GetNetCode() == code), None)
                if pad:
                    _focus(pad)
        elif kind == "xy":
            x, y = (pcbnew.FromMM(float(v)) for v in value.split(","))
            pad = min(board.GetPads(), key=lambda p: (p.GetPosition().x - x) ** 2 + (p.GetPosition().y - y) ** 2, default=None)
            if pad:
                _focus(pad)
        pcbnew.Refresh()
    except Exception as e:
        print(f"[Koppar] could not show {kind}:{value}: {e}")


class AccountDialog(wx.Dialog):
    """The API key."""

    def __init__(self, parent, theme, settings):
        super().__init__(parent, title="Koppar account")
        self.SetBackgroundColour(theme["surface"])
        root = wx.BoxSizer(wx.VERTICAL)
        pad = self.FromDIP(20)
        root.Add(text(self, theme, "Your Koppar account", 15, 600, family="display"), 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        root.Add(text(self, theme, "An API key opens the board tools and the assistant, and boards past the free size. "
                                   "Create one in the Koppar app, under Settings.", 10, colour="ink2", wrap=440),
                 0, wx.LEFT | wx.RIGHT | wx.TOP, pad // 2)
        grid = wx.FlexGridSizer(0, 2, self.FromDIP(10), self.FromDIP(12))
        grid.AddGrowableCol(1)
        grid.Add(text(self, theme, "API key", 9.5, 600, "ink2"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.key = wx.TextCtrl(self, value=settings.get("api_key", ""), style=wx.TE_PASSWORD, size=(self.FromDIP(320), -1))
        self.key.SetHint("sc_...")
        grid.Add(self.key, 1, wx.EXPAND)
        root.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, pad)
        if cli_login() and not settings.get("api_key"):
            root.Add(text(self, theme, "Using the key of your Koppar CLI login until one is entered here.", 9, colour="muted"),
                     0, wx.LEFT | wx.RIGHT | wx.TOP, pad)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(Button(self, theme, "Create a key", lambda: webbrowser.open(API_KEYS_URL), kind="ghost", icon="external-link", small=True), 0)
        row.AddStretchSpacer()
        row.Add(Button(self, theme, "Cancel", lambda: self.EndModal(wx.ID_CANCEL)), 0, wx.RIGHT, self.FromDIP(8))
        row.Add(Button(self, theme, "Save", self._on_ok, kind="primary"), 0)
        root.Add(row, 0, wx.EXPAND | wx.ALL, pad)
        root.Add(text(self, theme, f"Koppar for KiCad {VERSION}", 8.5, colour="faint"), 0, wx.LEFT | wx.BOTTOM, pad)
        self.SetSizerAndFit(root)
        self.CentreOnParent()

    def _on_ok(self):
        key = self.key.GetValue().strip()
        if key and not (key.startswith("sc_") and len(key) == 43):
            wx.MessageBox("That does not look like a Koppar API key (sc_ and 40 characters).",
                          "Koppar account", wx.OK | wx.ICON_WARNING, self)
            return
        self.EndModal(wx.ID_OK)

    def values(self):
        return {"api_key": self.key.GetValue().strip()}
