"""Log the bridge in to MAX as a device of its own, approved by QR.

A token copied from the user's browser does not survive: MAX rotates it once
another device presents it, and when the browser presents its old copy again
MAX revokes the whole chain, logging out both the bridge and web.max.ru
(2026-10-09). A QR login gives the bridge a session the browser never touches.
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable

import aiohttp

from .max_client import MaxClient, OpCode, WS_HEADERS, web_handshake_payload

log = logging.getLogger("max_bridge.qr_login")

QR_ATTEMPTS = 5
RESPONSE_TIMEOUT = 30

Call = Callable[[int, dict], Awaitable[tuple[int, dict]]]
OnQr = Callable[[str], Awaitable[None]]


class QrLoginError(Exception):
    """MAX refused the QR login."""


class QrExpiredError(QrLoginError):
    """Nobody approved the QR codes before they expired."""


def _error(payload: dict, fallback: str) -> QrLoginError:
    return QrLoginError(payload.get("message") or payload.get("error") or fallback)


async def run_qr_flow(call: Call, on_qr: OnQr, attempts: int = QR_ATTEMPTS) -> str:
    """Show QR codes until one is approved in the MAX app; return the login token.

    Each QR lives about two minutes, so an expired one is replaced by a fresh
    one up to `attempts` times.
    """
    for _ in range(attempts):
        cmd, qr = await call(OpCode.GET_QR, {})
        if cmd != 1:
            raise _error(qr, "QR request failed")
        await on_qr(qr["qrLink"])

        deadline = time.monotonic() + qr.get("ttl", 120_000) / 1000
        interval = qr.get("pollingInterval", 5000) / 1000
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            cmd, status = await call(OpCode.GET_QR_STATUS, {"trackId": qr["trackId"]})
            if cmd != 1:
                # MAX drops the track at expiry, a poll or so before the ttl
                # runs out on our side, and answers "invalid trackId"
                log.info(f"QR status check failed: {status.get('message') or status.get('error')}")
                break
            if not status.get("status", {}).get("loginAvailable"):
                continue

            cmd, result = await call(OpCode.LOGIN_BY_QR, {"trackId": qr["trackId"]})
            if cmd != 1:
                raise _error(result, "QR login failed")
            token = (result.get("tokenAttrs", {}).get("LOGIN") or {}).get("token")
            if token:
                return token
            if result.get("passwordChallenge"):
                raise QrLoginError("MAX asks for the 2FA password, which the bridge does not support")
            raise QrLoginError("MAX returned no login token")

        log.info("QR expired without approval")

    raise QrExpiredError("QR expired")


async def qr_login(device_id: str, on_qr: OnQr, attempts: int = QR_ATTEMPTS) -> str:
    """Open a MAX connection as `device_id` and run the QR flow over it."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            MaxClient.WS_URL, headers=WS_HEADERS, heartbeat=MaxClient.WS_PING_INTERVAL,
        ) as ws:
            seq = 0

            async def call(opcode: int, payload: dict) -> tuple[int, dict]:
                nonlocal seq
                my_seq, seq = seq, seq + 1
                await ws.send_json({
                    "ver": 11, "cmd": 0, "seq": my_seq,
                    "opcode": int(opcode), "payload": payload,
                })
                while True:
                    msg = await ws.receive(timeout=RESPONSE_TIMEOUT)
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        raise QrLoginError(f"MAX closed the connection (code={ws.close_code})")
                    frame = msg.json()
                    if frame.get("seq") == my_seq and frame.get("opcode") == int(opcode):
                        return frame.get("cmd"), frame.get("payload") or {}

            cmd, payload = await call(OpCode.HANDSHAKE, web_handshake_payload(device_id))
            if cmd != 1:
                raise _error(payload, "handshake failed")
            return await run_qr_flow(call, on_qr, attempts)
