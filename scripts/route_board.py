"""Route a board with Koppar from the command line, with the plugin's own code.

    python3 scripts/route_board.py board.kicad_pcb [-o routed.kicad_pcb] [--server URL] [--api-key KEY]
                                   [--nets unrouted|all|NAME,NAME] [--rip-existing] [--vias-in-pads]
                                   [--no-zones] [--cancel-after SECONDS]

Needs a Python that imports KiCad's pcbnew (KiCad 8+). It does what the
plugin does: snapshot the board, send it with its .kicad_pro / .kicad_dru,
follow the job, download the routed board and bring the chosen nets' copper
into the board, then saves the result to -o (default <board>.koppar.kicad_pcb).
--cancel-after stops the job after that many seconds (checks the Stop path).
"""

import argparse
import os
import sys
import tempfile
import time

os.environ["KOPPAR_NO_REGISTER"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pcbnew  # noqa: E402

from koppar import board_io  # noqa: E402
from koppar.api import ApiError, KopparClient  # noqa: E402
from koppar.config import DEFAULT_SERVER, POLL_INTERVAL_S, ROUTED_ARTIFACT  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("board")
    ap.add_argument("-o", "--out")
    ap.add_argument("--server", default=os.environ.get("KOPPAR_SERVER", DEFAULT_SERVER))
    ap.add_argument("--api-key", default=os.environ.get("KOPPAR_API_KEY"))
    ap.add_argument("--nets", default="unrouted")
    ap.add_argument("--rip-existing", action="store_true", help="route from scratch instead of keeping the copper on the board")
    ap.add_argument("--vias-in-pads", action="store_true")
    ap.add_argument("--no-zones", action="store_true")
    ap.add_argument("--cancel-after", type=float)
    a = ap.parse_args()

    board_file = os.path.abspath(a.board)
    out = os.path.abspath(a.out or os.path.splitext(board_file)[0] + ".koppar.kicad_pcb")
    board = pcbnew.LoadBoard(board_file)
    census = board_io.net_census(board)
    if a.nets == "all":
        nets = [n["name"] for n in census]
    elif a.nets == "unrouted":
        nets = [n["name"] for n in census if not n["routed"]]
    else:
        nets = [n.strip() for n in a.nets.split(",") if n.strip()]
    print(f"{len(census)} nets, {sum(not n['routed'] for n in census)} not connected; bringing back {len(nets)}")

    work = tempfile.mkdtemp(prefix="koppar-")
    text = board_io.snapshot_board(board, os.path.join(work, "sent.kicad_pcb"))
    extra = board_io.project_files(board_file)
    client = KopparClient(a.server, a.api_key, board_io.kicad_version())
    try:
        sub = client.submit_route(text, extra.get("project"), extra.get("rules"),
                                  preserve_existing=not a.rip_existing, vias_in_pads=a.vias_in_pads)
    except ApiError as e:
        print(f"not submitted ({e.status}): {e.detail}")
        return 2
    job_id = sub["job_id"]
    print(f"job {job_id} ({sub.get('estimate')})")

    t0 = time.time()
    last = None
    while True:
        job = client.get_job(job_id)
        line = f"{job['status']}: {job.get('progress')}"
        if line != last:
            print(f"[{time.time() - t0:6.0f} s] {line}")
            last = line
        if job["status"] in ("completed", "failed"):
            break
        if a.cancel_after is not None and time.time() - t0 > a.cancel_after:
            job = client.cancel_job(job_id)
            print(f"[{time.time() - t0:6.0f} s] stop sent: {job['status']} cancelled={job.get('cancelled')}")
            a.cancel_after = None
        time.sleep(POLL_INTERVAL_S)

    if job["status"] != "completed":
        print(f"job ended {job['status']}: {job.get('error')} (cancelled={job.get('cancelled')})")
        return 1
    routing = (job.get("result") or {}).get("routing") or {}
    drc = (job.get("result") or {}).get("drc") or {}
    print(f"server: router={routing.get('router')} route_s={routing.get('route_s')} "
          f"drc errors={drc.get('errors')} unconnected={drc.get('unconnected_items')}")

    routed = os.path.join(work, "routed.kicad_pcb")
    with open(routed, "wb") as f:
        f.write(client.download_artifact(job_id, ROUTED_ARTIFACT))
    stats = board_io.apply_routed(board, routed, nets, import_zones=not a.no_zones)
    print(f"applied: {stats}")
    after = board_io.net_census(board)
    print(f"now connected: {sum(n['routed'] for n in after)} of {len(after)} nets")
    pcbnew.SaveBoard(out, board, True)
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
