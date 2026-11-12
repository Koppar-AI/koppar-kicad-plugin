"""Screenshots of the Koppar window's pages, inside the real KiCad PCB editor.

    KOPPAR_SMOKE_DRIVER=scripts/gui_tour.py KOPPAR_SMOKE_FRAME=1920x1200 \
        scripts/gui_smoke.sh board.kicad_pcb [server] [out-dir]

KOPPAR_SMOKE_FRAME keeps the editor inside the virtual screen: a dialog centred outside it reads black.

gui_smoke.sh runs it in a throwaway KiCad config on a private X display. It opens the window and
visits each page without sending anything that changes a board: the task pages, the board check
before a route (KiCad's DRC through kicad-cli, on this machine), a failed run with its Feedback
box, the account dialog, and the header with an update on offer. Each page is written to
$KOPPAR_SMOKE_OUT/<n>-<name>.png. With KOPPAR_SMOKE_API_KEY the account's tasks are shown too
(their pages are read from the server; nothing runs). KOPPAR_SMOKE_TOUR=a,b limits the pages.
"""

import os
import struct
import subprocess
import sys
import time
import traceback

import wx

OUT = os.environ.get("KOPPAR_SMOKE_OUT", "/tmp/koppar-tour")
ONLY = [p for p in os.environ.get("KOPPAR_SMOKE_TOUR", "").split(",") if p]
TASK_PAGES = ["autoroute", "route_nets", "ripup", "auto_place", "anneal", "polish_vias", "physics"]
t0 = time.time()


def log(*a):
    print("[tour]", round(time.time() - t0, 1), *a, flush=True)


def mod(suffix):
    for name, m in list(sys.modules.items()):
        if name.endswith("." + suffix) and "koppar" in (getattr(m, "__file__", "") or "") + name:
            return m
    raise RuntimeError(f"no {suffix} module")


def top(kind):
    for w in wx.GetTopLevelWindows():
        if type(w).__name__ == kind and w.IsShown():
            return w
    return None


def shot(name, win):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{len([f for f in os.listdir(OUT) if not f.endswith('.editor.png')]):02d}-{name}.png")
    capture(win, path)
    if type(win).__name__ == "KopparWindow":
        editor_too(path)
    log("shot", path)


def capture(win, path):
    """A window's pixels, read by its X id (a frame reads black from the root window when there is
    no window manager); the root window when `win` is None."""
    from PIL import Image

    target = ["-id", str(win.GetHandle())] if win is not None else ["-root"]
    raw = subprocess.run(["xwd", *target, "-silent"], capture_output=True, timeout=20).stdout
    h = struct.unpack(">25I", raw[:100])
    width, height, bpl, ncolors = h[4], h[5], h[12], h[19]
    off = h[0] + ncolors * 12
    img = Image.frombuffer("RGBX", (width, height), raw[off:off + bpl * height], "raw", "BGRX", bpl, 1)
    img.convert("RGB").save(path)


def editor_too(path):
    """With KOPPAR_SMOKE_COMPOSE, the PCB editor beside each shot (<shot>.editor.png), for
    scripts/compose_shots.py."""
    if os.environ.get("KOPPAR_SMOKE_COMPOSE"):
        frame = mod("ui").pcb_frame()
        if frame is not None:
            capture(frame, path[:-4] + ".editor.png")


def identify_and_size(w):
    """KOPPAR_SMOKE_CLIENT on every request (the server's request log keeps who asked), and the
    window at KOPPAR_SMOKE_WINDOW (e.g. 1040x900) for screenshots."""
    client = os.environ.get("KOPPAR_SMOKE_CLIENT")
    api = mod("api")
    if client and not getattr(api, "_smoke_client", None):
        orig = api.urllib.request.Request

        def request(url, data=None, headers=None, method=None):
            return orig(url, data=data, headers={**(headers or {}), "X-Koppar-Client": client}, method=method)
        api.urllib.request.Request = request
        api._smoke_client = client
    size = os.environ.get("KOPPAR_SMOKE_WINDOW")
    if size and w is not None:
        width, height = (int(v) for v in size.split("x"))
        w.SetSize(w.FromDIP(wx.Size(width, height)))
        w.Layout()
    at = os.environ.get("KOPPAR_SMOKE_WINDOW_AT")
    if at and w is not None:
        # beside the editor, not over it: X keeps no pixels of a covered window without a window manager
        w.Move(*(int(v) for v in at.split(",")))
    if os.environ.get("KOPPAR_SMOKE_COMPOSE") and not getattr(identify_and_size, "fitted", False):
        # the whole board in view, once the editor's canvas is up (it is by the time the window opens)
        print("[shots] zoom to fit:", menu_click("Zoom to Fit", True), flush=True)
        if w is not None:
            # one step out, so the board clears the window that sits over the editor's right side
            wx.CallLater(800, lambda: print("[shots] zoom out:", menu_click("Zoom Out", True), flush=True))
            identify_and_size.fitted = True


