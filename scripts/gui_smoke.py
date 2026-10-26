"""Drive the plugin inside the real KiCad PCB editor: a task, Apply, Undo.

    scripts/gui_smoke.sh board.kicad_pcb [server]

gui_smoke.sh installs this file beside the plugin in a throwaway KiCad
config (never the user's) and starts pcbnew on a private X display. Loaded as
an action plugin, it drives the Koppar window on a timer the way a person
would: opens it from its menu entry, picks the task (KOPPAR_SMOKE_TASK, default
autoroute), sets the net scope (KOPPAR_SMOKE_SCOPE: all, open, pick with
KOPPAR_SMOKE_NETS) and any server option (KOPPAR_SMOKE_ARGS, JSON, typed into
All options), runs it, waits for the result, presses Apply, then Edit > Undo.
With KOPPAR_SMOKE_REOPEN=1 it closes the window once the result is in and
reopens it from the button first. KOPPAR_SMOKE_TASK=chat sends
KOPPAR_SMOKE_MESSAGE to Ask Koppar instead; when the reply starts a new
design, it waits for the design, opens it into KOPPAR_SMOKE_DESIGN_DIR (or
$KOPPAR_SMOKE_OUT/designs) and checks KiCad was started on it. Writes what it saw to
$KOPPAR_SMOKE_OUT/report.json with screenshots, then quits KiCad.
"""

import json
import os
import struct
import subprocess
import sys
import time
import traceback
import webbrowser

import pcbnew
import wx

OUT = os.environ.get("KOPPAR_SMOKE_OUT", "/tmp/koppar-smoke")
DEADLINE_S = float(os.environ.get("KOPPAR_SMOKE_DEADLINE_S", "900"))
TASK = os.environ.get("KOPPAR_SMOKE_TASK", "autoroute")
SCOPE = os.environ.get("KOPPAR_SMOKE_SCOPE")
NETS = [n for n in os.environ.get("KOPPAR_SMOKE_NETS", "").split(",") if n]
ARGS = json.loads(os.environ.get("KOPPAR_SMOKE_ARGS") or "{}")
REOPEN = bool(os.environ.get("KOPPAR_SMOKE_REOPEN"))
# KOPPAR_SMOKE_TASK=chat: send this message to the assistant
MESSAGE = os.environ.get("KOPPAR_SMOKE_MESSAGE", "Which nets are still open, and why?")
DESIGN_DIR = os.environ.get("KOPPAR_SMOKE_DESIGN_DIR") or os.path.join(OUT, "designs")
report = {"task": TASK, "steps": [], "ok": False}
t0 = time.time()


def log(step, **kw):
    entry = {"t": round(time.time() - t0, 1), "step": step, **kw}
    report["steps"].append(entry)
    print("[smoke]", json.dumps(entry), flush=True)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "report.json"), "w") as f:
        json.dump(report, f, indent=2)


def plugin_module(suffix):
    """A module of the plugin, whatever folder it was installed as."""
    for name, mod in list(sys.modules.items()):
        if name.endswith("." + suffix) and hasattr(mod, "__file__") and "koppar" in (mod.__file__ or "") + name:
            return mod
    raise RuntimeError(f"the Koppar plugin's {suffix} module is not loaded")


def window():
    for w in wx.GetTopLevelWindows():
        if type(w).__name__ == "KopparWindow" and w.IsShown():
            return w
    return None


def shot(name):
    """The Koppar window, read by its X id (a frame reads black from the root
    window when there is no window manager)."""
    try:
        from PIL import Image

        w = window()
        target = ["-id", str(w.GetHandle())] if w else ["-root"]
        raw = subprocess.run(["xwd", *target, "-silent"], capture_output=True, timeout=20).stdout
        h = struct.unpack(">25I", raw[:100])
        width, height, bpl, ncolors = h[4], h[5], h[12], h[19]
        off = h[0] + ncolors * 12
        img = Image.frombuffer("RGBX", (width, height), raw[off:off + bpl * height], "raw", "BGRX", bpl, 1)
        img.convert("RGB").save(os.path.join(OUT, f"{name}.png"))
    except Exception as e:
        log("screenshot failed", name=name, error=str(e))


def board_state():
    """What the board holds: copper counts and a fingerprint of the placement."""
    b = pcbnew.GetBoard()
    tracks = list(b.GetTracks())
    places = sorted((f.GetReference(), f.GetPosition().x, f.GetPosition().y, round(f.GetOrientationDegrees(), 2), f.GetLayer())
                    for f in b.GetFootprints())
    return {"tracks": sum(1 for t in tracks if t.GetClass() != "PCB_VIA"),
            "vias": sum(1 for t in tracks if t.GetClass() == "PCB_VIA"),
            "zones": len(list(b.Zones())),
            "placement": hash(tuple(places)) & 0xFFFFFFFF}


