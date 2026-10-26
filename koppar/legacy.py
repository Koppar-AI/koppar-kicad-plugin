"""Results for an older KiCad.

The server writes boards with KiCad 10. KiCad 9 and earlier cannot parse a
newer file at all (and abort the process trying), so on those the routed board
is never handed to KiCad's parser. Instead the board as it is in the editor
(written by this KiCad, in its own format) keeps everything but its copper,
and the result's tracks, arcs, vias and pours are added in the syntax this
KiCad reads: nets by number from the board's own net table, `tstamp` for
KiCad 7, and the fields an older parser does not know left out. Footprint
placement is read from the result as text. No pcbnew here.
"""

import re

# the newest board file each KiCad reads (pcbnew SEXPR_BOARD_FILE_VERSION at each release)
MAX_VERSION = {6: 20211014, 7: 20221018, 8: 20240108, 9: 20241229}

COPPER = ("segment", "arc", "via", "zone")
KEEP = {
    "segment": {"start", "end", "width", "layer", "net", "uuid", "tstamp", "locked"},
    "arc": {"start", "mid", "end", "width", "layer", "net", "uuid", "tstamp", "locked"},
    "via": {"at", "size", "drill", "layers", "net", "uuid", "tstamp", "locked", "free", "remove_unused_layers",
            "keep_end_layers"},
    "zone": {"net", "net_name", "layer", "layers", "uuid", "tstamp", "name", "hatch", "priority", "connect_pads",
             "min_thickness", "filled_areas_thickness", "fill", "keepout", "polygon"},
}
# bare words a via may carry (its type)
VIA_FLAGS = {"blind", "micro", "through", "buried"}


def file_version(text):
    m = re.search(r"\(version\s+(\d+)\)", text[:2000])
    return int(m.group(1)) if m else 0


def needs_downgrade(text, kicad_major):
    """Whether this KiCad (major version) cannot read the board file `text`."""
    top = MAX_VERSION.get(kicad_major)
    return top is not None and file_version(text) > top


# -- s-expressions -------------------------------------------------------------

_TOKEN = re.compile(r'\s*(?:(\()|(\))|("(?:[^"\\]|\\.)*")|([^\s()"]+))', re.S)


def parse(text):
    """Nested lists of raw atoms (strings keep their quotes, so writing back is exact)."""
    stack = [[]]
    for m in _TOKEN.finditer(text):
        if m.group(1):
            stack.append([])
        elif m.group(2):
            node = stack.pop()
            stack[-1].append(node)
        elif m.group(3) is not None:
            stack[-1].append(m.group(3))
        elif m.group(4) is not None:
            stack[-1].append(m.group(4))
    return stack[0][0] if stack[0] else []


def dump(node, indent=0):
    """An item back as text, its words and lists in their order."""
    if not isinstance(node, list):
        return node
    out = "(" + str(node[0])
    for x in node[1:]:
        out += ("\n" + "\t" * (indent + 1) + dump(x, indent + 1)) if isinstance(x, list) else " " + x
    return out + ")"


def _unquote(atom):
    return atom[1:-1].replace('\\"', '"') if atom.startswith('"') else atom


def _quote(s):
    return '"' + s.replace('"', '\\"') + '"'


def _child(node, name):
    return next((x for x in node[1:] if isinstance(x, list) and x and x[0] == name), None)


# -- the conversion ----------------------------------------------------------------


def _net_tables(board):
    """name -> number of the board's own net table, and number -> name of the result's (if it has one)."""
    numbers, names = {}, {}
    for x in board[1:]:
        if isinstance(x, list) and x and x[0] == "net" and len(x) >= 3:
            numbers[_unquote(x[2])] = x[1]
            names[x[1]] = _unquote(x[2])
    return numbers, names


def _net_name(item, result_names):
    """An item's net by name, from (net "GND"), (net 3) with the result's table, or (net_name ...)."""
    n = _child(item, "net")
    if n is not None and len(n) >= 2:
        if n[1].startswith('"'):
            return _unquote(n[1])
        if len(n) >= 3:
            return _unquote(n[2])
        if n[1] in result_names:
            return result_names[n[1]]
    nn = _child(item, "net_name")
    return _unquote(nn[1]) if nn is not None and len(nn) >= 2 else None


