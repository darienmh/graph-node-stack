"""Tiny forwardAuth target for Traefik: validates an API key header.

Traefik calls this on every request to the protected router, forwarding the
client's original headers. We check `X-Api-Key` (or `Authorization: Bearer <k>`)
against the comma-separated keys in the API_KEYS env var. 2xx => allow, 401 => deny.
No third-party deps; stdlib only.
"""
import base64
import hmac
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

KEYS = [k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()]


def is_valid(key: str) -> bool:
    # constant-time compare against each configured key
    return bool(key) and any(hmac.compare_digest(key, k) for k in KEYS)


def candidate_keys(headers) -> list:
    """Collect possible keys from the supported auth schemes.

    - X-Api-Key: <key>             (apps doing queries)
    - Authorization: Bearer <key>
    - Authorization: Basic base64(user:pass)  -> graph-cli URL creds
      (key may sit in either the user or the pass field, e.g. https://x:KEY@host
      or https://KEY@host), so we try both.
    """
    out = []
    xk = headers.get("X-Api-Key", "")
    if xk:
        out.append(xk)
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        out.append(auth[7:])
    elif auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8", "replace")
            user, _, pw = decoded.partition(":")
            if pw:
                out.append(pw)
            if user:
                out.append(user)
        except Exception:
            pass
    return out


class Handler(BaseHTTPRequestHandler):
    def _check(self):
        ok = any(is_valid(k) for k in candidate_keys(self.headers))
        self.send_response(200 if ok else 401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _check
    do_POST = _check
    do_HEAD = _check
    do_PUT = _check
    do_DELETE = _check
    do_OPTIONS = _check

    def log_message(self, *args):  # silence access logs
        pass


if __name__ == "__main__":
    if not KEYS:
        raise SystemExit("API_KEYS env var is empty; refusing to start (would deny all).")
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
