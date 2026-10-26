"""The window's controls, drawn with wx.GraphicsContext so they look the same on
GTK, Windows and macOS and match the app: buttons, segmented choices,
switches, a minutes stepper, chips, cards, stat tiles, the task list, the
run's copper trace, and a rich text view for replies and result details.
"""

import math
import time

import wx

from . import rich
from .style import paint_paper


# focus rings show only while the keyboard drives (Tab), never after a click
KEYBOARD = {"on": False}


def pointer():
    KEYBOARD["on"] = False


def _gc(dc):
    gc = wx.GraphicsContext.Create(dc)
    gc.SetAntialiasMode(wx.ANTIALIAS_DEFAULT)
    return gc


def _extent(gc, text):
    w, h, _, _ = gc.GetFullTextExtent(text)
    return w, h


def paper_owner(win):
    """The ancestor that paints the paper, and `win`'s offset in its (unscrolled) space."""
    x, y = 0, 0
    w = win
    while w is not None and not getattr(w, "paper", False):
        px, py = w.GetPosition()
        x, y = x + px, y + py
        w = w.GetParent()
    if w is not None and isinstance(w, wx.ScrolledWindow):
        x, y = w.CalcUnscrolledPosition(x, y)
    return w, (x, y)


def paint_backdrop(gc, win, w, h, theme):
    """What sits behind a control: the paper when it is on the paper, its parent's colour otherwise."""
    parent = win.GetParent()
    if getattr(parent, "paper", False) or getattr(parent, "transparent", False):
        owner, origin = paper_owner(win)
        if owner is not None:
            paint_paper(gc, w, h, theme, win, origin)
            return
    gc.SetBrush(wx.Brush(parent.GetBackgroundColour()))
    gc.SetPen(wx.TRANSPARENT_PEN)
    gc.DrawRectangle(0, 0, w, h)


class Painted(wx.Control):
    """A control that draws itself and knows hover, press and focus."""

    def __init__(self, parent, theme, focusable=False):
        super().__init__(parent, style=wx.BORDER_NONE | (wx.WANTS_CHARS if focusable else 0))
        self.theme = theme
        self.hover = self.pressed = False
        self.focusable = focusable
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ENTER_WINDOW, lambda e: self._set("hover", True))
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: (self._set("hover", False), self._set("pressed", False)))
        self.Bind(wx.EVT_SET_FOCUS, lambda e: self.Refresh())
        self.Bind(wx.EVT_KILL_FOCUS, lambda e: self.Refresh())
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    def AcceptsFocus(self):
        return self.focusable and self.IsEnabled()

    def _set(self, attr, value):
        if getattr(self, attr) != value:
            setattr(self, attr, value)
            self.Refresh()

    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        gc = _gc(dc)
        w, h = self.GetClientSize()
        paint_backdrop(gc, self, w, h, self.theme)
        self.draw(gc, w, h)

    def draw(self, gc, w, h):
        raise NotImplementedError

    def focus_ring(self, gc, x, y, w, h, r):
        if self.HasFocus() and self.focusable and KEYBOARD["on"]:
            gc.SetPen(wx.Pen(self.theme["focus"], self.FromDIP(2)))
            gc.SetBrush(wx.TRANSPARENT_BRUSH)
            gc.DrawRoundedRectangle(x - 2, y - 2, w + 4, h + 4, r + 2)


class Button(Painted):
    """primary: the page's one action, in ink; secondary: outlined; ghost: text only; danger: for undoing work."""

    def __init__(self, parent, theme, label, on_click=None, kind="secondary", icon=None, small=False):
        super().__init__(parent, theme, focusable=True)
        self.label, self.kind, self.icon, self.small = label, kind, icon, small
        self.on_click = on_click
        self.font = theme.text(9.5 if small else 10, 600 if kind == "primary" else 500)
        self.Bind(wx.EVT_LEFT_DOWN, lambda e: (pointer(), self._set("pressed", True), self.SetFocus()))
        self.Bind(wx.EVT_LEFT_UP, self._release)
        self.Bind(wx.EVT_KEY_DOWN, self._key)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self._fit()

    def _fit(self):
        dc = wx.ClientDC(self)
        dc.SetFont(self.font)
        tw, th = dc.GetTextExtent(self.label)
        icon = self.FromDIP(16) + self.FromDIP(8) if self.icon else 0
        padx = self.FromDIP(12 if self.small else 16)
        self.SetMinSize((tw + icon + 2 * padx, self.FromDIP(30 if self.small else 36)))

    def set_label(self, label):
        self.label = label
        self._fit()
        self.Refresh()

    def _release(self, event):
        was = self.pressed
        self._set("pressed", False)
        if was and self.IsEnabled() and self.on_click:
            self.on_click()

    def _key(self, event):
        if event.GetKeyCode() in (wx.WXK_SPACE, wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and self.on_click and self.IsEnabled():
            self.on_click()
        else:
            event.Skip()

    def Enable(self, enable=True):
        r = super().Enable(enable)
        self.Refresh()
        return r

    def draw(self, gc, w, h):
        t = self.theme
        r = self.FromDIP(8)
        enabled = self.IsEnabled()
        if self.kind == "primary":
            fill = t["primary"] if enabled else t["line2"]
            if enabled and (self.hover or self.pressed):
                fill = wx.Colour(fill.Red() * 0.85 + 30, fill.Green() * 0.85 + 30, fill.Blue() * 0.85 + 30) if t.dark else \
                    wx.Colour(min(255, fill.Red() + 40), min(255, fill.Green() + 38), min(255, fill.Blue() + 36))
            ink = t["primary_ink"] if enabled else t["surface"]
            gc.SetBrush(wx.Brush(fill))
            gc.SetPen(wx.TRANSPARENT_PEN)
        elif self.kind == "ghost":
            gc.SetBrush(wx.Brush(t["hover"]) if (self.hover and enabled) else wx.TRANSPARENT_BRUSH)
            gc.SetPen(wx.TRANSPARENT_PEN)
            ink = t["ink2"] if enabled else t["faint"]
        else:
            gc.SetBrush(wx.Brush(t["hover"] if (self.hover and enabled) else t["surface"]))
            gc.SetPen(wx.Pen(t["line2"] if enabled else t["line"], 1))
            ink = (t["bad"] if self.kind == "danger" else t["ink"]) if enabled else t["faint"]
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - 1, r)
        gc.SetFont(self.font, ink)
        tw, th = _extent(gc, self.label)
        isz = self.FromDIP(16) if self.icon else 0
        gap = self.FromDIP(8) if self.icon else 0
        x = (w - tw - isz - gap) / 2
        if self.icon:
            bmp = t.icon(self.icon, isz, ink, self)
            if bmp.IsOk():
                gc.DrawBitmap(bmp, x, (h - isz) / 2, isz, isz)
        gc.DrawText(self.label, x + isz + gap, (h - th) / 2)
        self.focus_ring(gc, 0.5, 0.5, w - 1, h - 1, r)


