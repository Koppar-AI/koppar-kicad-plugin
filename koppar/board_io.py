"""The board side: snapshot the open board, list its nets, bring routed copper back.

Everything that touches pcbnew is here. The routed board is read with KiCad's
own parser (PCB_IO_KICAD_SEXPR, which loads a file without touching the open
project) and its items are duplicated into the open board, so tracks, arcs,
vias (padstacks included) and zones come back exactly as the server wrote
them.
"""

import io
import json
import os
import re
import tempfile
import zipfile

import pcbnew

from . import config, legacy


def kicad_version():
    try:
        return pcbnew.Version()
    except Exception:
        return None


def install_source():
    """Where this copy of the plugin was installed from, sent as X-Koppar-Install.

    KiCad records every package it installed in installed_packages.json, in its user settings
    folder, with the repository it came from: 'pcm:<repository id>' (the Plugin and Content
    Manager; KiCad's official repository is 46f88614eade3439, the head of the sha256 of its
    URL), 'file' (Install from File), 'manual' (copied into the plugins folder, so not in the
    record), 'source' (a checkout, not a package). None when the record cannot be read.
    """
    if not config.PACKAGED:
        return "source"
    try:
        path = os.path.join(pcbnew.SETTINGS_MANAGER.GetUserSettingsPath(), "installed_packages.json")
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f).get("packages") or []
    except Exception:
        return None
    for entry in entries:
        if (entry.get("package") or {}).get("identifier") == config.PACKAGE_ID:
            repo = str(entry.get("repository_id") or "").strip().lower()
            return f"pcm:{repo}" if re.fullmatch(r"[0-9a-f]{1,64}", repo) else "file"
    return "manual"


def kicad_major():
    m = re.search(r"(\d+)\.", kicad_version() or "")
    return int(m.group(1)) if m else 99


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def is_newer_format(path):
    """Whether the file was written by a KiCad newer than this one (the server's 10 on a 9, say)."""
    return legacy.needs_downgrade(_read(path)[:2000], kicad_major())


def board_path(board):
    return board.GetFileName() or ""


def snapshot_board(board, dest_path):
    """Write the board as it is in the editor (unsaved edits included).

    The third argument skips the project settings, so the open project is
    neither written nor re-pointed at the snapshot's folder.
    """
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    pcbnew.SaveBoard(dest_path, board, True)
    with open(dest_path, "r", encoding="utf-8") as f:
        return f.read()


def project_files(path):
    """The project (.kicad_pro) and custom rules (.kicad_dru) beside the board."""
    stem = os.path.splitext(path)[0]
    out = {}
    for key, ext in (("project", ".kicad_pro"), ("rules", ".kicad_dru")):
        try:
            with open(stem + ext, "r", encoding="utf-8") as f:
                text = f.read()
            if text.strip():
                out[key] = text
        except OSError:
            pass
    return out


def project_zip(path, board_text):
    """The board as it is in the editor with its project and rules, zipped under
    the board's own name: what an account workspace is created or synced from."""
    stem = os.path.splitext(os.path.basename(path))[0]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{stem}.kicad_pcb", board_text)
        extra = project_files(path)
        if "project" in extra:
            z.writestr(f"{stem}.kicad_pro", extra["project"])
        if "rules" in extra:
            z.writestr(f"{stem}.kicad_dru", extra["rules"])
    return buf.getvalue()


def net_census(board):
    """Every net with pads: [{name, pads, routed}], `routed` meaning all its
    pads are joined by copper already (tracks, vias or a filled zone)."""
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    pads_by_net = {}
    for pad in board.GetPads():
        if pad.GetNetCode() > 0:
            pads_by_net.setdefault(pad.GetNetname(), []).append(pad)
    joined = _joined_by_items(conn, pads_by_net)
    if joined is None:
        joined = _joined_by_walk(board, conn, pads_by_net)
    out = [{"name": name, "pads": len(pads), "routed": joined(name, pads)}
           for name, pads in pads_by_net.items() if len(pads) >= 2]  # a one-pad net has nothing to route
    out.sort(key=lambda n: (n["routed"], n["name"].lower()))
    return out


def _uuid(item):
    return item.m_Uuid.AsString()


def _joined_by_items(conn, pads_by_net):
    """KiCad 10: GetConnectedItems(item) answers directly. None where it needs a
    type list Python cannot build (KiCad 8 and 9)."""
    try:
        conn.GetConnectedItems(next(p[0] for p in pads_by_net.values()))
    except (TypeError, StopIteration):
        return None

    def joined(name, pads):
        # the connected set may or may not hold the pad it started from: count by identity
        reached = {_uuid(pads[0])}
        for it in conn.GetConnectedItems(pads[0]):
            if it.GetClass() == "PAD" and it.GetNetname() == name:
                reached.add(_uuid(it))
        return len(reached) >= len(pads)

    return joined