def menu_item(label):
    ui = plugin_module("ui")
    bar = ui.pcb_frame().GetMenuBar()
    for i in range(bar.GetMenuCount()):
        item = ui._find_menu_item(bar.GetMenu(i), label)
        if item is not None:
            return item
    return None


def click_menu(label, to_menu=False):
    """Post the menu event the way a click does. Action plugin entries are bound
    on the frame; KiCad's own actions (Undo) on the menu that holds them."""
    frame = plugin_module("ui").pcb_frame()
    item = menu_item(label)
    if item is None:
        return False
    target = item.GetMenu() if to_menu else frame
    evt = wx.CommandEvent(wx.wxEVT_COMMAND_MENU_SELECTED, item.GetId())
    evt.SetEventObject(target)
    wx.PostEvent(target, evt)
    return True


messages = []


def fake_message_box(message, caption="", style=wx.OK, parent=None, *args):
    """wx.MessageBox, answered the way a person would (Yes / OK), and recorded."""
    messages.append(message)
    log("message box", caption=caption, message=message)
    return wx.YES if style & wx.YES_NO else wx.OK


wx.MessageBox = fake_message_box
webbrowser.open = lambda url, *a, **k: log("browser", url=url) or True

state = {"phase": "start", "since": time.time()}


def phase(name):
    state["phase"] = name
    state["since"] = time.time()


def waited(s):
    return time.time() - state["since"] > s


def fill_advanced(w, args):
    """Type server options into All options, as a person would."""
    if not args:
        return
    if w.advanced is None:
        raise RuntimeError("this task has no All options to type into")
    for name, prop, kind, widget in w.advanced.fields:
        if name not in args:
            continue
        value = args[name]
        if kind == "boolean":
            widget.SetSelection(1 if value else 2)
        elif kind == "enum":
            widget.SetSelection([str(v) for v in prop["enum"]].index(str(value)) + 1)
        elif kind == "strings":
            widget.SetValue(", ".join(value))
        elif kind == "json":
            widget.SetValue(json.dumps(value))
        else:
            widget.SetValue(str(value))


def tick():
    try:
        step()
    except Exception:
        log("driver error", error=traceback.format_exc())
        finish()
        return
    if state["phase"] != "done":
        wx.CallLater(1000, tick)