class AccountButton(Button):
    """The header's account control. With no key: "Add API key". Connected: the account's
    initial on a round avatar with a green dot, and its email. A key the server refused:
    the avatar in red and what went wrong."""

    def __init__(self, parent, theme, on_click):
        super().__init__(parent, theme, "Add API key", on_click, kind="secondary", icon="key-round", small=True)
        self.state, self.initial = "none", ""

    def show_none(self):
        self.state, self.icon = "none", "key-round"
        self.SetToolTip("Add a Koppar API key")
        self.set_label("Add API key")

    def show_pending(self):
        self.state, self.icon = "pending", "user"
        self.SetToolTip("Connecting to your Koppar account")
        self.set_label("Account")

    def show_user(self, email, name=None):
        who = (name or "").strip() or (email or "").strip()
        self.initial = next((c.upper() for c in who if c.isalnum()), "?")
        self.state, self.icon = "user", None
        self.SetToolTip(f"Connected as {email or who}. Click to change the API key.")
        self.set_label(_clip_email(email or who))

    def show_error(self, label, message):
        self.state, self.icon = "error", None
        self.initial = "!"
        self.SetToolTip(message)
        self.set_label(label)

    def _fit(self):
        if getattr(self, "state", "none") not in ("user", "error"):
            return super()._fit()
        dc = wx.ClientDC(self)
        dc.SetFont(self.font)
        tw, _ = dc.GetTextExtent(self.label)
        self.SetMinSize((self.FromDIP(4 + 22 + 8 + 12) + tw, self.FromDIP(30)))

    def draw(self, gc, w, h):
        if self.state not in ("user", "error"):
            return super().draw(gc, w, h)
        t = self.theme
        r = h / 2
        # a pill: the avatar at its left end, the email after it
        gc.SetBrush(wx.Brush(t["hover"] if self.hover else t["surface"]))
        gc.SetPen(wx.Pen(t["line2"], 1))
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - 1, r)
        d = self.FromDIP(22)
        x, y = self.FromDIP(4), (h - d) / 2
        bad = self.state == "error"
        gc.SetBrush(wx.Brush(t["bad"] if bad else t["primary"]))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawEllipse(x, y, d, d)
        gc.SetFont(self.theme.display(9.5, 600), t["primary_ink"])
        iw, ih = _extent(gc, self.initial)
        gc.DrawText(self.initial, x + (d - iw) / 2, y + (d - ih) / 2)
        if not bad:
            # connected: a green dot on the avatar's edge, ringed in the pill's colour
            s = self.FromDIP(8)
            dx, dy = x + d - s + self.FromDIP(1), y + d - s + self.FromDIP(1)
            gc.SetBrush(wx.Brush(t["surface"]))
            gc.DrawEllipse(dx - 1.5, dy - 1.5, s + 3, s + 3)
            gc.SetBrush(wx.Brush(t["good"]))
            gc.DrawEllipse(dx, dy, s, s)
        gc.SetFont(self.font, t["bad"] if bad else t["ink"])
        tw, th = _extent(gc, self.label)
        gc.DrawText(self.label, x + d + self.FromDIP(8), (h - th) / 2)
        self.focus_ring(gc, 0.5, 0.5, w - 1, h - 1, r)


def _clip_email(email, most=28):
    """A long address shortened in its name part, so the domain still shows."""
    if len(email) <= most:
        return email
    name, at, domain = email.partition("@")
    if not at or len(domain) >= most - 4:
        return email[: most - 1] + "\u2026"
    return name[: most - len(domain) - 2] + "\u2026@" + domain