def _joined_by_walk(board, conn, pads_by_net):
    """KiCad 8 and 9: the copper graph from the queries that take an item alone.
    Neighbours are not reported both ways (a via lists no tracks), so every
    track, via and pour adds its edges and the clusters come from a union-find."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    def link(item, others):
        for o in others:
            if o.GetNetCode() == item.GetNetCode():
                union(_uuid(item), _uuid(o))

    for t in board.GetTracks():
        if t.GetNetCode() > 0:
            link(t, conn.GetConnectedTracks(t))
            link(t, conn.GetConnectedPads(t))
    for z in board.Zones():
        if not z.GetIsRuleArea() and z.GetNetCode() > 0:
            link(z, conn.GetConnectedTracks(z))
            link(z, conn.GetConnectedPads(z))
    for pads in pads_by_net.values():
        for p in pads:
            link(p, conn.GetConnectedPads(p))

    def joined(name, pads):
        return len({find(_uuid(p)) for p in pads}) == 1

    return joined


def _track_key(t):
    if t.GetClass() == "PCB_VIA":
        return ("via", t.GetPosition().x, t.GetPosition().y)
    return (t.GetClass(), t.GetLayer(), t.GetStart().x, t.GetStart().y, t.GetEnd().x, t.GetEnd().y)


def _zone_layers(z):
    return tuple(sorted(z.GetLayerSet().Seq()))


def _footprint_offsets(board, routed):
    """References whose position or rotation differs between the two boards:
    copper routed around a part that has since moved will not line up."""
    here = {fp.GetReference(): fp for fp in board.GetFootprints()}
    moved = []
    for fp in routed.GetFootprints():
        mine = here.get(fp.GetReference())
        if mine is None:
            continue
        d = mine.GetPosition() - fp.GetPosition()
        if abs(d.x) > 1000 or abs(d.y) > 1000 or abs(mine.GetOrientationDegrees() - fp.GetOrientationDegrees()) > 0.01:
            moved.append(fp.GetReference())
    return moved


def _off_board_layers(board, item):
    """Whether a track or via sits on a copper layer the board does not have. A via
    is judged by the layers it spans (KiCad 7 reports all 32 for a through via)."""
    if item.GetClass() == "PCB_VIA":
        via = item.Cast() if hasattr(item, "Cast") else item
        layers = [via.TopLayer(), via.BottomLayer()]
    else:
        layers = [item.GetLayer()]
    return any(not board.IsLayerEnabled(layer) for layer in layers)


def load_routed(path, board=None):
    """The routed board, parsed by KiCad without loading it as a project.

    On a KiCad older than the file (KiCad 9 reading the server's KiCad 10 board)
    the file never reaches the parser, which aborts the process on a newer
    format: the open board's own file takes the result's copper instead
    (legacy.py), and that is what is parsed."""
    if is_newer_format(path):
        board = board or pcbnew.GetBoard()
        work = tempfile.mkdtemp(prefix="koppar-")
        base = snapshot_board(board, os.path.join(work, "base.kicad_pcb"))
        merged = os.path.join(work, "result.kicad_pcb")
        with open(merged, "w", encoding="utf-8") as f:
            f.write(legacy.merge_copper(base, _read(path), kicad_major()))
        path = merged
    return _read_board(path)


def _read_board(path):
    """KiCad's s-expression reader: PCB_IO_KICAD_SEXPR.LoadBoard from KiCad 8, PCB_PLUGIN.Load in 7."""
    if hasattr(pcbnew, "PCB_IO_KICAD_SEXPR"):
        return pcbnew.PCB_IO_KICAD_SEXPR().LoadBoard(path, None)
    return pcbnew.PCB_PLUGIN().Load(path, None)


def routed_nets(path, board=None):
    """The nets the routed board has copper for."""
    routed = load_routed(path, board)
    return sorted({t.GetNetname() for t in routed.GetTracks() if t.GetNetCode() > 0})


def apply_routed(board, routed_path, nets, import_zones=True, fill_zones=True):
    """Swap the copper of `nets` (names; None for every net) on `board` for the routed board's.

    For each chosen net: its tracks, arcs and vias on the board are removed
    (locked ones stay) and the routed ones are added; with `import_zones`, a
    routed pour is added where the board has no zone of that net on those
    layers. Nothing else on the board changes. Run inside an action plugin's
    Run() and KiCad records the whole swap as one undo step.
    """
    routed = load_routed(routed_path, board)
    board_nets = board.GetNetsByName()
    if nets is None:
        wanted = {t.GetNetname() for b in (board, routed) for t in b.GetTracks() if t.GetNetCode() > 0}
        wanted |= {z.GetNetname() for z in routed.Zones() if z.GetNetCode() > 0}
    else:
        wanted = set(nets)
    stats = {
        "removed": 0, "tracks": 0, "vias": 0, "zones": 0,
        "skipped_layers": 0, "missing_nets": [], "moved_footprints": _footprint_offsets(board, routed),
    }

    def net_of(name):
        return board_nets[name] if board_nets.has_key(name) else None

    missing = {n for n in wanted if net_of(n) is None}
    stats["missing_nets"] = sorted(missing)
    wanted -= missing

    locked = set()
    for t in list(board.GetTracks()):
        if t.GetNetname() not in wanted:
            continue
        if t.IsLocked():
            locked.add(_track_key(t))
            continue
        # RemoveNative keeps the C++ object owned by KiCad (the undo list holds it)
        if hasattr(board, "RemoveNative"):
            board.RemoveNative(t)
        else:
            board.Remove(t)
            t.thisown = False
        stats["removed"] += 1

    for t in routed.GetTracks():
        name = t.GetNetname()
        if name not in wanted or _track_key(t) in locked:
            continue
        if _off_board_layers(board, t):
            stats["skipped_layers"] += 1
            continue
        item = t.Duplicate().Cast()
        item.SetNet(net_of(name))
        board.Add(item)
        stats["vias" if item.GetClass() == "PCB_VIA" else "tracks"] += 1

    if import_zones:
        have = {}
        for z in board.Zones():
            if not z.GetIsRuleArea():
                have.setdefault(z.GetNetname(), []).append(set(_zone_layers(z)))
        for z in routed.Zones():
            name = z.GetNetname()
            if z.GetIsRuleArea() or name not in wanted:
                continue
            layers = set(_zone_layers(z))
            if any(layers & mine for mine in have.get(name, [])):
                continue
            if any(not board.IsLayerEnabled(layer) for layer in layers):
                stats["skipped_layers"] += 1
                continue
            item = z.Duplicate().Cast()
            item.SetNet(net_of(name))
            # filled again below, against this board
            item.UnFill()
            board.Add(item)
            have.setdefault(name, []).append(layers)
            stats["zones"] += 1

    board.BuildConnectivity()
    if fill_zones and len(list(board.Zones())) > 0:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        board.BuildConnectivity()
    return stats


def _flip(fp):
    """Put a footprint on the other side, across its own position (the signature
    changed in KiCad 9: a FLIP_DIRECTION instead of a left/right bool)."""
    pos = fp.GetPosition()
    if hasattr(pcbnew, "FLIP_DIRECTION"):
        fp.Flip(pos, pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    else:
        fp.Flip(pos, False)


def apply_placement(board, result_path):
    """Move the board's footprints where the workspace placed them (matched by
    UUID, else reference): position, side, rotation. Locked footprints stay."""
    mine_by_uuid = {fp.m_Uuid.AsString(): fp for fp in board.GetFootprints()}
    mine_by_ref = {fp.GetReference(): fp for fp in board.GetFootprints()}
    stats = {"moved": 0, "locked": [], "missing": []}
    for p in _placements(board, result_path):
        mine = mine_by_uuid.get(p["uuid"]) or mine_by_ref.get(p["ref"])
        if mine is None:
            stats["missing"].append(p["ref"])
            continue
        same = (mine.GetPosition() == p["pos"] and mine.GetLayer() == p["layer"]
                and abs(mine.GetOrientationDegrees() - p["rot"]) < 0.01)
        if same:
            continue
        if mine.IsLocked():
            stats["locked"].append(mine.GetReference())
            continue
        if mine.GetLayer() != p["layer"]:
            _flip(mine)
        mine.SetOrientationDegrees(p["rot"])
        mine.SetPosition(p["pos"])
        stats["moved"] += 1
    return stats


def _placements(board, result_path):
    """Where the result puts each footprint: from KiCad's parser, or as text from a
    file newer than this KiCad (the merged board keeps the editor's positions)."""
    if is_newer_format(result_path):
        out = []
        for p in legacy.placements(_read(result_path)):
            out.append({"uuid": p["uuid"], "ref": p["ref"], "rot": p["rot"],
                        "pos": pcbnew.VECTOR2I(pcbnew.FromMM(p["x"]), pcbnew.FromMM(p["y"])),
                        "layer": pcbnew.B_Cu if p["layer"] == "B.Cu" else pcbnew.F_Cu})
        return out
    return [{"uuid": fp.m_Uuid.AsString(), "ref": fp.GetReference(), "rot": fp.GetOrientationDegrees(),
             "pos": fp.GetPosition(), "layer": fp.GetLayer()} for fp in load_routed(result_path, board).GetFootprints()]


def apply_result(board, result_path, mode, nets=None, import_zones=True):
    """Bring a result into the board the way its tool calls for (catalog.py):
    copper, or placement and then copper. Returns the stats of each part."""
    out = {}
    if mode == "placement":
        out["placement"] = apply_placement(board, result_path)
    out["copper"] = apply_routed(board, result_path, nets, import_zones=import_zones)
    return out
