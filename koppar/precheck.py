"""The board check before a route: what on the board a router cannot fix, said before it runs.

A route works around the copper and the parts as they are. A short (two nets' copper touching),
parts placed on each other, or a net class clearance a footprint's own pads cannot meet stay on
the board after any route, and the route is judged with them. The check runs KiCad's DRC on the
board as it is in the editor, in a separate kicad-cli process (a DRC run inside the editor's own
process can take KiCad down with it), sorts what it finds, and proposes what can be done:

    shorts        copper of two nets touching: parts on each other, or a via or track in a
                  pad of another net
    overlaps      courtyards of two parts on each other
    conflicts     a net class clearance larger than the gap between a footprint's own pads,
                  grouped per class: no route can meet it there
    other         every other error, counted by type

and the fix the account can run on its copy of the board before the route: move the smaller part
of each overlapping pair (only those parts move), rip up the stray via or track.

`classify` and `fix_plan` read the report only, with no pcbnew, so they run anywhere.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

KICAD_CLI_TIMEOUT_S = 240
# a part touching another counts as a placement problem; these kinds are copper to rip up
COPPER_KINDS = ("Via", "Track", "Arc")
_REF = re.compile(r" of (\S+)")
_NET = re.compile(r"\[([^\]]*)\]")
_CLASS_RULE = re.compile(r"netclass '([^']+)'(?: \(diff pair\))? clearance ([\d.]+) mm; actual ([\d.]+) mm")


def _kind(item):
    return (item.get("description") or "").split(" ")[0]


def _ref(item):
    m = _REF.search(item.get("description") or "")
    return m.group(1) if m else None


def _net(item):
    m = _NET.search(item.get("description") or "")
    return m.group(1) if m else None


def _footprint_ref(item):
    """'Footprint C150' (a courtyard item) -> C150."""
    d = item.get("description") or ""
    return d.split(" ", 1)[1].strip() if d.startswith("Footprint ") else None


def classify(report):
    """A kicad-cli DRC report (JSON) sorted into what a route cannot fix.

    Returns {shorts, overlaps, conflicts, other, errors, unconnected}: each short and overlap with
    its parts, nets and position; each conflict as {netclass, clearance_mm, closest_mm, refs}."""
    shorts, overlaps, other = [], [], {}
    conflicts = {}
    errors = 0
    for v in report.get("violations") or []:
        if v.get("excluded") or v.get("severity") != "error":
            continue
        errors += 1
        items = v.get("items") or []
        kind = v.get("type")
        if kind == "shorting_items":
            refs = [r for r in (_ref(i) for i in items) if r]
            copper = [i for i in items if _kind(i) in COPPER_KINDS]
            shorts.append({
                "refs": sorted(set(refs)),
                "nets": [n for n in (_net(i) for i in items) if n],
                # two parts' pads on each other, or a via / track of one net in another's pad
                "cause": "copper" if copper else "parts",
                "copper": [{"kind": _kind(i), "net": _net(i), "pos": i.get("pos"), "uuid": i.get("uuid")} for i in copper],
                "pos": (items[0].get("pos") if items else None),
                "text": v.get("description") or "",
            })
        elif kind == "courtyards_overlap":
            refs = [r for r in (_footprint_ref(i) for i in items) if r]
            if len(set(refs)) >= 2:
                overlaps.append({"refs": sorted(set(refs)), "pos": items[0].get("pos") if items else None})
            else:
                other[kind] = other.get(kind, 0) + 1
        elif kind == "clearance" and len(items) == 2 and all(_kind(i) in ("Pad", "PTH") for i in items) \
                and _ref(items[0]) and _ref(items[0]) == _ref(items[1]):
            m = _CLASS_RULE.search(v.get("description") or "")
            if not m:
                other[kind] = other.get(kind, 0) + 1
                continue
            c = conflicts.setdefault(m.group(1), {"netclass": m.group(1), "clearance_mm": float(m.group(2)),
                                                  "closest_mm": float(m.group(3)), "refs": set(), "count": 0})
            c["closest_mm"] = min(c["closest_mm"], float(m.group(3)))
            c["refs"].add(_ref(items[0]))
            c["count"] += 1
        else:
            other[kind] = other.get(kind, 0) + 1
    out_conflicts = []
    for c in sorted(conflicts.values(), key=lambda c: -c["count"]):
        out_conflicts.append({**c, "refs": sorted(c["refs"], key=_natural)})
    return {
        "shorts": shorts,
        "overlaps": overlaps,
        "conflicts": out_conflicts,
        "other": dict(sorted(other.items(), key=lambda kv: -kv[1])),
        "errors": errors,
        "unconnected": len(report.get("unconnected_items") or []),
    }


def _natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


def serious(found):
    """Whether the check has something to say before the route (shorts, parts on each other, or a
    class a footprint cannot meet). Other errors are only counted."""
    return bool(found["shorts"] or found["overlaps"] or found["conflicts"])


def fix_plan(found, pads_of=None, locked=()):
    """What the account can fix on its copy of the board before the route.

    `pads_of(ref)` gives a part's pad count (the larger part of a pair stays where it is);
    `locked` parts never move. Returns {move: [refs], rip: [{net, x_mm, y_mm}], left: [text]}:
    the parts that move (only those), the stray copper to rip, and what has no automatic fix."""
    pads_of = pads_of or (lambda ref: 0)
    locked = set(locked)
    move, rip, left = set(), [], []
    pairs = [s["refs"] for s in found["shorts"] if s["cause"] == "parts"] + [o["refs"] for o in found["overlaps"]]
    for refs in pairs:
        free = [r for r in refs if r not in locked]
        if not free:
            left.append(f"{' and '.join(refs)} are locked and overlap: move one in KiCad.")
            continue
        if len(free) < len(refs):
            move.update(free)
            continue
        # the part with fewer pads moves; parts of the same size both may move
        fewest = min(pads_of(r) for r in free)
        move.update(r for r in free if pads_of(r) == fewest)
    for s in found["shorts"]:
        for c in s["copper"]:
            pos = c.get("pos") or {}
            if c.get("net") and "x" in pos and "y" in pos:
                rip.append({"net": c["net"], "x_mm": pos["x"], "y_mm": pos["y"], "kind": c["kind"]})
    for c in found["conflicts"]:
        left.append(f"Net class {c['netclass']} asks for {c['clearance_mm']:g} mm, and the pads of "
                    f"{_list(c['refs'])} are as close as {c['closest_mm']:g} mm: lower the class clearance, "
                    f"or add a rule area with a smaller clearance at these parts, in Board Setup.")
    return {"move": sorted(move, key=_natural), "rip": rip, "left": left}


def _list(refs, most=6):
    return ", ".join(refs[:most]) + (f" and {len(refs) - most} more" if len(refs) > most else "")


def fingerprint(found):
    """The findings in a few words: a board the user chose to route anyway is not asked again
    until what the check finds changes."""
    parts = sorted("|".join(s["refs"] + s["nets"]) for s in found["shorts"])
    parts += sorted("|".join(o["refs"]) for o in found["overlaps"])
    parts += sorted(f"{c['netclass']}:{','.join(c['refs'])}" for c in found["conflicts"])
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16] if parts else ""


# -- running KiCad's DRC ----------------------------------------------------------------------


def kicad_cli(pcbnew_module=None):
    """kicad-cli of the KiCad this plugin runs in (KiCad 8 and later), or None."""
    names = ["kicad-cli.exe", "kicad-cli"] if sys.platform == "win32" else ["kicad-cli"]
    roots = []
    mod_file = getattr(pcbnew_module, "__file__", None) if pcbnew_module else None
    for start in (mod_file, sys.executable):
        if not start:
            continue
        d = os.path.dirname(os.path.abspath(start))
        for _ in range(8):
            roots.append(d)
            if sys.platform == "darwin" and d.endswith(".app"):
                roots.append(os.path.join(d, "Contents", "MacOS"))
            d = os.path.dirname(d)
    if sys.platform == "darwin":
        roots.append("/Applications/KiCad/KiCad.app/Contents/MacOS")
    for root in roots:
        for name in names:
            p = os.path.join(root, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    found = shutil.which("kicad-cli")
    return found


def run_drc(cli, folder, stem):
    """KiCad's DRC on <folder>/<stem>.kicad_pcb (its .kicad_pro and .kicad_dru beside it), in its
    own process. Returns the JSON report, or raises RuntimeError with what went wrong."""
    out = os.path.join(folder, f"{stem}.check.json")
    try:
        os.remove(out)
    except OSError:
        pass
    args = [cli, "pcb", "drc", "--format", "json", "--severity-all", "-o", out, f"{stem}.kicad_pcb"]
    kw = {"cwd": folder, "capture_output": True, "text": True, "timeout": KICAD_CLI_TIMEOUT_S}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console flashes up
    try:
        proc = subprocess.run(args, **kw)
    except subprocess.TimeoutExpired:
        raise RuntimeError("KiCad's design-rule check took too long")
    except OSError as e:
        raise RuntimeError(f"KiCad's design-rule check did not start: {e}")
    if not os.path.exists(out):
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no report"]
        raise RuntimeError(f"KiCad's design-rule check did not finish: {tail[0][:200]}")
    with open(out, "r", encoding="utf-8") as f:
        return json.load(f)


def from_server_report(view):
    """The account's run_drc result in the shape `classify` reads (the fallback where this KiCad
    has no kicad-cli)."""
    violations = []
    for v in (view or {}).get("violations") or []:
        violations.append({
            "type": v.get("type"), "severity": v.get("severity"), "description": v.get("description"),
            "items": [{"description": i.get("item"), "pos": {"x": i.get("x_mm"), "y": i.get("y_mm")}} for i in v.get("items") or []],
        })
    return {"violations": violations, "unconnected_items": [None] * int((view or {}).get("unconnected_items") or 0)}