class IconButton(Painted):
    def __init__(self, parent, theme, icon, on_click, tooltip="", size=32, filled=False):
        super().__init__(parent, theme, focusable=True)
        self.icon, self.on_click, self.filled = icon, on_click, filled
        self.SetToolTip(tooltip)
        self.SetMinSize((self.FromDIP(size), self.FromDIP(size)))
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self.Bind(wx.EVT_LEFT_UP, lambda e: self.on_click() if self.IsEnabled() else None)
        self.Bind(wx.EVT_KEY_DOWN, lambda e: self.on_click() if e.GetKeyCode() in (wx.WXK_SPACE, wx.WXK_RETURN) else e.Skip())

    def Enable(self, enable=True):
        r = super().Enable(enable)
        self.Refresh()
        return r

    def draw(self, gc, w, h):
        t = self.theme
        enabled = self.IsEnabled()
        if self.filled:
            gc.SetBrush(wx.Brush(t["primary"] if enabled else t["line2"]))
            ink = t["primary_ink"]
        else:
            gc.SetBrush(wx.Brush(t["hover"]) if self.hover and enabled else wx.TRANSPARENT_BRUSH)
            ink = t["ink2"] if enabled else t["faint"]
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(0, 0, w, h, self.FromDIP(8))
        s = self.FromDIP(16)
        bmp = t.icon(self.icon, s, ink, self)
        if bmp.IsOk():
            gc.DrawBitmap(bmp, (w - s) / 2, (h - s) / 2, s, s)
        self.focus_ring(gc, 0, 0, w, h, self.FromDIP(8))


class Segmented(Painted):
    """One choice among a few, side by side."""

    def __init__(self, parent, theme, options, value, on_change):
        super().__init__(parent, theme, focusable=True)
        self.options = options  # [(key, label)]
        self.value = value
        self.on_change = on_change
        self.font = theme.text(9.5, 500)
        self.hot = None
        self.Bind(wx.EVT_LEFT_DOWN, self._click)
        self.Bind(wx.EVT_MOTION, self._motion)
        self.Bind(wx.EVT_KEY_DOWN, self._key)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        dc = wx.ClientDC(self)
        dc.SetFont(self.font)
        self.widths = [dc.GetTextExtent(label)[0] + self.FromDIP(28) for _, label in options]
        self.SetMinSize((sum(self.widths) + self.FromDIP(6), self.FromDIP(34)))

    def _index_at(self, x):
        acc = self.FromDIP(3)
        for i, w in enumerate(self.widths):
            if acc <= x < acc + w:
                return i
            acc += w
        return None

    def _motion(self, event):
        i = self._index_at(event.GetX())
        if i != self.hot:
            self.hot = i
            self.Refresh()

    def _click(self, event):
        pointer()
        self.SetFocus()
        i = self._index_at(event.GetX())
        if i is not None:
            self.set(self.options[i][0], notify=True)

    def _key(self, event):
        keys = [k for k, _ in self.options]
        i = keys.index(self.value)
        if event.GetKeyCode() == wx.WXK_LEFT and i > 0:
            self.set(keys[i - 1], notify=True)
        elif event.GetKeyCode() == wx.WXK_RIGHT and i < len(keys) - 1:
            self.set(keys[i + 1], notify=True)
        else:
            event.Skip()

    def set(self, value, notify=False):
        self.value = value
        self.Refresh()
        if notify and self.on_change:
            self.on_change(value)

    def draw(self, gc, w, h):
        t = self.theme
        r = self.FromDIP(9)
        gc.SetBrush(wx.Brush(t["sunken"]))
        gc.SetPen(wx.Pen(t["line"], 1))
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - 1, r)
        x = self.FromDIP(3)
        pad = self.FromDIP(3)
        for i, ((key, label), sw) in enumerate(zip(self.options, self.widths)):
            on = key == self.value
            if on:
                gc.SetBrush(wx.Brush(t["surface"]))
                gc.SetPen(wx.Pen(t["line2"], 1))
                gc.DrawRoundedRectangle(x, pad, sw, h - 2 * pad, r - 2)
            elif i == self.hot:
                gc.SetBrush(wx.Brush(t["hover"]))
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.DrawRoundedRectangle(x, pad, sw, h - 2 * pad, r - 2)
            gc.SetFont(self.theme.text(9.5, 600 if on else 500), t["ink"] if on else t["muted"])
            tw, th = _extent(gc, label)
            gc.DrawText(label, x + (sw - tw) / 2, (h - th) / 2)
            x += sw
        self.focus_ring(gc, 0.5, 0.5, w - 1, h - 1, r)


