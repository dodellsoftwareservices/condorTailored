from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from condor.web.auth import decode_jwt, extract_ws_token, watch_websocket_session
from condor.web.ws_manager import get_ws_manager

router = APIRouter()
log = logging.getLogger(__name__)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str | None = Query(default=None)):
    # Browser cookie or service-client subprotocol. URL tokens are ignored.
    auth_token, accept_subprotocol = extract_ws_token(ws, token)
    manager = get_ws_manager()
    conn = await manager.connect(ws, auth_token, subprotocol=accept_subprotocol)
    if conn is None:
        return

    watcher = asyncio.create_task(watch_websocket_session(ws, auth_token, decode_jwt))
    try:
        while True:
            raw = await ws.receive_text()
            if decode_jwt(auth_token) is None:
                await ws.close(code=4001, reason="Session expired")
                break
            try:
                await manager.handle_message(conn, raw)
            except WebSocketDisconnect:
                raise
            except Exception:
                # One malformed/failing message must not tear down the whole
                # multiplexed connection (all subscribed channels).
                log.exception("WS message error for user %s (ignored)", conn.user_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WS error for user %s", conn.user_id)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        manager.disconnect(conn)
