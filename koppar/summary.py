"""A tool's result, as a person reads it: a headline, a few numbers, the details.

summarize(task_id, result, context) -> {
    "headline": str, "tone": good | warn | bad | info,
    "tiles": [{"label", "value", "sub", "tone"}],          # the numbers that matter, before -> after
    "sections": [{"title", "kind": bullets | table | text, ...}],
}

Bullet and table cells may be (text, link) pairs; a link is "net:<name>",
"ref:<reference>", "xy:<x_mm>,<y_mm>" or "file:<path>", and the window turns
it into "show on board" / "open". The server's results are written for agents
too (next steps, revision numbers to restore): those fields never reach the
page. No wx here.
"""

import ast
import re

# fields written for an agent driving the tools, not for the person at the board
AGENT_FIELDS = {"next", "undo", "download", "note_for_agent"}


def _n(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _fmt(v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.0f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def plural(n, word):
    return f"{_fmt(n)} {word}" + ("" if n == 1 else "s")


def tile(label, value, sub="", tone="info"):
    return {"label": label, "value": _fmt(value), "sub": sub, "tone": tone}


def count_tile(label, value, bad_if_any=True, sub=""):
    """A count where zero is the goal (errors, unconnected items)."""
    v = _n(value)
    return tile(label, v, sub, ("bad" if bad_if_any else "warn") if v else "good")


def change_tile(label, before, after, lower_is_better=True):
    b, a = _n(before), _n(after)
    better = a < b if lower_is_better else a > b
    tone = "good" if better else ("info" if a == b else "warn")
    return tile(label, a, f"was {_fmt(b)}" if a != b else "unchanged", tone)


def humanize(key):
    words = key.replace("_", " ").strip()
    words = re.sub(r"\bmm\b", "(mm)", words)
    words = re.sub(r"\bpct\b", "%", words)
    words = re.sub(r"\b(\w+) s$", r"\1 (s)", words)
    return words[:1].upper() + words[1:]


def net_link(name):
    return (name, f"net:{name}")


def ref_link(ref):
    return (ref, f"ref:{ref}")


def _generic(result):
    """Every plain field as a row, every list as bullets: for tools without a summary of their own."""
    rows, sections = [], []
    for k, v in (result or {}).items():
        if k in AGENT_FIELDS:
            continue
        if isinstance(v, (int, float, str, bool)) and not (isinstance(v, str) and len(v) > 200):
            rows.append((humanize(k), _fmt(v)))
        elif isinstance(v, list) and v:
            items = [x if isinstance(x, str) else ", ".join(f"{humanize(a)}: {_fmt(b)}" for a, b in x.items())
                     if isinstance(x, dict) else _fmt(x) for x in v[:40]]
            sections.append({"title": humanize(k), "kind": "bullets", "items": items})
        elif isinstance(v, dict) and v:
            flat = [(humanize(a), _fmt(b)) for a, b in v.items() if isinstance(b, (int, float, str, bool))]
            if flat:
                sections.append({"title": humanize(k), "kind": "table", "rows": flat})
        elif isinstance(v, str):
            sections.append({"title": humanize(k), "kind": "text", "text": v})
    if rows:
        sections.insert(0, {"title": "Details", "kind": "table", "rows": rows})
    return sections


# -- per task -----------------------------------------------------------------


def _physics(r):
    tiles = [
        tile("Impedance", f"{_n(r.get('impedanceChecked')) - _n(r.get('impedanceOffTarget'))}/{_n(r.get('impedanceChecked'))}",
             "nets on target", "bad" if r.get("impedanceOffTarget") else "good"),
        tile("Current", f"{_n(r.get('currentChecked')) - _n(r.get('currentFailing'))}/{_n(r.get('currentChecked'))}",
             "nets carry it", "bad" if r.get("currentFailing") else "good"),
        tile("Skew", f"{_n(r.get('skewChecked')) - _n(r.get('skewed'))}/{_n(r.get('skewChecked'))}",
             "groups matched", "warn" if r.get("skewed") else "good"),
        tile("Return path", _n(r.get("returnUncovered")), "vias without a ground via", "warn" if r.get("returnUncovered") else "good"),
    ]
    sections = [{"title": "Checks", "kind": "bullets", "items": list(r.get("checks") or [])}]
    for key in ("impedance", "current", "skew", "return_path", "crosstalk", "thermal"):
        rows = r.get(key) or []
        if rows:
            items = [[net_link(x["net"]), "  " + ", ".join(f"{humanize(a)} {_fmt(b)}" for a, b in x.items()
                                                       if a != "net" and isinstance(b, (int, float, str)))]
                     if isinstance(x, dict) and "net" in x else _fmt(x) for x in rows[:40]]
            sections.append({"title": humanize(key), "kind": "bullets", "items": items})
    if r.get("assumptions"):
        sections.append({"title": "How it is judged", "kind": "bullets", "items": list(r["assumptions"]), "small": True})
    failing = _n(r.get("impedanceOffTarget")) + _n(r.get("currentFailing")) + _n(r.get("skewed"))
    summary = str(r.get("summary") or "")
    if summary == "not needed":
        return "Nothing on this board needs a physics check", "info", tiles, sections
    return (f"{failing} problems found" if failing else "Every checked net passes"), ("bad" if failing else "good"), tiles, sections


def _net_rules(r):
    s = r.get("summary") or {}
    tiles = [tile("Net classes", len(r.get("classes") or [])), tile("Nets with a rule", _n(s.get("ruled_nets"))),
             count_tile("Below their width", s.get("nets_below_width"), bad_if_any=False),
             tile("Differential pairs", _n(s.get("pairs")),
                  f"{_n(s.get('pairs_under_50pct_coupled'))} under 50% coupled" if s.get("pairs") else "",
                  "warn" if s.get("pairs_under_50pct_coupled") else "info")]
    sections = [{"title": "Classes", "kind": "bullets", "items": list(r.get("classes") or [])}]
    if r.get("pairs"):
        sections.append({"title": "Pairs", "kind": "bullets", "items": [
            ", ".join(f"{humanize(a)} {_fmt(b)}" for a, b in p.items()) if isinstance(p, dict) else _fmt(p) for p in r["pairs"][:60]]})
    if r.get("width_short"):
        sections.append({"title": "Tracks below their class width", "kind": "bullets", "items": [
            [net_link(w["net"]), "  " + ", ".join(f"{humanize(a)} {_fmt(b)}" for a, b in w.items() if a != "net")]
            if isinstance(w, dict) and "net" in w else _fmt(w) for w in r["width_short"][:60]]})
    if r.get("note"):
        sections.append({"title": "Note", "kind": "text", "text": r["note"]})
    if len(r.get("classes") or []) <= 1 and not s.get("ruled_nets"):
        return "Every net uses the Default class", "info", tiles, sections
    bad = _n(s.get("nets_below_width")) + _n(s.get("pairs_under_50pct_coupled"))
    return (f"{bad} nets need attention" if bad else "Net classes and pairs are in order"), ("warn" if bad else "good"), tiles, sections


def _routed_quality(r, tiles):
    if "drc_errors" in r:
        tiles.append(count_tile("DRC errors", r.get("drc_errors")))
    if "unconnected_items" in r:
        tiles.append(count_tile("Unconnected", r.get("unconnected_items")))


def _polish(r):
    tiles = [change_tile("Vias", r.get("vias_before"), r.get("vias_after")),
             tile("Nets improved", f"{_n(r.get('nets_kept'))}/{_n(r.get('nets_tried'))}", "kept / tried")]
    _routed_quality(r, tiles)
    steps = [[net_link(s["net"]), f"  {s.get('vias_before')} vias before, " + ("kept" if s.get("kept") else "not better, put back")]
             for s in r.get("steps") or []]
    sections = [{"title": "Nets", "kind": "bullets", "items": steps}] if steps else []
    saved = _n(r.get("vias_before")) - _n(r.get("vias_after"))
    return (f"{plural(saved, 'fewer via')}" if saved > 0 else "No via could be removed"), ("good" if saved > 0 else "info"), tiles, sections


def _tidy(r):
    rep = r.get("report") or {}
    seg = rep.get("segments") or []
    tiles = []
    if len(seg) == 2:
        tiles.append(change_tile("Segments", seg[0], seg[1]))
    tiles.append(tile("Mended", _n(rep.get("mended")), "joins fixed"))
    _routed_quality(r, tiles)
    kept = r.get("kept_board", True)
    return ("Copper tidied" if kept else "Nothing better found: the board is unchanged"), ("good" if kept else "info"), tiles, []


def _prune(r):
    tiles = [tile("Vias removed", _n(r.get("vias_removed")), f"of {_n(r.get('candidates'))} candidates", "good" if r.get("vias_removed") else "info"),
             tile("Stubs removed", _n(r.get("stub_tracks_removed"))),
             change_tile("Unconnected", r.get("unconnected_before"), r.get("unconnected_after"))]
    by_net = r.get("removed_by_net")
    if isinstance(by_net, str):
        try:
            by_net = ast.literal_eval(by_net)
        except (ValueError, SyntaxError):
            by_net = {}
    sections = [{"title": "Removed by net", "kind": "bullets",
                 "items": [[net_link(k), f"  {v}"] for k, v in (by_net or {}).items()]}] if by_net else []
    return f"{plural(_n(r.get('vias_removed')), 'redundant via')} removed", "good" if r.get("vias_removed") else "info", tiles, sections


def _cleanup(r):
    keys = ("dangling_tracks", "duplicates", "zero_length", "unconnected_vias", "trimmed_overhangs")
    tiles = [tile(humanize(k), _n(r.get(k))) for k in keys if k in r]
    total = sum(_n(r.get(k)) for k in keys)
    return (f"{total} items removed" if total else "Nothing to clean up"), ("good" if total else "info"), tiles, []


def _place(r):
    tiles = [tile("Parts moved", _n(r.get("moved")), f"{_n(r.get('held'))} held" if r.get("held") else "")]
    if "overlap_count" in r:
        tiles.append(count_tile("Overlaps", r.get("overlap_count")))
    if "overlap_mm2" in r:
        tiles.append(tile("Overlap", _n(r.get("overlap_mm2")), "mm² of courtyards", "bad" if r.get("overlap_mm2") else "good"))
    for k in ("ratsnest_mm", "wirelength_mm"):
        if k in r:
            tiles.append(tile("Connection length", _n(r.get(k)), "mm of ratsnest"))
    if "bottom_side" in r:
        tiles.append(tile("On the bottom", _n(r.get("bottom_side"))))
    sections = []
    notes = list(r.get("placement_notes") or [])
    if notes:
        sections.append({"title": "Notes", "kind": "bullets", "items": [_link_refs(n) for n in notes[:40]]})
    pairs = r.get("overlapping_pairs") or []
    if pairs:
        sections.append({"title": "Overlapping", "kind": "bullets", "items": [_fmt(p) for p in pairs[:40]]})
    if r.get("note"):
        sections.append({"title": "Next", "kind": "text", "text": "Check the placement, then route it."})
    return f"{plural(_n(r.get('moved')), 'part')} placed", "good", tiles, sections


def _link_refs(text):
    """A note that starts with a reference ("P101: ...") gets the reference as a link."""
    m = re.match(r"^([A-Z]+\d+[A-Z]?): (.*)$", text)
    return [ref_link(m.group(1)), ": " + m.group(2)] if m else text


def _route(r, ctx):
    if r.get("message") and not r.get("nets_routed_now") and not r.get("failed"):
        return r["message"][:1].upper() + r["message"][1:], "good", [tile("Nets kept", _n(r.get("nets_kept")))], []
    asked = (ctx or {}).get("nets")
    failed = list(r.get("failed") or [])
    if asked:
        # failed lists every open net on the board; only the ones asked for matter here
        wanted = set(asked)
        failed = [f for f in failed if f in wanted]
    routed_now = _n(r.get("nets_routed_now", r.get("nets_routed")))
    tiles = [tile("Nets routed", routed_now, f"of {len(asked)} asked" if asked else "this run", "good" if routed_now else "warn")]
    kicad = r.get("kicad") or {}
    if "completion_pct" in r:
        tiles.append(tile("Board complete", f"{_fmt(_n(r.get('completion_pct')))} %", f"{_n(r.get('unconnected_nets'))} nets open"))
    if "unconnected_items" in kicad:
        tiles.append(count_tile("Unconnected", kicad.get("unconnected_items"), bad_if_any=False))
    if "via_count" in r:
        tiles.append(tile("Vias", _n(r.get("via_count"))))
    sections = []
    if failed:
        sections.append({"title": "Not routed", "kind": "bullets", "items": [[net_link(n)] for n in failed[:80]]})
    rules = r.get("rules_used") or {}
    if rules:
        sections.append({"title": "Rules used", "kind": "table", "rows": [(humanize(k), _fmt(v)) for k, v in rules.items()]})
    if failed:
        return f"{plural(len(failed), 'net')} could not be routed", "warn", tiles, sections
    return (f"{plural(routed_now, 'net')} routed" if routed_now else "Nothing to route"), ("good" if routed_now else "info"), tiles, sections


def _diagnose(r):
    nets = r.get("nets") or []
    items, blocked, cramped, advice_count = [], 0, 0, {}
    for n in nets[:80]:
        advice = (n.get("advice") or "").split(":", 1)[-1].strip()
        if advice:
            advice_count[advice] = advice_count.get(advice, 0) + 1
    for n in nets[:80]:
        line = [net_link(n.get("net", "?"))]
        blockers = n.get("blockers") or []
        if blockers:
            blocked += 1
            line.append("  in the way: ")
            for i, b in enumerate(blockers[:4]):
                item = re.sub(r"\s*\((pad [^)]+)\)", r" \1", b.get("item", "?"))
                ref = re.match(r"^([A-Z]+\d+[A-Z]?)\b", item)
                label = f"{item} ({b['net']})" if b.get("net") else item
                line += [", " if i else "", (label, f"ref:{ref.group(1)}") if ref else label]
        if any(p.get("cramped") or p.get("cramped_by_placement") or p.get("walled_by_own_pads") for p in n.get("pads") or []):
            cramped += 1
            line.append("; cramped pads")
        advice = (n.get("advice") or "").split(":", 1)[-1].strip()
        if advice and advice_count.get(advice, 0) == 1:
            # advice shared by several nets is said once, under What to do
            line.append(" - " + advice)
        items.append(line)
    tiles = [tile("Open nets", len(r.get("open_nets") or nets), "", "warn" if nets else "good"),
             tile("Blocked by other copper", blocked, "", "warn" if blocked else "good"),
             tile("Cramped pads", cramped, "", "warn" if cramped else "good")]
    sections = []
    shared = sorted(((c, a) for a, c in advice_count.items() if c > 1), reverse=True)
    if shared:
        sections.append({"title": "What to do", "kind": "bullets",
                         "items": [f"{a[:1].upper()}{a[1:]} ({plural(c, 'net')})" for c, a in shared]})
    if items:
        sections.append({"title": "Open nets", "kind": "bullets", "items": items})
    if not nets:
        return "Every net is connected", "good", tiles, sections
    return f"{plural(len(nets), 'open net')}, {blocked} blocked by other copper", "warn", tiles, sections


def _bridge(r):
    tiles = [tile("Islands", _n(r.get("islands"))), tile("Vias added", _n(r.get("vias_added")), "", "good" if r.get("vias_added") else "info")]
    left = r.get("islands_without_site") or []
    if left:
        tiles.append(count_tile("Left without a site", len(left), bad_if_any=False))
    return (f"{_n(r.get('vias_added'))} vias join the islands" if r.get("vias_added") else "No pour island to join"), \
        ("good" if not left else "warn"), tiles, []


def _tie(r):
    per = r.get("per_net") or {}
    tiles = [tile("Pads tied", _n(r.get("ties")), ", ".join(r.get("nets") or []))]
    sections = [{"title": "By net", "kind": "bullets", "items": [[net_link(k), f"  {v}"] for k, v in per.items()]}] if per else []
    return (f"{_n(r.get('ties'))} pads tied to their pour" if r.get("ties") else "Every plane pad already reaches its pour"), "good", tiles, sections


def _autoroute(r, ctx):
    routing, drc = r.get("routing") or {}, r.get("drc") or {}
    tiles = []
    census = (ctx or {}).get("census_after")
    if census:
        done = sum(1 for n in census if n["routed"])
        tiles.append(tile("Nets connected", f"{done}/{len(census)}", "in the result", "good" if done == len(census) else "warn"))
    tiles += [count_tile("DRC errors", drc.get("errors")), count_tile("Unconnected", drc.get("unconnected_items")),
              tile("Server time", f"{round(_n(routing.get('route_s')) + _n(routing.get('finish_s')))} s",
                   "stopped at the time cap" if routing.get("capped") else "")]
    sections = []
    rows = [("Router", routing.get("router")), ("Winning arm", routing.get("shipped")),
            ("Copper finishing", "kept" if routing.get("copper_finished") else "not kept"),
            ("Open after the first route", routing.get("open_after_route"))]
    sections.append({"title": "Run", "kind": "table", "rows": [(a, _fmt(b)) for a, b in rows if b is not None]})
    if drc.get("errors_by_type"):
        sections.append({"title": "Errors by type", "kind": "table", "rows": [(humanize(k), _fmt(v)) for k, v in drc["errors_by_type"].items()]})
    bad = _n(drc.get("errors")) + _n(drc.get("unconnected_items"))
    head = "Routed: DRC clean, everything connected" if not bad else f"Routed with {_n(drc.get('errors'))} DRC errors and {_n(drc.get('unconnected_items'))} open connections"
    return head, ("good" if not bad else "warn"), tiles, sections


def _finish(r):
    tiles = [change_tile("Unconnected", r.get("unconnected_items_before"), r.get("unconnected_items_after")),
             change_tile("DRC errors", r.get("drc_errors_before"), r.get("drc_errors_after"))]
    steps = [f"{_fmt(t.get('at_s'))} s: {t.get('unconnected')} open, {t.get('errors')} errors ({t.get('step')})" for t in r.get("timeline") or []]
    sections = [{"title": "Progress", "kind": "bullets", "items": steps}] if steps else []
    left = _n(r.get("unconnected_items_after"))
    closed = _n(r.get("unconnected_items_before")) - left
    head = "Every connection is made" if not left else f"{closed} connections made, {left} still open"
    return head, ("good" if not left else "warn"), tiles, sections


def _ripup(r):
    tiles = [tile("Tracks removed", _n(r.get("removed_tracks"))), tile("Vias removed", _n(r.get("removed_vias"))),
             tile("Pours removed", _n(r.get("removed_zones"))), tile("Locked, kept", _n(r.get("kept_locked")))]
    return f"{_n(r.get('removed_tracks'))} tracks and {_n(r.get('removed_vias'))} vias removed", "info", tiles, []


def _couple(r):
    vias, unc, drc = r.get("vias") or {}, r.get("unconnected_items") or {}, r.get("drc_errors") or {}
    tiles = [tile("Pairs coupled", f"{_n(r.get('kept'))}/{_n(r.get('pairs_tried'))}", "kept / tried"),
             change_tile("Vias", vias.get("start"), vias.get("end")),
             change_tile("Unconnected", unc.get("start"), unc.get("end")),
             change_tile("DRC errors", drc.get("start"), drc.get("end"))]
    pairs = r.get("pairs_now") or []
    sections = [{"title": "Pairs", "kind": "bullets", "items": [
        ", ".join(f"{humanize(a)} {_fmt(b)}" for a, b in p.items()) if isinstance(p, dict) else _fmt(p) for p in pairs[:40]]}] if pairs else []
    if not r.get("pairs_tried"):
        return "This board has no differential pairs to couple", "info", tiles, sections
    return f"{_n(r.get('kept'))} of {_n(r.get('pairs_tried'))} pairs coupled", "good" if r.get("kept") else "info", tiles, sections


SUMMARIES = {
    "physics": _physics, "net_rules": _net_rules,
    "polish_vias": _polish, "tidy": _tidy, "prune_vias": _prune, "cleanup": _cleanup,
    "auto_place": _place, "anneal": _place, "diagnose": _diagnose, "bridge": _bridge, "tie": _tie,
    "finish": _finish, "ripup": _ripup, "couple_pairs": _couple,
}
WITH_CONTEXT = {"route_nets": _route, "repair": _route, "autoroute": _autoroute}


def summarize(task_id, result, context=None):
    result = result or {}
    try:
        if task_id in SUMMARIES:
            head, tone, tiles, sections = SUMMARIES[task_id](result)
        elif task_id in WITH_CONTEXT:
            head, tone, tiles, sections = WITH_CONTEXT[task_id](result, context)
        else:
            head, tone, tiles, sections = "Done", "info", [], []
    except Exception as e:  # a result shape this plugin has not seen: still show it
        head, tone, tiles, sections = "Done", "info", [], [{"title": "Could not summarise", "kind": "text", "text": str(e)}]
    # whatever the summary did not show is still there, under All details
    sections.append({"title": "All details", "kind": "details", "sections": _generic(result)})
    return {"headline": head, "tone": tone, "tiles": tiles, "sections": sections}