class Switch(Painted):
    """An option that is on or off, with its label beside it."""

    def __init__(self, parent, theme, label, value, on_change=None, hint=""):
        super().__init__(parent, theme, focusable=True)
        self.label, self.value, self.on_change, self.hint = label, bool(value), on_change, hint
        self.Bind(wx.EVT_LEFT_DOWN, lambda e: (pointer(), self.SetFocus(), self.toggle()))
        self.Bind(wx.EVT_KEY_DOWN, lambda e: self.toggle() if e.GetKeyCode() == wx.WXK_SPACE else e.Skip())
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        dc = wx.ClientDC(self)
        dc.SetFont(theme.text(10, 500))
        tw, _ = dc.GetTextExtent(label)
        dc.SetFont(theme.text(9))
        hw = dc.GetTextExtent(hint)[0] if hint else 0
        self.SetMinSize((self.FromDIP(46) + max(tw, hw), self.FromDIP(40 if hint else 26)))

    def GetValue(self):
        return self.value

    def SetValue(self, v):
        self.value = bool(v)
        self.Refresh()

    def toggle(self):
        self.value = not self.value
        self.Refresh()
        if self.on_change:
            self.on_change(self.value)

    def draw(self, gc, w, h):
        t = self.theme
        tw, th = self.FromDIP(34), self.FromDIP(20)
        y = self.FromDIP(3)
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(t["primary"] if self.value else t["line2"]))
        gc.DrawRoundedRectangle(0, y, tw, th, th / 2)
        knob = th - self.FromDIP(4)
        kx = tw - knob - self.FromDIP(2) if self.value else self.FromDIP(2)
        gc.SetBrush(wx.Brush(t["primary_ink"] if self.value else t["surface"]))
        gc.DrawEllipse(kx, y + self.FromDIP(2), knob, knob)
        self.focus_ring(gc, 0, y, tw, th, th / 2)
        gc.SetFont(t.text(10, 500), t["ink"])
        _, lh = _extent(gc, self.label)
        gc.DrawText(self.label, tw + self.FromDIP(12), y + (th - lh) / 2)
        if self.hint:
            gc.SetFont(t.text(9), t["muted"])
            gc.DrawText(self.hint, tw + self.FromDIP(12), y + th + self.FromDIP(1))


class MinutesStepper(Painted):
    """A time budget in minutes, with − and + on either side."""

    def __init__(self, parent, theme, value, lo, hi):
        super().__init__(parent, theme, focusable=True)
        self.value, self.lo, self.hi = value, lo, hi
        self.SetMinSize((self.FromDIP(148), self.FromDIP(34)))
        self.Bind(wx.EVT_LEFT_DOWN, self._click)
        self.Bind(wx.EVT_KEY_DOWN, self._key)

    def GetValue(self):
        return self.value

    def _bump(self, d):
        self.value = max(self.lo, min(self.hi, self.value + d))
        self.Refresh()

    def _click(self, event):
        pointer()
        self.SetFocus()
        w = self.GetClientSize().width
        if event.GetX() < self.FromDIP(34):
            self._bump(-1)
        elif event.GetX() > w - self.FromDIP(34):
            self._bump(1)

    def _key(self, event):
        k = event.GetKeyCode()
        if k in (wx.WXK_LEFT, wx.WXK_DOWN, ord("-")):
            self._bump(-1)
        elif k in (wx.WXK_RIGHT, wx.WXK_UP, ord("+"), ord("=")):
            self._bump(1)
        else:
            event.Skip()

    def draw(self, gc, w, h):
        t = self.theme
        r = self.FromDIP(9)
        gc.SetBrush(wx.Brush(t["surface"]))
        gc.SetPen(wx.Pen(t["line2"], 1))
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - 1, r)
        s = self.FromDIP(14)
        for name, x, enabled in (("minus", self.FromDIP(10), self.value > self.lo),
                                 ("plus", w - self.FromDIP(10) - s, self.value < self.hi)):
            bmp = t.icon(name, s, t["ink2"] if enabled else t["faint"], self)
            if bmp.IsOk():
                gc.DrawBitmap(bmp, x, (h - s) / 2, s, s)
        label = f"{self.value} min"
        gc.SetFont(t.text(10, 600), t["ink"])
        tw, th = _extent(gc, label)
        gc.DrawText(label, (w - tw) / 2, (h - th) / 2)
        self.focus_ring(gc, 0.5, 0.5, w - 1, h - 1, r)


class Chip(Painted):
    """A small rounded label: the runs left, an account state."""

    def __init__(self, parent, theme, text, tone="neutral", icon=None):
        super().__init__(parent, theme)
        self.text, self.tone, self.icon = text, tone, icon
        self._fit()

    def _fit(self):
        dc = wx.ClientDC(self)
        dc.SetFont(self.theme.text(9, 600))
        tw, _ = dc.GetTextExtent(self.text)
        self.SetMinSize((tw + self.FromDIP(22) + (self.FromDIP(18) if self.icon else 0), self.FromDIP(26)))

    def set(self, text, tone=None, icon=None):
        self.text = text
        self.tone = tone or self.tone
        self.icon = icon if icon is not None else self.icon
        self._fit()
        self.GetParent().Layout()
        self.Refresh()

    def draw(self, gc, w, h):
        t = self.theme
        fg = {"good": t["good"], "warn": t["warn"], "bad": t["bad"]}.get(self.tone, t["ink2"])
        bg = {"good": t["good_bg"], "warn": t["warn_bg"], "bad": t["bad_bg"]}.get(self.tone, t["sunken"])
        gc.SetBrush(wx.Brush(bg))
        gc.SetPen(wx.Pen(t["line"], 1) if self.tone == "neutral" else wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - 1, (h - 1) / 2)
        x = self.FromDIP(11)
        if self.icon:
            s = self.FromDIP(13)
            bmp = t.icon(self.icon, s, fg, self)
            if bmp.IsOk():
                gc.DrawBitmap(bmp, x, (h - s) / 2, s, s)
            x += s + self.FromDIP(5)
        gc.SetFont(t.text(9, 600), fg)
        _, th = _extent(gc, self.text)
        gc.DrawText(self.text, x, (h - th) / 2)


