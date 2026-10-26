"""Constants for the Koppar plugin."""

import os

VERSION = "0.1.0"
# the package identifier in KiCad's Plugin and Content Manager
PACKAGE_ID = "ai.koppar.kicad"

DEFAULT_SERVER = "https://tools.koppar.ai"
# the account API (board workspace tools) is served from the app's host
ACCOUNT_SERVER = "https://app.koppar.ai"
# build_package.py sets this in the package: a release talks to Koppar's servers only
PACKAGED = False


def servers():
    """(tools server, account server). From a source checkout, KOPPAR_SERVER points both at
    another server (a local one, while developing); a packaged plugin ignores it."""
    dev = None if PACKAGED else os.environ.get("KOPPAR_SERVER", "").strip().rstrip("/")
    return (dev, dev) if dev else (DEFAULT_SERVER, ACCOUNT_SERVER)
# where an account API key is created (Settings > API keys); `ref` records the account as
# one the plugin brought in, when it is made from this link
API_KEYS_URL = "https://app.koppar.ai/settings?ref=kicad-plugin"
HOMEPAGE_URL = "https://koppar.ai"

# the largest board file the server takes
MAX_BOARD_BYTES = 20 * 1024 * 1024

POLL_INTERVAL_S = 3
HTTP_TIMEOUT_S = 60

# the routed board the route tool leaves among its artifacts
ROUTED_ARTIFACT = "circuit.kicad_pcb"

# per-project working folder: the session record and the downloaded results
PROJECT_DIR_NAME = ".koppar"
