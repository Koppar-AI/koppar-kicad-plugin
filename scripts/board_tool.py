"""Run one Koppar board tool on a board from the command line, with the plugin's own code.

    python3 scripts/board_tool.py board.kicad_pcb TOOL ['{"json": "args"}'] [-o out.kicad_pcb]
                                  [--server URL] [--api-key KEY]
    python3 scripts/board_tool.py --list [--server URL] [--api-key KEY]

Does what the Board Tools window does: pushes the board (with its .kicad_pro /
.kicad_dru) to the account workspace, runs the tool, then brings the result
back the way the tool calls for (catalog.py) and saves the board to -o
(default <board>.koppar.kicad_pcb), or prints the report, or saves the file.
The API key is --api-key, KOPPAR_API_KEY, the plugin's settings or the CLI's login.
"""

import argparse
import json
import os
import sys

os.environ["KOPPAR_NO_REGISTER"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pcbnew  # noqa: E402

from koppar import board_io, catalog  # noqa: E402
from koppar.api import ApiError, KopparClient  # noqa: E402
from koppar.config import ACCOUNT_SERVER  # noqa: E402
from koppar.settings import api_key_of, load_settings  # noqa: E402
from koppar.workspace import Workspace  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("board", nargs="?")
    ap.add_argument("tool", nargs="?")
    ap.add_argument("args", nargs="?", default="{}")
    ap.add_argument("-o", "--out")
    ap.add_argument("--server", default=os.environ.get("KOPPAR_URL", ACCOUNT_SERVER))
    ap.add_argument("--api-key", default=os.environ.get("KOPPAR_API_KEY") or api_key_of(load_settings()))
    ap.add_argument("--list", action="store_true", help="the tools the plugin offers to this account")
    a = ap.parse_args()

    client = KopparClient(a.server, a.api_key, board_io.kicad_version())
    if a.list:
        for group, entries in catalog.offered(client.agent_tools()):
            print(group)
            for task, tool in entries:
                print(f"  {task.tool or '(autoroute)':<20} {task.mode:<30} {task.title}")
        return 0
    if not (a.board and a.tool):
        ap.error("give the board and the tool (or --list)")

    args = json.loads(a.args)
    board_file = os.path.abspath(a.board)
    board = pcbnew.LoadBoard(board_file)
    text = board_io.snapshot_board(board, os.path.join(os.path.dirname(board_file), ".koppar", "sent.kicad_pcb"))
    ws = Workspace(client, board_file)
    try:
        record = ws.push(board_io.project_zip(board_file, text))
        print(f"workspace {record['design_id']} ({record.get('web_url')})")
        view = ws.run(a.tool, args, on_progress=lambda p, s: print(f"  [{s} s] {p}", flush=True))
    except ApiError as e:
        print(f"{a.tool} failed ({e.status}): {e.detail}")
        return 1
    print(json.dumps(view.get("result"), indent=2)[:4000])

    mode = catalog.mode_of(a.tool, args)
    if mode == "report":
        return 0
    if mode.startswith("download:"):
        print(f"saved {ws.fetch(mode.split(':', 1)[1])}")
        return 0
    path = ws.fetch("workspace.kicad_pcb")
    stats = board_io.apply_result(board, path, mode)
    print(f"applied ({mode}): {stats}")
    after = board_io.net_census(board)
    print(f"now connected: {sum(n['routed'] for n in after)} of {len(after)} nets")
    out = os.path.abspath(a.out or os.path.splitext(board_file)[0] + ".koppar.kicad_pcb")
    pcbnew.SaveBoard(out, board, True)
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
