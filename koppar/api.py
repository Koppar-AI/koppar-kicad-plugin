"""HTTP client for the Koppar tools API (tools.koppar.ai/api/tools/*).

Standard library only: KiCad's bundled Python is not guaranteed to have
`requests`, and nothing here may import pcbnew or wx, so the same client runs
inside KiCad and in scripts/route_board.py.
"""

import json
import platform
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

from .config import HTTP_TIMEOUT_S, VERSION

# how long one agent call holds for a board job (the server's ceiling)
WAIT_S = 55


class ApiError(Exception):
    """A request the server refused or that never reached it.

    `status` is the HTTP status (0 when there was no response), `detail` the
    server's message, `body` the parsed JSON body when there was one.
    """

    def __init__(self, status, detail, body=None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.body = body or {}


PEM_FOOTER = "-----END CERTIFICATE-----"
_ssl_context = None


def _load_macos_keychain(ctx):
    """OpenSSL cannot read the macOS keychain, the only place a corporate
    TLS-inspection root lives there, so the roots are exported by `security`."""
    try:
        pem = subprocess.run(
            ["/usr/bin/security", "find-certificate", "-a", "-p"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return
    try:
        ctx.load_verify_locations(cadata=pem)
        return
    except Exception:
        pass
    # one unparsable entry rejects the whole blob: load them one at a time
    for chunk in pem.split(PEM_FOOTER):
        if "BEGIN CERTIFICATE" in chunk:
            try:
                ctx.load_verify_locations(cadata=chunk + PEM_FOOTER + "\n")
            except Exception:
                pass


def ssl_context():
    """The OS trust store plus certifi when KiCad's Python has it.

    KiCad's bundled Python on macOS and Windows often has no CA bundle of its
    own, which makes every HTTPS call fail certificate verification.
    """
    global _ssl_context
    if _ssl_context is None:
        ctx = ssl.create_default_context()
        try:
            import certifi

            ctx.load_verify_locations(cafile=certifi.where())
        except Exception:
            pass
        if sys.platform == "darwin":
            _load_macos_keychain(ctx)
        _ssl_context = ctx
    return _ssl_context


def user_agent(kicad_version=None):
    kicad = f"KiCad {kicad_version}" if kicad_version else "KiCad"
    return f"KopparKiCadPlugin/{VERSION} ({kicad}; {platform.system()} {platform.machine()})"


class KopparClient:
    def __init__(self, server, api_key=None, kicad_version=None):
        self.server = server.rstrip("/")
        self.api_key = (api_key or "").strip() or None
        self.user_agent = user_agent(kicad_version)

    # -- transport ---------------------------------------------------------

    def _request(self, method, path, body=None, timeout=HTTP_TIMEOUT_S, raw=False):
        url = self.server + path
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        context = ssl_context() if url.startswith("https:") else None
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            raw_body = e.read()
            try:
                parsed = json.loads(raw_body.decode("utf-8"))
            except Exception:
                parsed = {}
            detail = parsed.get("detail") if isinstance(parsed, dict) else None
            raise ApiError(e.code, detail or f"HTTP {e.code} from {self.server}", parsed if isinstance(parsed, dict) else {})
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise ApiError(
                    0,
                    f"Could not verify the TLS certificate of {self.server}. The issuer is not in this "
                    "machine's certificate store (a company proxy that inspects TLS does this).",
                )
            raise ApiError(0, f"Could not reach {self.server}: {reason}")
        except (TimeoutError, OSError) as e:
            raise ApiError(0, f"Could not reach {self.server}: {e}")
        if raw:
            return payload
        try:
            return json.loads(payload.decode("utf-8"))
        except Exception:
            raise ApiError(0, f"{self.server} sent a response that is not JSON")

    # -- endpoints ---------------------------------------------------------

    def health(self):
        return self._request("GET", "/api/tools/health", timeout=15)

    def submit_route(self, board_text, project_text=None, rules_text=None,
                     preserve_existing=True, vias_in_pads=False):
        """Queue a route job. Returns {job_id, status, tool, estimate}.

        A board past the free size without an API key is refused with 402;
        its body carries the server's `estimate`.
        """
        body = {
            "content": board_text,
            "preserve_existing": bool(preserve_existing),
            "vias_in_pads": "allow" if vias_in_pads else "none",
        }
        if project_text:
            body["project"] = project_text
        if rules_text:
            body["rules"] = rules_text
        # a 20 MB upload on a slow uplink takes a while
        return self._request("POST", "/api/tools/route", body, timeout=300)

    def get_job(self, job_id):
        return self._request("GET", f"/api/tools/jobs/{job_id}", timeout=30)

    def cancel_job(self, job_id):
        return self._request("POST", f"/api/tools/jobs/{job_id}/cancel", {}, timeout=30)

    def download_artifact(self, job_id, name):
        return self._request("GET", f"/api/tools/jobs/{job_id}/artifact/{name}", timeout=300, raw=True)

    # -- account: the board workspace tools (/api/agent, the MCP server's tools over HTTP) --

    def agent_tools(self):
        """Every tool the account may call, with its JSON Schema. A 401 means the key is wrong."""
        return self._request("GET", "/api/agent/tools", timeout=30)["tools"]

    def call_tool(self, name, args):
        """One agent tool. Board tools hold the call up to their wait_s (55 s at most) and
        come back with status "running" past that: follow with wait_board_job."""
        return self._request("POST", f"/api/agent/tools/{name}", args, timeout=WAIT_S + 60)

    def wait_board_job(self, design_id):
        """The workspace's latest board job, after waiting up to WAIT_S for it to end."""
        return self.call_tool("get_board_job", {"design_id": design_id, "wait_s": WAIT_S})

    def account(self):
        """{user, quota: {remaining, unlimited, ...}, chat: {available}}: who the key is and what is left."""
        return self._request("GET", "/api/agent/account", timeout=30)

    def chat_history(self, design_id):
        return self._request("GET", f"/api/agent/workspaces/{design_id}/chat", timeout=30)

    def chat(self, design_id, content, on_event, is_cancelled=lambda: False):
        """One chat turn; `on_event(event)` for each server-sent event as it arrives
        (text deltas, tool steps, done, error). Runs until the stream ends."""
        url = f"{self.server}/api/agent/workspaces/{design_id}/chat"
        headers = {"User-Agent": self.user_agent, "Accept": "text/event-stream", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=json.dumps({"content": content}).encode("utf-8"), headers=headers, method="POST")
        context = ssl_context() if url.startswith("https:") else None
        try:
            resp = urllib.request.urlopen(req, timeout=WAIT_S * 20, context=context)
        except urllib.error.HTTPError as e:
            try:
                parsed = json.loads(e.read().decode("utf-8"))
            except Exception:
                parsed = {}
            raise ApiError(e.code, parsed.get("detail") or f"HTTP {e.code} from {self.server}", parsed)
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(0, f"Could not reach {self.server}: {getattr(e, 'reason', e)}")
        with resp:
            buf = ""
            while not is_cancelled():
                line = resp.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace")
                if line.startswith("data:"):
                    buf += line[5:].strip()
                elif not line.strip() and buf:
                    try:
                        on_event(json.loads(buf))
                    except ValueError:
                        pass
                    buf = ""

    def design_progress(self, design_id):
        """Where a new design stands: {status, stage, board, done, has_project, web_url}."""
        return self._request("GET", f"/api/agent/designs/{design_id}/progress", timeout=30)

    def download_result(self, design_id, route):
        return self._request("GET", f"/api/result/{design_id}/{route}", timeout=300, raw=True)