def step():
    p = state["phase"]
    if time.time() - t0 > DEADLINE_S:
        log("deadline", phase=p)
        finish()
        return
    if p == "start":
        try:
            found = menu_item("Koppar")
        except RuntimeError:
            found = None
        if found is None:
            if waited(30):
                log("plugin menu entry missing")
                finish()
            return
        report["before"] = board_state()
        log("plugin loaded", kicad=pcbnew.Version(), before=report["before"])
        click_menu("Koppar")
        phase("window")
    elif p == "window":
        w = window()
        if w is None:
            if waited(30):
                log("window never opened")
                finish()
            return
        if TASK == "chat":
            if not w._chat_available():
                if waited(30):
                    log("chat never offered", account=w.account_info, listing=w.listing is not None)
                    finish()
                return
            log("window", runs=w.runs_chip.text, nav=[r[1].id for r in w.nav.rows if r[0] == "item"][:3])
            shot("0-window")
            w._pick(plugin_module("ui").CHAT)
            phase("chat-open")
            return
        task = plugin_module("catalog").task(TASK)
        if task.account and w.account is None:
            # no key: the task shows what it needs instead of running
            w._pick(task)
            log("locked task")
            phase("locked")
            return
        if task.account and w.listing is None:
            if waited(30):
                log("account never connected", error=w.account_error)
                shot("0-window")
                finish()
            return
        log("window", runs=w.runs_chip.text if w.runs_chip.IsShown() else None, items=len([r for r in w.nav.rows if r[0] == "item"]))
        shot("0-window")
        w._pick(task)
        phase("task")
    elif p == "chat-open":
        w = window()
        if waited(2):
            shot("1-chat-empty")
            w._send_chat(MESSAGE)
            phase("chat-running")
    elif p == "chat-running":
        w = window()
        if not state.get("chat_shot") and waited(2):
            state["chat_shot"] = True
            shot("2-chat-running")
        if w.chat_busy:
            if waited(300):
                log("chat never finished")
                finish()
            return
        turn = w.chat_turn
        steps = [(type(r).__name__) for r in turn["rows"].values()]
        log("chat reply", text=turn["text"][:600], tools=len(steps), pending=bool(w.pending))
        phase("chat-done")
    elif p == "chat-done":
        if waited(2):
            shot("3-chat-reply")
            w = window()
            report["ok"] = bool(w.chat_turn["text"].strip())
            if w.design_cards:
                state["design"] = next(iter(w.design_cards))
                log("design started", design=state["design"], record=w.designs.get(state["design"]))
                phase("design-wait")
                return
            finish()
    elif p == "design-wait":
        w = window()
        progress = w.designs[state["design"]].get("progress") or {}
        if not progress.get("done"):
            if waited(DEADLINE_S):
                log("design never finished", progress=progress)
                finish()
            return
        log("design done", progress=progress, card=w.design_cards[state["design"]].status.GetLabel())
        shot("4-design-ready")
        if not progress.get("has_project"):
            report["ok"] = False
            finish()
            return
        # the folder dialog, answered with DESIGN_DIR; KiCad's own launch is recorded, then left to run
        os.makedirs(DESIGN_DIR, exist_ok=True)

        class Folder:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def ShowModal(self):
                return wx.ID_OK

            def GetPath(self):
                return DESIGN_DIR

        projects = plugin_module("projects")
        launched = []
        real_popen = projects.subprocess.Popen

        def popen(cmd, *a, **k):
            proc = real_popen(cmd, *a, **k)
            launched.append({"cmd": cmd, "pid": proc.pid})
            return proc

        projects.subprocess.Popen = popen
        state["launched"], state["popen"] = launched, real_popen
        plugin_module("ui").wx.DirDialog = Folder
        w._open_design(state["design"])
        phase("design-open")
    elif p == "design-open":
        w = window()
        record = w.designs[state["design"]]
        if not record.get("project"):
            if waited(120):
                log("project never arrived", record=record)
                finish()
            return
        if waited(8):
            pro = record["project"]
            files = sorted(os.listdir(os.path.dirname(pro)))
            log("design opened", project=pro, files=files, launched=state["launched"],
                card=w.design_cards[state["design"]].status.GetLabel())
            shot("5-design-opened")
            plugin_module("projects").subprocess.Popen = state["popen"]
            report["ok"] = bool(state["launched"]) and any(f.endswith(".kicad_sch") for f in files)
            finish()
    elif p == "locked":
        if waited(1):
            shot("1-locked")
            report["ok"] = True
            finish()
    elif p == "task":
        w = window()
        if SCOPE and w.scope is not None:
            if SCOPE == "pick":
                w.scope.picked = NETS
            w.scope.set_scope(SCOPE)
        fill_advanced(w, ARGS)
        task = plugin_module("catalog").task(TASK)
        log("task page", args=w._arguments(task), scope=w.scope.count.GetLabel() if w.scope else None)
        shot("1-task")
        wx.CallAfter(w._run, task)
        phase("running")
    elif p == "running":
        w = window()
        if w.busy:
            if not state.get("run_shot") and waited(4):
                state["run_shot"] = True
                shot("2-running")
            if int(time.time() - state["since"]) % 15 == 0:
                log("running", stage=w.run_stage, detail=w.run_detail.GetLabel())
            return
        if w.book.GetSelection() != 2:
            return
        log("result", head=w.result_headline, tiles=[(t["label"], t["value"], t.get("sub")) for t in w.result_tiles],
            pending=bool(w.pending))
        shot("3-result")
        if REOPEN and not state.get("reopened"):
            state["reopened"] = True
            w.Close()
            click_menu("Koppar")
            phase("reopen")
            return
        if "did not finish" in w.result_headline or w.result_headline.startswith("This board needs"):
            finish()
            return
        if not w.pending:
            report["ok"] = True
            finish()
            return
        w._on_apply()
        phase("apply")
    elif p == "reopen":
        w = window()
        if w is None or w.book.GetSelection() != 2:
            if waited(30):
                log("reopened window never showed the result")
                finish()
            return
        report["reopen_has_result"] = bool(w.pending)
        log("reopened", head=w.result_headline, pending=bool(w.pending))
        w._on_apply()
        phase("apply")
    elif p == "apply":
        w = window()
        if w is not None and w.applied_text:
            report["after_apply"] = board_state()
            log("applied", text=w.applied_text, board=report["after_apply"])
            phase("applied")
        elif waited(120):
            log("apply never finished", messages=messages[-3:])
            finish()
    elif p == "applied":
        if waited(2):
            shot("4-applied")
            click_menu("Undo", to_menu=True)
            phase("undo")
    elif p == "undo":
        if waited(3):
            report["after_undo"] = board_state()
            log("after undo", board=report["after_undo"])
            changed = report["after_apply"] != report["before"]
            report["ok"] = changed and report["after_undo"] == report["before"] and report.get("reopen_has_result", True)
            finish()


def finish():
    phase("done")
    log("finished", ok=report["ok"])
    # the board is a scratch copy: leave without the save prompt
    wx.CallLater(500, lambda: os._exit(0 if report["ok"] else 1))


wx.CallLater(3000, tick)
