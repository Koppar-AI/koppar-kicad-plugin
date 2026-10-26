"""The tasks the Koppar window offers, in the words a board designer uses.

A task is one server tool (or the anonymous Autoroute) with a title, a line
that says what it does, the button's verb, a few options with real labels,
and how its result comes back:

    copper     the board's tracks, vias and new pours are replaced by the workspace's
    placement  footprint positions, rotations and sides, then the copper
    report     nothing changes on the board: the result is shown
    download   a file (e.g. STEP) is saved beside the board

Every other option the server publishes for the tool is under Advanced, built
from its JSON Schema, so nothing the MCP server or the CLI can do is out of
reach. Left out on purpose: what KiCad does by hand as well (outline,
keepouts, zones, exact placement, silkscreen, footprint swaps, design rules:
the plugin cannot write the open project's rules), and the design/BOM/order
tools, which need a design with a netlist rather than a board.
"""

from collections import namedtuple

# kind: nets | toggle | minutes
#   nets: default scope "all", "open", "selected" or "power"; arg None means "which nets come back"
#   toggle: sends `extra` (default true) when on and nothing when off, so the server's default applies
#   minutes: a time budget in minutes, sent in seconds; extra = (min, max)
Option = namedtuple("Option", "kind arg label default extra", defaults=(None,))
Task = namedtuple("Task", "id group title subtitle verb tool mode account options running")

ROUTE = "Route"
PLACE = "Place"
IMPROVE = "Improve the copper"
CHECK = "Check"

TASKS = [
    Task("autoroute", ROUTE, "Autoroute",
         "Route the open connections with Koppar's learned router, then finish and polish the copper and "
         "check it with KiCad's DRC. Small boards need no account.",
         "Route", None, "copper", False, [
             Option("nets", None, "Bring back", "open"),
             Option("toggle", "preserve_existing", "Keep the copper already on the board and route around it", True),
             Option("toggle", "vias_in_pads", "Allow vias in pads (the board is ordered with filled vias)", False),
             Option("toggle", "import_zones", "Bring back copper pours the router adds", True),
         ], "Routing"),
    Task("route_nets", ROUTE, "Route selected nets",
         "Route only the nets you pick, around everything already on the board.",
         "Route", "route_board", "copper", True, [
             Option("nets", "nets", "Nets", "selected"),
             Option("toggle", "fewest_vias", "Fewest vias (slower)", False),
         ], "Routing"),
    Task("finish", ROUTE, "Finish open nets",
         "Close the connections still open on a routed board, leaving the rest of the copper alone.",
         "Finish", "finish_board", "copper", True, [
             Option("minutes", "time_budget_s", "Time budget", 5, (1, 20)),
             Option("toggle", "vias_in_pads", "Allow vias in pads (filled vias)", False, "allow"),
         ], "Finishing"),
    Task("repair", ROUTE, "Repair broken connections",
         "Re-route only what is broken, for example after moving a few parts.",
         "Repair", "repair_board", "copper", True, [
             Option("minutes", "time_budget_s", "Time budget", 2, (1, 15)),
         ], "Repairing"),
    Task("ripup", ROUTE, "Rip up routing",
         "Remove tracks and vias, from the whole board or from the nets you pick. Locked copper stays.",
         "Rip up", "ripup_routing", "copper", True, [
             Option("nets", "nets", "Nets", "all"),
         ], "Ripping up"),
    Task("auto_place", PLACE, "Auto-place",
         "Place the parts by connectivity: connectors on the edges, decoupling beside its IC, nothing overlapping. "
         "Locked parts stay where they are.",
         "Place", "auto_place", "placement", True, [
             Option("toggle", "two_sided", "Use both sides of the board", False),
             Option("toggle", "refine", "Refine the placement afterwards (annealing)", True, "anneal"),
         ], "Placing"),
    Task("anneal", PLACE, "Refine a dense placement",
         "Nudge and swap parts to shorten connections and open routing room, keeping your arrangement.",
         "Refine", "anneal_place", "placement", True, [
             Option("toggle", "bottom_any", "Small SMD parts may go on the bottom", False),
         ], "Refining"),
    Task("polish_vias", IMPROVE, "Fewer vias",
         "Re-route the nets with the most vias and keep only the changes KiCad scores as better.",
         "Polish", "polish_vias", "copper", True, [
             Option("minutes", "total_budget_s", "Time budget", 5, (1, 20)),
         ], "Polishing"),
    Task("tidy", IMPROVE, "Tidy the copper",
         "Straighten jogs, merge segments and clean corners without changing what connects to what.",
         "Tidy", "tidy_copper", "copper", True, [], "Tidying"),
    Task("couple_pairs", IMPROVE, "Couple differential pairs",
         "Route each pair's two legs side by side at a constant gap, with matched lengths.",
         "Couple", "couple_pairs", "copper", True, [
             Option("minutes", "total_budget_s", "Time budget", 5, (1, 20)),
         ], "Coupling"),
    Task("widen", IMPROVE, "Widen power nets",
         "Bring power tracks up to their net class width wherever there is room.",
         "Widen", "widen_nets", "copper", True, [
             Option("nets", "nets", "Nets", "power"),
         ], "Widening"),
    Task("prune_vias", IMPROVE, "Remove redundant vias",
         "Delete vias whose connection is already made another way.",
         "Prune", "prune_vias", "copper", True, [], "Pruning"),
    Task("bridge", IMPROVE, "Join pour islands",
         "Stitch isolated copper pour islands back to their net with vias.",
         "Join", "bridge_islands", "copper", True, [], "Joining"),
    Task("tie", IMPROVE, "Tie plane pads",
         "Short tracks from ground and power pads to the pour where thermal reliefs cannot reach.",
         "Tie", "tie_rail_pads", "copper", True, [], "Tying"),
    Task("cleanup", IMPROVE, "Clean up",
         "Remove dangling tracks, duplicate segments and vias that connect nothing.",
         "Clean up", "cleanup_board", "copper", True, [], "Cleaning up"),
    Task("physics", CHECK, "Physics check",
         "Impedance, current capacity and skew of the nets that need them.",
         "Check", "check_physics", "report", True, [
             Option("toggle", "repair", "Fix what can be fixed (changes the copper)", False),
         ], "Checking"),
    Task("net_rules", CHECK, "Net classes and pairs",
         "Which nets have no class, and which differential pairs are not set up right.",
         "Check", "check_net_rules", "report", True, [], "Checking"),
    Task("diagnose", CHECK, "Why nets do not route",
         "For each open connection, what is in the way.",
         "Diagnose", "diagnose_open_nets", "report", True, [], "Diagnosing"),
]

