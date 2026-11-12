"""The design rules as the editor holds them, sent with the board.

The .kicad_pro on disk has the rules as last saved. A clearance or a width changed in Board
Setup and not saved yet is only in the editor: sending the file alone routed the board under the
old value. Here the open board's net classes (clearance, track width, via size and drill,
differential pair width and gap) and its board minimums are written over the file's before it
goes, in the file's own units (mm). What KiCad's Python does not reach (the net class patterns,
custom rules) stays as the file has it. Every read is guarded: on a KiCad whose bindings differ,
the file goes as it is, and `stale_rules_note` says so when the board has unsaved changes.
"""

import json

# BOARD_DESIGN_SETTINGS member -> .kicad_pro board.design_settings.rules key
BOARD_RULES = {
    "m_MinClearance": "min_clearance",
    "m_TrackMinWidth": "min_track_width",
    "m_ViasMinSize": "min_via_diameter",
    "m_ViasMinAnnularWidth": "min_via_annular_width",
    "m_MinThroughDrill": "min_through_hole_diameter",
    "m_HoleToHoleMin": "min_hole_to_hole",
    "m_HoleClearance": "min_hole_clearance",
    "m_CopperEdgeClearance": "min_copper_edge_clearance",
    "m_MicroViasMinSize": "min_microvia_diameter",
}

# NETCLASS getter -> .kicad_pro net_settings.classes key; Has<name> says whether the class sets it
CLASS_FIELDS = {
    "Clearance": "clearance",
    "TrackWidth": "track_width",
    "ViaDiameter": "via_diameter",
    "ViaDrill": "via_drill",
    "DiffPairWidth": "diff_pair_width",
    "DiffPairGap": "diff_pair_gap",
    "uViaDiameter": "microvia_diameter",
    "uViaDrill": "microvia_drill",
}

NM_PER_MM = 1_000_000


def _mm(nm):
    return round(nm / NM_PER_MM, 6)


def _class_values(nc):
    out = {}
    for getter, key in CLASS_FIELDS.items():
        has = getattr(nc, f"Has{getter}", None)
        get = getattr(nc, f"Get{getter}", None)
        if get is None:
            continue
        try:
            if has is not None and not has():
                continue
            out[key] = _mm(int(get()))
        except Exception:
            continue
    return out


def live_rules(board):
    """{"rules": {key: mm}, "classes": {name: {key: mm}}} from the open board, as far as this
    KiCad's Python reaches; empty parts where it does not."""
    rules, classes = {}, {}
    try:
        ds = board.GetDesignSettings()
    except Exception:
        return {"rules": rules, "classes": classes}
    for member, key in BOARD_RULES.items():
        try:
            rules[key] = _mm(int(getattr(ds, member)))
        except Exception:
            pass
    try:
        ns = ds.m_NetSettings
        default = ns.GetDefaultNetclass()
        classes["Default"] = _class_values(default)
        for name, nc in ns.GetNetclasses().items():
            classes[str(name)] = _class_values(nc)
    except Exception:
        pass
    return {"rules": rules, "classes": {k: v for k, v in classes.items() if v}}


def merge_into_project(project_text, live):
    """The .kicad_pro text with the editor's rules written over the file's. Returns
    (text, changed: [what differed from the file]). A file that is not JSON goes as it is."""
    if not live["rules"] and not live["classes"]:
        return project_text, []
    try:
        pro = json.loads(project_text) if project_text else {}
    except ValueError:
        return project_text, []
    changed = []
    disk_rules = pro.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
    for key, value in live["rules"].items():
        if disk_rules.get(key) is not None and abs(float(disk_rules[key]) - value) > 1e-6:
            changed.append(f"{key} {disk_rules[key]} -> {value}")
        disk_rules[key] = value
    net = pro.setdefault("net_settings", {})
    disk_classes = net.setdefault("classes", [])
    by_name = {c.get("name"): c for c in disk_classes if isinstance(c, dict)}
    for name, values in live["classes"].items():
        c = by_name.get(name)
        if c is None:
            # a class made in Board Setup and not saved: it goes, its nets come with the next save
            c = {"name": name}
            disk_classes.append(c)
            changed.append(f"class {name} (new)")
        for key, value in values.items():
            if c.get(key) is not None and abs(float(c[key]) - value) > 1e-6:
                changed.append(f"{name} {key} {c[key]} -> {value}")
            c[key] = value
    return json.dumps(pro, indent=2) + "\n", changed


def stale_rules_note(board, live):
    """A line for the user when the editor's rules could not be read and the board has unsaved
    changes (KiCad's older bindings): the saved file's rules are the ones that go."""
    if live["rules"] or live["classes"]:
        return None
    try:
        modified = board.IsModified() if hasattr(board, "IsModified") else False
    except Exception:
        modified = False
    return ("This KiCad does not let Koppar read the rules in the editor: the ones in the saved project file go. "
            "Save the project first if you changed them in Board Setup.") if modified else None
