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

    def fetch(self, route):
        """Download one of the workspace's files into the project's .koppar folder."""
        data = self.client.download_result(self.design_id, route)
        name = route.replace("workspace.", f"{self.stem}.workspace.", 1)
        dest = os.path.join(project_work_dir(self.board_file), name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        return dest
