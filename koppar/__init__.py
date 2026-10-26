"""Koppar for KiCad: autoroute, place, finish, polish and check the open board."""

import os

try:
    # scripts/route_board.py imports the client and board modules from a plain Python
    if not os.environ.get("KOPPAR_NO_REGISTER"):
        from .plugin import ApplyAction, KopparAction

        KopparAction().register()
        ApplyAction().register()
except Exception as e:  # a broken plugin must never stop the PCB editor from opening
    import traceback

    print(f"[Koppar] plugin not loaded: {e}")
    traceback.print_exc()
