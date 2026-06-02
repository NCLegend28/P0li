"""
Dashboard service — standalone FastAPI app.

Consumes the scanner's WebSocket state feed and re-broadcasts it to
connected browser clients.  Runs independently of the weather bot so
either service can be restarted without affecting the other.

Communication flow:
    weather-bot:8765/ws  →  _consume_scanner()  →  _latest_state
                                                          ↓
    browser  ←  ws://dashboard-host:8766/ws  ←  websocket_endpoint()

Usage (handled by cli.py run_dashboard entry point):
    SCANNER_WS_URL=ws://weather-bot:8765/ws dashboard
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from polybot.config import settings
from polybot.web.server import _DASHBOARD_HTML  # reuse the existing browser UI

app = FastAPI(title="Polybot Dashboard Service")

_connections: set[WebSocket] = set()
_latest_state: dict | None = None
_scanner_connected: bool = False


# ─── WebSocket endpoint (browser → dashboard service) ─────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _connections.add(ws)
    logger.info(f"Browser connected. Active connections: {len(_connections)}")
    try:
        while True:
            if _latest_state is not None:
                await ws.send_text(json.dumps(_latest_state))
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass
    except WebSocketDisconnect:
        pass
    except (RuntimeError, OSError) as e:
        logger.debug(f"WebSocket send error: {e}")
    finally:
        _connections.discard(ws)
        logger.info(f"Browser disconnected. Active connections: {len(_connections)}")


# ─── Scanner consumer (dashboard service → weather bot) ───────────────────────

async def _consume_scanner() -> None:
    """
    Connects to the scanner's WebSocket and keeps _latest_state current.
    Retries indefinitely with exponential back-off so a scanner restart
    is handled transparently.
    """
    global _latest_state, _scanner_connected
    retry = 0
    while True:
        url = settings.scanner_ws_url
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                logger.info(f"Connected to scanner at {url}")
                _scanner_connected = True
                retry = 0
                async for message in ws:
                    _latest_state = json.loads(message)
        except Exception as e:
            _scanner_connected = False
            wait = min(60, 5 * (2 ** min(retry, 5)))
            logger.warning(f"Scanner connection lost ({e.__class__.__name__}): {e}. Retry in {wait}s")
            retry += 1
            await asyncio.sleep(wait)


# ─── Health probe ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scanner_connected": _scanner_connected,
        "scanner_url": settings.scanner_ws_url,
        "browser_connections": len(_connections),
    })


# ─── Web dashboard ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    # The HTML uses `location.host` to build the WebSocket URL dynamically,
    # so it will connect to ws://dashboard-host:8766/ws automatically.
    return _DASHBOARD_HTML


# ─── Server runner ─────────────────────────────────────────────────────────────

async def run_dashboard_server(host: str = "0.0.0.0", port: int = 8766) -> None:
    import uvicorn

    asyncio.create_task(_consume_scanner(), name="scanner_consumer")

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info(f"Dashboard service → http://{host}:{port}  (scanner: {settings.scanner_ws_url})")
    await server.serve()
