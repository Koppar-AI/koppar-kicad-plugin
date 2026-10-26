"""Koppar's look in wx: tokens, the brand's typefaces, lucide icons, the paper.

The tokens are the Koppar app's (stone neutrals on a dotted engineering paper,
white surfaces, near-black primary actions, blue for links and focus) with a
dark set beside them, since KiCad runs dark as
often as light. Copper (the mark's) is kept for the one place a board is being
drawn: the run's trace.
"""

import os
import re
import sys

import wx

try:
    import wx.svg as wxsvg
except ImportError:  # wxPython before 4.1: icons fall back to nothing
    wxsvg = None

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")

DISPLAY, TEXT, MONO = "Bricolage Grotesque", "Hanken Grotesk", "JetBrains Mono"

LIGHT = {
    "paper": "#F7F6F1", "dot": "#E9E7E0", "surface": "#FFFFFF", "sunken": "#F5F5F4", "hover": "#F1F0EC",
    "selected": "#ECEAE4", "ink": "#1C1917", "ink2": "#44403C", "muted": "#78716C", "faint": "#A8A29E",
    "line": "#E7E5E4", "line2": "#D6D3D1", "link": "#2563EB", "focus": "#93B4F5",
    "good": "#047857", "good_bg": "#ECFDF5", "warn": "#B45309", "warn_bg": "#FFFBEB",
    "bad": "#E11D48", "bad_bg": "#FFF1F2", "primary": "#1C1917", "primary_ink": "#FAFAF9",
    "copper": "#E0955A", "copper_deep": "#B8743F", "board": "#0B2F28",
}
DARK = {
    "paper": "#171514", "dot": "#24211F", "surface": "#1F1C1A", "sunken": "#262321", "hover": "#2A2725",
    "selected": "#322E2B", "ink": "#F5F5F4", "ink2": "#D6D3D1", "muted": "#A8A29E", "faint": "#78716C",
    "line": "#34302D", "line2": "#44403C", "link": "#7AA7FF", "focus": "#3B5EA8",
    "good": "#34D399", "good_bg": "#0F2A22", "warn": "#FBBF24", "warn_bg": "#2B2313",
    "bad": "#FB7185", "bad_bg": "#2E1519", "primary": "#F5F5F4", "primary_ink": "#1C1917",
    "copper": "#E0955A", "copper_deep": "#C9834E", "board": "#0B2F28",
}

_fonts_registered = None


def register_fonts():
    """Register the bundled cuts once per process; True when the brand faces are usable."""
    global _fonts_registered
    if _fonts_registered is not None:
        return _fonts_registered
    ok = True
    folder = os.path.join(ASSETS, "fonts")
    # wxOSX takes private fonts only from the app bundle's Resources/Fonts, which a plugin
    # cannot write to: on macOS the fonts are registered for this process with CoreText
    add = _coretext_register if sys.platform == "darwin" else wx.Font.AddPrivateFont
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if name.endswith(".ttf"):
            try:
                ok = add(os.path.join(folder, name)) and ok
            except Exception:
                ok = False
    _fonts_registered = ok
    return ok


def _coretext_register(path):
    """Make a font file usable by this process (macOS), wherever it is."""
    import ctypes

    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ct = ctypes.CDLL("/System/Library/Frameworks/CoreText.framework/CoreText")
    cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
    cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    ct.CTFontManagerRegisterFontsForURL.restype = ctypes.c_bool
    ct.CTFontManagerRegisterFontsForURL.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    cf.CFErrorGetCode.restype = ctypes.c_long
    cf.CFErrorGetCode.argtypes = [ctypes.c_void_p]
    raw = os.fsencode(path)
    url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False)
    if not url:
        return False
    error = ctypes.c_void_p()
    try:
        # kCTFontManagerScopeProcess: for KiCad while it runs, nothing installed on the system
        if ct.CTFontManagerRegisterFontsForURL(url, 1, ctypes.byref(error)):
            return True
        # kCTFontManagerErrorAlreadyRegistered: the plugin was loaded again in this KiCad
        return bool(error.value) and cf.CFErrorGetCode(error.value) == 105
    finally:
        if error.value:
            cf.CFRelease(error.value)
        cf.CFRelease(url)