def menu_click(label, to_menu=False):
    """Post a menu entry's event: action plugins are bound on the frame, KiCad's own actions on
    the menu that holds them."""
    ui = mod("ui")
    frame = ui.pcb_frame()
    bar = frame.GetMenuBar()
    for i in range(bar.GetMenuCount()):
        item = ui._find_menu_item(bar.GetMenu(i), label)
        if item is not None:
            target = item.GetMenu() if to_menu else frame
            evt = wx.CommandEvent(wx.wxEVT_COMMAND_MENU_SELECTED, item.GetId())
            evt.SetEventObject(target)
            wx.PostEvent(target, evt)
            return True
    return False


def wanted(page):
    return not ONLY or page in ONLY


def modal(open_it, kind, name):
    """Open a dialog, photograph it once laid out, and close it."""
    def take():
        d = top(kind)
        if d is None:
            log("no", kind)
            return
        d.Refresh()
        d.Update()
        wx.CallLater(1500, lambda: (shot(name, d), d.EndModal(wx.ID_CANCEL)))
    wx.CallLater(3000, take)
    open_it()


def tour():
    """The pages, one after the other: (seconds to wait first, what to do)."""
    w = top("KopparWindow")
    catalog, api = mod("catalog"), mod("api")
    # the tour never runs a task: past the board check, nothing is sent
    w._dispatch = lambda *a, **k: log("a run would start here; the tour stops it")
    steps = []
    for tid in TASK_PAGES:
        if wanted(tid):
            steps.append((2, lambda tid=tid: (w._pick(catalog.task(tid)), wx.CallLater(1500, lambda: shot(f"task-{tid}", w)))))
    if wanted("update"):
        def offer():
            w.update_offer = {"version": "0.1.3", "url": "https://example.invalid/koppar.zip", "sha256": "0" * 64}
            w._show_update()
            w._pick(catalog.task("autoroute"))
            wx.CallLater(1500, lambda: shot("header-update", w))
        steps.append((3, offer))
    if wanted("check"):
        def check():
            shown = w._show_check

            def seen(*a, **k):
                shown(*a, **k)
                wx.CallLater(1500, lambda: shot("board-check", w))
            w._show_check = seen
            w._pick(catalog.task("autoroute"))
            w._run(catalog.task("autoroute"))
        steps.append((3, check))
    if wanted("error"):
        def failed():
            err = api.ApiError(0, "KeyError: 'nets'", {"traceback": 'Traceback (most recent call last):\n  File "~/koppar/ui.py", line 1, in work\nKeyError: \'nets\'\n'})
            w._show_error(catalog.task("autoroute"), err)
            wx.CallLater(1500, lambda: shot("run-failed", w))
        steps.append((25, failed))
        steps.append((4, lambda: modal(lambda: w._on_feedback(include=True), "FeedbackDialog", "feedback")))
    if wanted("account"):
        steps.append((5, lambda: modal(w._on_account, "AccountDialog", "account")))
    steps.append((5, lambda: os._exit(0)))

    def run(i):
        wait, do = steps[i]

        def go():
            try:
                do()
            except Exception:
                log("step failed", traceback.format_exc())
            if i + 1 < len(steps):
                run(i + 1)
        wx.CallLater(int(wait * 1000), go)
    run(0)


def start(tries=[0]):
    tries[0] += 1
    try:
        ready = mod("ui").pcb_frame() is not None and menu_click("Koppar")
    except Exception:
        ready = False
    if not ready:
        if tries[0] > 60:
            log("the Koppar menu entry never appeared")
            os._exit(1)
        wx.CallLater(1000, start)
        return
    identify_and_size(None)

    def opened(waited=[0]):
        w = top("KopparWindow")
        account = bool(os.environ.get("KOPPAR_SMOKE_API_KEY"))
        if w is None or (account and w.listing is None and waited[0] < 30):
            waited[0] += 1
            wx.CallLater(1000, opened)
            return
        identify_and_size(w)
        tour()
    wx.CallLater(1000, opened)


wx.CallLater(3000, start)