def _rewrite(item, numbers, result_names, kicad_major):
    """The item in this KiCad's syntax, or None when its net is not on the board."""
    kind = item[0]
    name = _net_name(item, result_names)
    if name is None or name not in numbers:
        return None
    keep = KEEP[kind]
    out = [kind]
    for x in item[1:]:
        if not isinstance(x, list):
            if kind == "via" and x in VIA_FLAGS and x != "through":
                out.append(x)
            continue
        if not x or x[0] not in keep:
            continue
        if x[0] == "net":
            out.append(["net", numbers[name]])
            if kind == "zone":
                out.append(["net_name", _quote(name)])
            continue
        if x[0] == "net_name":
            continue
        if x[0] == "uuid" and kicad_major <= 7:
            x = ["tstamp", x[1]]
        if x[0] == "locked" and kicad_major <= 7:
            # KiCad 7 writes a bare `locked` word, not (locked yes)
            if len(x) > 1 and x[1] == "yes":
                out.insert(1, "locked")
            continue
        out.append(x)
    return out


def _top_level_spans(text):
    """(start, end, head word) of each item directly inside (kicad_pcb ...), in order."""
    spans, depth, start, i, n = [], 0, None, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == "(":
            depth += 1
            if depth == 2:
                start = i
        elif ch == ")":
            if depth == 2 and start is not None:
                m = re.match(r"\(\s*([^\s()]+)", text[start:start + 64])
                spans.append((start, i + 1, m.group(1) if m else ""))
                start = None
            depth -= 1
        i += 1
    return spans


def merge_copper(board_text, result_text, kicad_major):
    """The board as it is in the editor, with the result's copper instead of its own.

    The board's text is kept as KiCad wrote it, byte for byte, except its own
    tracks, arcs, vias and pours, which are cut out; the result's are added at
    the end in this KiCad's syntax. Footprints, outline, rules and text are the
    board's: KiCad reads the file because KiCad wrote it.
    """
    numbers, _ = _net_tables(parse(board_text))
    result = parse(result_text)
    _, result_names = _net_tables(result)
    pieces, last = [], 0
    for start, end, head in _top_level_spans(board_text):
        if head in COPPER and not (head == "zone" and "(keepout" in board_text[start:end]):
            pieces.append(board_text[last:start])
            last = end
    body = "".join(pieces) + board_text[last:]
    close = body.rstrip().rfind(")")
    added = []
    for x in result[1:]:
        if isinstance(x, list) and x and x[0] in COPPER and not _is_rule_area(x):
            item = _rewrite(x, numbers, result_names, kicad_major)
            if item is not None:
                added.append("\t" + dump(item, 1))
    return body[:close].rstrip() + "\n" + "\n".join(added) + "\n)\n"


def _is_rule_area(zone):
    return zone[0] == "zone" and _child(zone, "keepout") is not None


def placements(result_text):
    """Footprint positions in the result: [{uuid, ref, x, y, rot, layer}] (mm, degrees)."""
    out = []
    for x in parse(result_text)[1:]:
        if not (isinstance(x, list) and x and x[0] == "footprint"):
            continue
        at = _child(x, "at")
        layer = _child(x, "layer")
        uuid = _child(x, "uuid") or _child(x, "tstamp")
        ref = None
        for p in x[1:]:
            if isinstance(p, list) and p and p[0] == "property" and len(p) >= 3 and _unquote(p[1]) == "Reference":
                ref = _unquote(p[2])
            elif isinstance(p, list) and p and p[0] == "fp_text" and len(p) >= 3 and p[1] == "reference":
                ref = _unquote(p[2])
        if at is None:
            continue
        out.append({
            "uuid": _unquote(uuid[1]) if uuid else None, "ref": ref,
            "x": float(at[1]), "y": float(at[2]), "rot": float(at[3]) if len(at) > 3 else 0.0,
            "layer": _unquote(layer[1]) if layer else "F.Cu",
        })
    return out