class Card(wx.Panel):
    """A white sheet on the paper: hairline edge, a soft shadow, children inside."""

    def __init__(self, parent, theme, pad=20):
        super().__init__(parent)
        self.theme = theme
        self.pad = self.FromDIP(pad)
        self.SetBackgroundColour(theme["surface"])
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        self.inner = wx.BoxSizer(wx.VERTICAL)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self.inner, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, self.pad)
        outer.AddSpacer(self.pad + self.FromDIP(4))
        self.SetSizer(outer)

    def _paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        gc = _gc(dc)
        w, h = self.GetClientSize()
        paint_backdrop(gc, self, w, h, self.theme)
        r = self.FromDIP(12)
        lift = self.FromDIP(4)
        if not self.theme.dark:
            # a soft fall-off under the sheet, as the app's shadow-card
            gc.SetPen(wx.TRANSPARENT_PEN)
            for i, a in ((4, 3), (3, 4), (2, 6), (1, 8)):
                gc.SetBrush(wx.Brush(wx.Colour(28, 25, 23, a)))
                gc.DrawRoundedRectangle(0, i, w, h - lift, r)
        gc.SetBrush(wx.Brush(self.theme["surface"]))
        gc.SetPen(wx.Pen(self.theme["line"], 1))
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - lift - 0.5, r)


class StatTile(Painted):
    """One number that matters: label, the number in the display face, what it was."""

    def __init__(self, parent, theme, data):
        super().__init__(parent, theme)
        self.data = data
        self.SetMinSize((self.FromDIP(120), self.FromDIP(88)))

    def draw(self, gc, w, h):
        t, d = self.theme, self.data
        gc.SetBrush(wx.Brush(t["sunken"]))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(0, 0, w, h, self.FromDIP(10))
        x = self.FromDIP(14)
        gc.SetFont(t.text(9, 500), t["muted"])
        gc.DrawText(_ellipsize(gc, d["label"], w - 2 * x), x, self.FromDIP(12))
        tone = d.get("tone")
        gc.SetFont(t.display(20, 600), t.tone(tone) if tone in ("good", "warn", "bad") else t["ink"])
        gc.DrawText(d["value"], x, self.FromDIP(30))
        if d.get("sub"):
            gc.SetFont(t.text(8.5), t["muted"])
            gc.DrawText(_ellipsize(gc, d["sub"], w - 2 * x), x, h - self.FromDIP(22))


def _ellipsize(gc, text, width):
    if _extent(gc, text)[0] <= width:
        return text
    while text and _extent(gc, text + "…")[0] > width:
        text = text[:-1]
    return text + "…"


class TaskList(wx.ScrolledWindow):
    """The sidebar: the assistant first when the account has it, then the tasks by
    group, each with its icon. Tasks that need an account carry a key."""

    def __init__(self, parent, theme, on_pick):
        super().__init__(parent, style=wx.VSCROLL | wx.BORDER_NONE)
        self.theme = theme
        self.on_pick = on_pick
        self.rows, self.current, self.hover, self.locked = [], None, None, set()
        self.enabled_ = True
        self.SetBackgroundColour(theme["surface"])
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetScrollRate(0, self.FromDIP(12))
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._click)
        self.Bind(wx.EVT_MOTION, self._motion)
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: self._hover(None))
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        self.SetMinSize((self.FromDIP(256), -1))

    def set_items(self, groups, locked):
        """groups: [(heading or None, [item])], an item has id, title, icon."""
        self.locked = locked
        self.rows = []
        y = self.FromDIP(12)
        for heading, items in groups:
            if heading:
                self.rows.append(("head", heading, y, self.FromDIP(32)))
                y += self.FromDIP(32)
            for it in items:
                self.rows.append(("item", it, y, self.FromDIP(36)))
                y += self.FromDIP(36)
            y += self.FromDIP(6)
        self.SetVirtualSize((-1, y + self.FromDIP(12)))
        self.Refresh()

    def select(self, item_id):
        self.current = item_id
        self.Refresh()

    def enable(self, on):
        self.enabled_ = on
        self.Refresh()

    def _at(self, pos):
        _, y = self.CalcUnscrolledPosition(pos)
        for kind, it, ry, rh in self.rows:
            if kind == "item" and ry <= y < ry + rh:
                return it
        return None

    def _hover(self, item):
        hid = item.id if item else None
        if hid != self.hover:
            self.hover = hid
            self.Refresh()

    def _motion(self, event):
        it = self._at(event.GetPosition()) if self.enabled_ else None
        self._hover(it)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND if it else wx.CURSOR_ARROW))
        self.SetToolTip(it.subtitle if it is not None and hasattr(it, "subtitle") else "")

    def _click(self, event):
        it = self._at(event.GetPosition())
        if it is not None and self.enabled_:
            self.select(it.id)
            self.on_pick(it)

    def _paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        self.DoPrepareDC(dc)
        gc = _gc(dc)
        t = self.theme
        w = self.GetClientSize().width
        _, top = self.CalcUnscrolledPosition(0, 0)
        gc.SetBrush(wx.Brush(t["surface"]))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRectangle(0, top, w, self.GetClientSize().height)
        gc.SetPen(wx.Pen(t["line"], 1))
        gc.StrokeLine(w - 0.5, top, w - 0.5, top + self.GetClientSize().height)
        pad = self.FromDIP(12)
        for kind, it, y, h in self.rows:
            if kind == "head":
                gc.SetFont(t.text(9, 600), t["faint"])
                gc.DrawText(it, pad + self.FromDIP(8), y + self.FromDIP(12))
                continue
            on = it.id == self.current
            if on or it.id == self.hover:
                gc.SetBrush(wx.Brush(t["selected"] if on else t["hover"]))
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.DrawRoundedRectangle(pad, y + 1, w - 2 * pad, h - 2, self.FromDIP(8))
            ink = t["ink"] if (on and self.enabled_) else (t["ink2"] if self.enabled_ else t["faint"])
            s = self.FromDIP(16)
            bmp = t.icon(it.icon, s, ink if on else t["muted"], self)
            if bmp.IsOk():
                gc.DrawBitmap(bmp, pad + self.FromDIP(10), y + (h - s) / 2, s, s)
            gc.SetFont(t.text(10, 600 if on else 500), ink)
            tw, th = _extent(gc, it.title)
            gc.DrawText(it.title, pad + self.FromDIP(36), y + (h - th) / 2)
            if it.id in self.locked:
                ks = self.FromDIP(13)
                kb = t.icon("key-round", ks, t["faint"], self)
                if kb.IsOk():
                    gc.DrawBitmap(kb, w - pad - self.FromDIP(10) - ks, y + (h - ks) / 2, ks, ks)


