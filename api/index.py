"""Vercel entrypoint: the whole FastAPI app runs as one Python function.

vercel.json rewrites every request to this function and passes the original path as `__p`.
Depending on the runtime, the path the function sees can be the rewrite destination
("/api/index"), so the shim below restores the original path before FastAPI routes it.
"""

import os
import sys
from urllib.parse import parse_qsl, unquote, urlencode

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from perpdesk.web.app import create_app

_app = create_app()


def _restore(scope: dict) -> dict:
    if scope.get("type") not in ("http", "websocket"):
        return scope
    path = scope.get("path") or "/"
    qs = (scope.get("query_string") or b"").decode("latin-1")
    pairs = parse_qsl(qs, keep_blank_values=True)
    original = next((v for k, v in pairs if k == "__p"), None)
    if original is not None:
        path = "/" + unquote(original).lstrip("/")
        qs = urlencode([(k, v) for k, v in pairs if k != "__p"])
    elif path == "/api/index" or path.startswith("/api/index/"):
        path = path[len("/api/index"):] or "/"
    scope = dict(scope)
    scope["path"] = path
    scope["raw_path"] = path.encode()
    scope["query_string"] = qs.encode("latin-1")
    return scope


async def app(scope, receive, send):  # type: ignore[no-untyped-def]
    await _app(_restore(scope), receive, send)
