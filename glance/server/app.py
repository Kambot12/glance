"""Local FastAPI server + token-gated WebSocket.

Security posture (section 5 of the spec, with one change):
  * Binds 127.0.0.1 unless --lan.
  * A random per-session token is required on every WebSocket connection.
  * The token travels in the URL FRAGMENT (#t=...), not the query string.
    Fragments are never sent to the server, so the token stays out of access
    logs, Referer headers and proxy logs. The page reads it and presents it as
    a WebSocket subprotocol.
  * Origin is checked and fails CLOSED: a missing Origin is rejected too.
  * Model output crosses the wire as text and is rendered with textContent /
    createElement only. No innerHTML anywhere.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from glance.models import Event

log = logging.getLogger("glance.server")

SUBPROTOCOL = "glance.v1"
TOKEN_PREFIX = "token."
STATIC = Path(__file__).parent / "static"

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; connect-src 'self' ws: wss:; object-src 'none'; "
    "base-uri 'none'; form-action 'none'"
)


def extract_token(subprotocols: list[str] | None) -> str | None:
    for sp in subprotocols or []:
        if sp.startswith(TOKEN_PREFIX):
            return sp[len(TOKEN_PREFIX) :]
    return None


def origin_allowed(origin: str | None, allowed: set[str]) -> bool:
    # Fail closed: a non-browser client sends no Origin at all.
    if not origin:
        return False
    return origin in allowed


class Hub:
    """Fan-out to every connected web view."""

    def __init__(self) -> None:
        self._conns: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._conns.add(ws)

    async def discard(self, ws: WebSocket) -> None:
        async with self._lock:
            self._conns.discard(ws)

    @property
    def count(self) -> int:
        return len(self._conns)

    async def broadcast(self, payload: dict) -> None:
        async with self._lock:
            targets = list(self._conns)
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:  # client vanished mid-send
                await self.discard(ws)


def create_app(
    *,
    hub: Hub,
    inbox: "asyncio.Queue[Event]",
    token: str,
    allowed_origins: set[str],
    mode_names: list[str],
) -> FastAPI:
    app = FastAPI(title="glance", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/modes")
    async def modes() -> dict:
        return {"modes": mode_names}

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if not origin_allowed(origin, allowed_origins):
            log.warning("ws rejected: origin %r not allowed", origin)
            await websocket.close(code=1008)
            return
        supplied = extract_token(websocket.scope.get("subprotocols"))
        if not supplied or not hmac.compare_digest(supplied, token):
            log.warning("ws rejected: bad or missing token")
            await websocket.close(code=1008)
            return

        await websocket.accept(subprotocol=SUBPROTOCOL)
        await hub.add(websocket)
        log.info("web view connected (%d total)", hub.count)
        try:
            while True:
                msg = await websocket.receive_json()
                kind = msg.get("type")
                if kind == "followup":
                    text = (msg.get("text") or "").strip()
                    if text:
                        await inbox.put(Event(kind="followup", text=text, origin="ui"))
                elif kind == "trigger":
                    await inbox.put(
                        Event(
                            kind="mode",
                            mode=msg.get("mode"),
                            manual=True,
                            origin="ui",
                        )
                    )
                elif kind == "pause":
                    await inbox.put(Event(kind="pause", origin="ui"))
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.debug("ws closed: %s", type(e).__name__)
        finally:
            await hub.discard(websocket)
            log.info("web view disconnected (%d left)", hub.count)

    return app