class TraceProgress(Painted):
    """A run, drawn as what it makes: a copper trace across the paper, one via per
    stage. Done stages are drilled and joined in copper; the current via pulses."""

    def __init__(self, parent, theme):
        super().__init__(parent, theme)
        self.stages, self.current = [], 0
        self.SetMinSize((-1, self.FromDIP(92)))
        self.started = time.time()
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self.Refresh(), self.timer)

    def set(self, stages, current):
        changed = (stages, current) != (self.stages, self.current)
        self.stages, self.current = stages, current
        if stages and not self.timer.IsRunning():
            self.timer.Start(50)
        if changed:
            self.Refresh()

    def stop(self):
        self.timer.Stop()

    def draw(self, gc, w, h):
        t = self.theme
        if not self.stages:
            return
        n = len(self.stages)
        r = self.FromDIP(8)
        x0, x1 = self.FromDIP(18), w - self.FromDIP(18)
        cy = self.FromDIP(26)
        xs = [x0 + (x1 - x0) * i / max(1, n - 1) for i in range(n)]
        width = self.FromDIP(5)
        # the trace still to be laid: a ghost of where it goes
        gc.SetPen(wx.Pen(t["line2"], width, wx.PENSTYLE_SOLID))
        gc.StrokeLine(xs[0], cy, xs[-1], cy)
        # what is laid: copper, up to the via being worked on, growing into it
        phase = (time.time() - self.started) % 1.6 / 1.6
        if self.current > 0 or n == 1:
            reach = xs[min(self.current, n - 1)]
            pen = wx.Pen(t["copper"], width)
            pen.SetCap(wx.CAP_ROUND)
            gc.SetPen(pen)
            gc.StrokeLine(xs[0], cy, reach, cy)
        for i, (x, label) in enumerate(zip(xs, self.stages)):
            done, now = i < self.current, i == self.current
            if now:
                pulse = 0.5 + 0.5 * math.sin(phase * 2 * math.pi)
                halo = r + self.FromDIP(4) + pulse * self.FromDIP(5)
                c = t["copper"]
                gc.SetBrush(wx.Brush(wx.Colour(c.Red(), c.Green(), c.Blue(), int(40 + 50 * (1 - pulse)))))
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.DrawEllipse(x - halo, cy - halo, 2 * halo, 2 * halo)
            # the pad
            gc.SetBrush(wx.Brush(t["copper"] if (done or now) else t["surface"]))
            gc.SetPen(wx.Pen(t["copper_deep"] if (done or now) else t["line2"], max(1, self.FromDIP(3) // 2)))
            gc.DrawEllipse(x - r, cy - r, 2 * r, 2 * r)
            # the drill: a done via is drilled through
            if done or now:
                d = r * (0.45 if done else 0.3)
                gc.SetBrush(wx.Brush(t["surface"]))
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.DrawEllipse(x - d, cy - d, 2 * d, 2 * d)
            gc.SetFont(t.text(9, 600 if now else 500), t["ink"] if (done or now) else t["faint"])
            tw, th = _extent(gc, label)
            lx = min(max(0, x - tw / 2), w - tw)
            gc.DrawText(label, lx, cy + r + self.FromDIP(12))


class RichView(wx.Control):
    """Text with structure: headings, paragraphs, lists, two-column rows, code,
    and spans that are bold, monospace (nets, references) or links. One renderer
    for the assistant's replies and the results' details."""

    def __init__(self, parent, theme, on_link=None, size=10):
        super().__init__(parent, style=wx.BORDER_NONE)
        self.theme = theme
        self.on_link = on_link
        self.size = size
        self.blocks = []
        self.ops = []
        self.links = []
        self.hover_link = None
        self.laid_width = -1
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, self._size)
        self.Bind(wx.EVT_MOTION, self._motion)
        self.Bind(wx.EVT_LEFT_UP, self._click)

    def set_blocks(self, blocks):
        self.blocks = blocks
        self.laid_width = -1
        self._layout(self.GetClientSize().width or self.FromDIP(520))
        self.Refresh()

    def _size(self, event):
        w = self.GetClientSize().width
        if w > 0 and w != self.laid_width:
            self._layout(w)
            self.Refresh()
        event.Skip()

    def _fonts(self, style):
        t, s = self.theme, self.size
        return {
            "normal": t.text(s), "bold": t.text(s, 600), "muted": t.text(s), "link": t.text(s, 500),
            "code": t.mono(s - 0.5), "h": t.text(s + 1, 600), "small": t.text(s - 1),
        }.get(style, t.text(s))

    def _colour(self, style):
        t = self.theme
        return {"muted": t["muted"], "link": t["link"], "small": t["muted"]}.get(style, t["ink"])

    def _layout(self, width):
        """Lay the blocks out at `width`: draw ops, link boxes, and the height they need."""
        self.laid_width = width
        dc = wx.ClientDC(self)
        gc = _gc(dc)
        ops, links = [], []
        y = 0
        line_gap = self.FromDIP(5)

        def flow(spans, x0, y, max_w, base_style="normal"):
            """Words of the spans wrapped into lines from x0; returns the next y."""
            x = x0
            line_h = 0
            line = []

            def flush(y):
                nonlocal line, line_h
                for op in line:
                    op["y"] = y + (line_h - op["h"]) * 0.8
                ops.extend(line)
                for op in line:
                    if op.get("link"):
                        links.append((op["x"], op["y"], op["w"], op["h"], op["link"]))
                y += line_h + line_gap if line else 0
                line, line_h = [], 0
                return y

            for text, style, link in spans:
                style = style if style != "normal" else base_style
                gc.SetFont(self._fonts(style), self._colour(style))
                words = text.replace("\n", " \n ").split(" ")
                for i, word in enumerate(words):
                    if word == "\n":
                        y = flush(y)
                        x = x0
                        continue
                    piece = word + (" " if i < len(words) - 1 else "")
                    if not piece:
                        continue
                    pw, ph = _extent(gc, piece)
                    if x + pw > x0 + max_w and x > x0:
                        y = flush(y)
                        x = x0
                        piece = piece.lstrip()
                        pw, ph = _extent(gc, piece)
                    line.append({"kind": "text", "text": piece, "style": style, "x": x, "w": pw, "h": ph, "link": link})
                    line_h = max(line_h, ph)
                    x += pw
            return flush(y)

        for b in self.blocks:
            kind = b["kind"]
            if kind == "gap":
                y += self.FromDIP(b.get("size", 8))
            elif kind == "h":
                y += self.FromDIP(6) if y else 0
                y = flow(b["spans"], 0, y, width, "h")
                y += self.FromDIP(2)
            elif kind == "p":
                y = flow(b["spans"], 0, y, width, b.get("style", "normal"))
                y += self.FromDIP(4)
            elif kind == "li":
                indent = self.FromDIP(18) + self.FromDIP(16) * b.get("level", 0)
                ops.append({"kind": "bullet", "x": indent - self.FromDIP(11), "y": y, "ordinal": b.get("ordinal")})
                y = flow(b["spans"], indent, y, width - indent, b.get("style", "normal"))
            elif kind == "row":
                col = min(self.FromDIP(220), width * 0.45)
                y0 = y
                ya = flow(b["cols"][0], 0, y0, col - self.FromDIP(12), "muted")
                yb = flow(b["cols"][1], col, y0, width - col, "normal")
                y = max(ya, yb)
                ops.append({"kind": "rule", "y": y - line_gap / 2})
            elif kind == "code":
                gc.SetFont(self._fonts("code"), self.theme["ink"])
                cw, lh = _extent(gc, "M")
                per_line = max(20, int((width - self.FromDIP(24)) / max(1, cw)))
                lines = []
                for raw in b["text"].rstrip("\n").split("\n"):
                    while len(raw) > per_line:
                        lines.append(raw[:per_line])
                        raw = "  " + raw[per_line:]
                    lines.append(raw)
                h = len(lines) * (lh + 2) + self.FromDIP(16)
                ops.append({"kind": "codebox", "y": y, "h": h, "lines": lines, "lh": lh + 2})
                y += h + self.FromDIP(6)
        self.ops, self.links = ops, links
        height = int(y) + self.FromDIP(2)
        self.SetMinSize((-1, height))
        if self.GetClientSize().height != height:
            self.SetSize((width, height))
            p = self.GetParent()
            while p is not None and not isinstance(p, wx.TopLevelWindow):
                p.Layout()
                if isinstance(p, wx.ScrolledWindow):
                    p.FitInside()
                    break
                p = p.GetParent()

    def _paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        gc = _gc(dc)
        w, h = self.GetClientSize()
        gc.SetBrush(wx.Brush(self.GetParent().GetBackgroundColour()))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRectangle(0, 0, w, h)
        t = self.theme
        for op in self.ops:
            k = op["kind"]
            if k == "text":
                style = op["style"]
                if style == "code":
                    gc.SetBrush(wx.Brush(t["sunken"]))
                    gc.SetPen(wx.TRANSPARENT_PEN)
                    gc.DrawRoundedRectangle(op["x"] - 2, op["y"] - 1, op["w"] - self.FromDIP(3) + 4, op["h"] + 2, 4)
                gc.SetFont(self._fonts(style), self._colour(style))
                gc.DrawText(op["text"], op["x"], op["y"])
                if op.get("link") and op["link"] == self.hover_link:
                    gc.SetPen(wx.Pen(t["link"], 1))
                    gc.StrokeLine(op["x"], op["y"] + op["h"], op["x"] + op["w"] - self.FromDIP(3), op["y"] + op["h"])
            elif k == "bullet":
                if op.get("ordinal"):
                    gc.SetFont(self._fonts("muted"), t["muted"])
                    gc.DrawText(f"{op['ordinal']}.", op["x"] - self.FromDIP(4), op["y"])
                else:
                    r = self.FromDIP(2)
                    gc.SetBrush(wx.Brush(t["faint"]))
                    gc.SetPen(wx.TRANSPARENT_PEN)
                    gc.DrawEllipse(op["x"], op["y"] + self.FromDIP(7), 2 * r, 2 * r)
            elif k == "rule":
                gc.SetPen(wx.Pen(t["line"], 1))
                gc.StrokeLine(0, op["y"], w, op["y"])
            elif k == "codebox":
                gc.SetBrush(wx.Brush(t["sunken"]))
                gc.SetPen(wx.Pen(t["line"], 1))
                gc.DrawRoundedRectangle(0.5, op["y"] + 0.5, w - 1, op["h"] - 1, self.FromDIP(8))
                gc.SetFont(self._fonts("code"), t["ink"])
                for i, line in enumerate(op["lines"]):
                    gc.DrawText(line, self.FromDIP(12), op["y"] + self.FromDIP(8) + i * op["lh"])

    def _link_at(self, pos):
        for x, y, w, h, link in self.links:
            if x <= pos.x <= x + w and y <= pos.y <= y + h:
                return link
        return None

    def _motion(self, event):
        link = self._link_at(event.GetPosition())
        if link != self.hover_link:
            self.hover_link = link
            self.SetCursor(wx.Cursor(wx.CURSOR_HAND if link else wx.CURSOR_ARROW))
            self.Refresh()

    def _click(self, event):
        link = self._link_at(event.GetPosition())
        if link and self.on_link:
            self.on_link(link)


class Composer(wx.Panel):
    """The message box: a rounded field and a send button. Enter sends."""

    def __init__(self, parent, theme, on_send, placeholder):
        super().__init__(parent)
        self.theme = theme
        self.on_send = on_send
        self.SetBackgroundColour(theme["surface"])
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        row = wx.BoxSizer(wx.HORIZONTAL)
        # single-line: GTK shows no placeholder in a multi-line field, and a message is a line or two
        self.field = wx.TextCtrl(self, style=wx.BORDER_NONE | wx.TE_PROCESS_ENTER, size=(-1, self.FromDIP(30)))
        self.field.SetHint(placeholder)
        self.field.SetFont(theme.text(10.5))
        self.field.SetBackgroundColour(theme["surface"])
        self.field.SetForegroundColour(theme["ink"])
        self.field.Bind(wx.EVT_KEY_DOWN, self._key)
        self.field.Bind(wx.EVT_SET_FOCUS, lambda e: (self.Refresh(), e.Skip()))
        self.field.Bind(wx.EVT_KILL_FOCUS, lambda e: (self.Refresh(), e.Skip()))
        row.Add(self.field, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, self.FromDIP(16))
        self.send = IconButton(self, theme, "arrow-up", self._send, "Send (Enter)", size=34, filled=True)
        row.Add(self.send, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, self.FromDIP(8))
        self.SetSizer(row)

    def _key(self, event):
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._send()
        else:
            event.Skip()

    def _send(self):
        text = self.field.GetValue().strip()
        if text and self.send.IsEnabled():
            self.on_send(text)

    def set_busy(self, busy):
        self.send.Enable(not busy)

    def _paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        gc = _gc(dc)
        w, h = self.GetClientSize()
        paint_backdrop(gc, self, w, h, self.theme)
        focused = self.field.HasFocus()
        gc.SetBrush(wx.Brush(self.theme["surface"]))
        gc.SetPen(wx.Pen(self.theme["focus"] if focused else self.theme["line2"], max(1, self.FromDIP(3) // 2) if focused else 1))
        gc.DrawRoundedRectangle(1, 1, w - 2, h - 2, self.FromDIP(14))


def mono_spans(text):
    """A line where nets and references read as data."""
    return rich.inline(text)