class Theme:
    def __init__(self):
        register_fonts()
        try:
            self.dark = wx.SystemSettings.GetAppearance().IsDark()
        except AttributeError:
            self.dark = False
        self.hex = DARK if self.dark else LIGHT
        self.c = {k: wx.Colour(v) for k, v in self.hex.items()}
        self._fonts = {}
        self._icons = {}

    def __getitem__(self, key):
        return self.c[key]

    def tone(self, name):
        return self.c.get({"good": "good", "warn": "warn", "bad": "bad"}.get(name, "ink"), self.c["ink"])

    # -- type ----------------------------------------------------------------

    def font(self, family=TEXT, size=10, weight=400):
        key = (family, size, weight)
        if key not in self._fonts:
            f = wx.Font(wx.FontInfo(size).FaceName(family))
            if not f.IsOk() or f.GetFaceName() != family:
                f = wx.Font(wx.FontInfo(size).Family(wx.FONTFAMILY_TELETYPE if family == MONO else wx.FONTFAMILY_SWISS))
            try:
                f.SetNumericWeight(weight)
            except AttributeError:
                f.SetWeight(wx.FONTWEIGHT_BOLD if weight >= 600 else wx.FONTWEIGHT_NORMAL)
            self._fonts[key] = f
        return self._fonts[key]

    def text(self, size=10, weight=400):
        return self.font(TEXT, size, weight)

    def display(self, size=16, weight=600):
        return self.font(DISPLAY, size, weight)

    def mono(self, size=9.5, weight=400):
        return self.font(MONO, size, weight)

    # -- icons ---------------------------------------------------------------

    def icon(self, name, px, colour, win):
        """A lucide icon drawn in `colour`, crisp at the window's scale."""
        hexc = colour if isinstance(colour, str) else colour.GetAsString(wx.C2S_HTML_SYNTAX)
        key = (name, px, hexc, win.GetContentScaleFactor())
        if key in self._icons:
            return self._icons[key]
        bmp = wx.NullBitmap
        path = os.path.join(ASSETS, "icons", f"{name}.svg")
        if wxsvg is not None and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                svg = f.read().replace("currentColor", hexc)
            img = wxsvg.SVGimage.CreateFromBytes(svg.encode("utf-8"))
            bmp = img.ConvertToScaledBitmap(wx.Size(px, px), win)
        self._icons[key] = bmp
        return bmp

    def wordmark(self, height, win):
        """The Koppar wordmark at `height` px, without the backdrop the brand file carries."""
        key = ("wordmark", height, win.GetContentScaleFactor())
        if key in self._icons:
            return self._icons[key]
        bmp = wx.NullBitmap
        path = os.path.join(ASSETS, "wordmark-dark.svg" if self.dark else "wordmark-light.svg")
        if wxsvg is not None and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                svg = f.read()
            svg = re.sub(r'<rect width="[\d.]+" height="[\d.]+" fill="#[0-9a-fA-F]{6}"/>', "", svg, count=1)
            img = wxsvg.SVGimage.CreateFromBytes(svg.encode("utf-8"))
            width = int(round(height * img.width / img.height))
            bmp = img.ConvertToScaledBitmap(wx.Size(width, height), win)
        self._icons[key] = bmp
        return bmp


def paint_paper(gc, w, h, theme, win, origin=(0, 0)):
    """The app's dotted engineering paper: a dot every 22 px."""
    gc.SetBrush(wx.Brush(theme["paper"]))
    gc.SetPen(wx.TRANSPARENT_PEN)
    gc.DrawRectangle(0, 0, w, h)
    step = win.FromDIP(22)
    r = max(1.0, win.FromDIP(10) / 10)
    gc.SetBrush(wx.Brush(theme["dot"]))
    ox, oy = origin
    y = step / 2 - (oy % step)
    while y < h:
        x = step / 2 - (ox % step)
        while x < w:
            gc.DrawEllipse(x - r, y - r, 2 * r, 2 * r)
            x += step
        y += step