GROUPS = [ROUTE, PLACE, IMPROVE, CHECK]

# arguments the plugin fills itself
HIDDEN_ARGS = {"design_id", "wait_s"}


def looks_like_power(name):
    """A rail by its name: GND, VCC, +3V3, VBUS, 5V, VDD_IO..."""
    import re

    n = name.rstrip("/").split("/")[-1].upper()  # the last sheet path segment
    return bool(n.startswith("+") or re.match(r"^(A|D|P)?GND|^V(CC|DD|SS|BUS|BAT|IN|SYS|REF)|^\d+V\d*|PWR|VBAT", n))


def task(task_id):
    return next(t for t in TASKS if t.id == task_id)


def mode_of(tool, args):
    """How the result of `tool` called with `args` comes back."""
    if tool == "check_physics" and args.get("repair"):
        return "copper"
    for t in TASKS:
        if t.tool == tool:
            return t.mode
    return "report"


def offered(listing):
    """[(group, [(task, server tool dict or None)])]: every task, with the server's
    tool when the account has it (None for Autoroute, and when the account cannot)."""
    by_name = {t["name"]: t for t in (listing or [])}
    out = []
    for group in GROUPS:
        entries = [(t, by_name.get(t.tool)) for t in TASKS if t.group == group and (t.tool is None or t.tool in by_name or not listing)]
        if entries:
            out.append((group, entries))
    return out


# the sidebar's icon for each task (lucide, assets/icons)
ICONS = {
    "autoroute": "route", "route_nets": "spline", "finish": "circle-check-big", "repair": "wrench",
    "ripup": "eraser", "auto_place": "layout-grid", "anneal": "move", "polish_vias": "circle-dot",
    "tidy": "paintbrush", "couple_pairs": "git-compare-arrows", "widen": "move-horizontal",
    "prune_vias": "scissors", "bridge": "link", "tie": "anchor", "cleanup": "trash-2",
    "physics": "zap", "net_rules": "list-checks", "diagnose": "search",
}

# what the assistant is doing, for each board tool it may call
TOOL_LABELS = {
    "get_board_info": "Reading the board", "get_board_job": "Waiting for the board job",
    "route_board": "Routing", "finish_board": "Finishing open nets", "repair_board": "Repairing connections",
    "ripup_routing": "Ripping up routing", "auto_place": "Placing parts", "anneal_place": "Refining the placement",
    "place_footprints": "Moving parts", "polish_vias": "Removing vias", "tidy_copper": "Tidying the copper",
    "couple_pairs": "Coupling differential pairs", "widen_nets": "Widening power nets",
    "prune_vias": "Removing redundant vias", "bridge_islands": "Joining pour islands",
    "tie_rail_pads": "Tying plane pads", "cleanup_board": "Cleaning up", "run_drc": "Checking the design rules",
    "check_physics": "Checking the physics", "check_net_rules": "Checking net classes and pairs",
    "diagnose_open_nets": "Finding what blocks the open nets",
    "render_board": "Rendering the board", "export_step": "Exporting STEP",
    "start_new_design": "Starting a new design",
    "set_board_outline": "Setting the outline", "set_design_rules": "Setting the design rules",
    "set_keepouts": "Editing keepouts", "set_zones": "Editing copper pours", "add_copper": "Adding copper",
    "set_silkscreen": "Tidying the silkscreen", "stamp_silkscreen": "Stamping the silkscreen",
    "swap_footprint": "Swapping footprints", "fill_zones": "Filling the pours",
    "list_board_revisions": "Looking at the history", "restore_board_revision": "Undoing a change",
}
# the tools that move parts: their result comes back as placement, then copper
PLACEMENT_TOOLS = {"auto_place", "anneal_place", "place_footprints", "swap_footprint"}


def tool_label(name):
    return TOOL_LABELS.get(name, name.replace("_", " ").capitalize())
