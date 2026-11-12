"""The account side: the board as a Koppar workspace, and one tool run on it.

A workspace is a copy of the KiCad project on the server that the board tools
(the ones the MCP server and the CLI offer) work on, with a revision snapshot
before every change. Before each tool the board in the editor is pushed over
the workspace's, so a tool always works on what the user sees. No wx and no
pcbnew here: the window and scripts/board_tool.py share it.
"""

import base64
import os
import time

from .api import ApiError
from .settings import load_workspace, project_work_dir, save_workspace


class Workspace:
    def __init__(self, client, board_file):
        self.client = client
        self.board_file = board_file
        self.stem = os.path.splitext(os.path.basename(board_file))[0]
        self.record = load_workspace(board_file)
        if self.record and self.record.get("server") != client.server:
            # a workspace lives on one server, for one account
            self.record = None

    @property
    def design_id(self):
        return self.record["design_id"] if self.record else None

    def push(self, zip_bytes):
        """Create the workspace from the project zip, or replace its board, project
        and rules with the zip's. Returns the workspace record."""
        payload = base64.b64encode(zip_bytes).decode("ascii")
        if self.record:
            try:
                self.client.call_tool("add_project_files", {
                    "design_id": self.design_id, "zip_base64": payload, "overwrite": True})
                return self.record
            except ApiError as e:
                # deleted, another account's, or no longer a workspace: start a new one
                if e.status not in (403, 404, 409):
                    raise
        made = self.client.call_tool("import_kicad_project", {
            "zip_base64": payload,
            "board_file": f"{self.stem}.kicad_pcb",
            "title": f"{self.stem} (KiCad)",
            "brief": "Board workspace opened from the KiCad plugin.",
        })
        self.record = {
            "design_id": made["design_id"],
            "board_file": made.get("board_file"),
            "web_url": made.get("web_url"),
            "session_id": made.get("session_id"),
            "server": self.client.server,
            "created_at": time.time(),
        }
        save_workspace(self.board_file, self.record)
        return self.record

    def run(self, tool, args, on_progress=None, is_cancelled=None):
        """Run one board tool to its end. Returns its job view ({status, result, ...});
        a tool that fails raises ApiError with the server's reason."""
        view = self.client.call_tool(tool, {**args, "design_id": self.design_id})
        while view.get("status") in ("queued", "running"):
            if on_progress:
                on_progress(view.get("progress") or view.get("status"), view.get("elapsed_s"))
            if is_cancelled and is_cancelled():
                return view
            view = self.client.wait_board_job(self.design_id)
        if view.get("status") == "failed":
            raise ApiError(422, view.get("error") or f"{tool} failed", view)
        return view

    def follow(self, on_progress=None, is_cancelled=None):
        """Wait for the workspace's running job (one started before the window closed) to end."""
        view = self.client.wait_board_job(self.design_id)
        while view.get("status") in ("queued", "running"):
            if on_progress:
                on_progress(view.get("progress") or view.get("status"), view.get("elapsed_s"))
            if is_cancelled and is_cancelled():
                return view
            view = self.client.wait_board_job(self.design_id)
        if view.get("status") == "failed":
            raise ApiError(422, view.get("error") or "the job failed", view)
        return view

    def fetch(self, route):
        """Download one of the workspace's files into the project's .koppar folder."""
        data = self.client.download_result(self.design_id, route)
        name = route.replace("workspace.", f"{self.stem}.workspace.", 1)
        dest = os.path.join(project_work_dir(self.board_file), name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        return dest


def route_result(result):
    """A workspace route's result (route_board's pipeline) in the shape the Autoroute summary
    reads: {routing: {...}, drc: {errors, unconnected_items, errors_by_type}}."""
    r = result or {}
    p = r.get("pipeline") or {}
    after = p.get("after_route") or {}
    return {
        "routing": {
            "router": (r.get("route") or {}).get("router") or r.get("router"),
            "route_s": p.get("route_s"),
            "finish_s": p.get("finish_s"),
            "open_after_route": after.get("unconnected_items"),
            "copper_finished": p.get("copper_finishing") == "kept",
            "capped": bool(p.get("capped")),
        },
        "drc": {
            "errors": r.get("drc_errors"),
            "unconnected_items": r.get("unconnected_items"),
            "errors_by_type": r.get("errors_by_type") or {},
        },
        "outcome": r.get("outcome"),
    }


def apply_fixes(workspace, fixes, on_stage=None, is_cancelled=None):
    """The board check's fixes (precheck.fix_plan) on the workspace, before its route.

    The overlapping parts move, and only those (anneal_place with their refs). The copper where
    they were and where they now are is cleared (ripup_routing of every net in each part's box,
    0.3 mm around it): a part moved onto routed copper shorted it, and its old tracks led nowhere;
    the route makes those connections again. Then the stray vias and tracks the check named go."""
    say = on_stage or (lambda _text: None)
    move = fixes.get("move") or []
    if move:
        def boxes():
            view = workspace.run("get_board_info", {"refs": move}, is_cancelled=is_cancelled)
            feet = ((view.get("result") or {}).get("footprints")) or []
            return [f["box"] for f in feet if f.get("ref") in move and f.get("box")]

        before = boxes()
        say(f"Moving {', '.join(move[:6])}{' and more' if len(move) > 6 else ''} apart")
        workspace.run("anneal_place", {"refs": move}, is_cancelled=is_cancelled)
        say("Clearing the copper where the parts were and where they are now")
        margin = 0.3
        for b in before + boxes():
            workspace.run("ripup_routing", {"region": {
                "x_mm": b["x_mm"] - margin, "y_mm": b["y_mm"] - margin,
                "w_mm": b["w_mm"] + 2 * margin, "h_mm": b["h_mm"] + 2 * margin}}, is_cancelled=is_cancelled)
    for r in fixes.get("rip") or []:
        say(f"Removing the stray {r.get('kind', 'copper').lower()} of {r['net']}")
        workspace.run("ripup_routing", {"nets": [r["net"]], "region": {
            "x_mm": r["x_mm"] - 0.3, "y_mm": r["y_mm"] - 0.3, "w_mm": 0.6, "h_mm": 0.6}}, is_cancelled=is_cancelled)
