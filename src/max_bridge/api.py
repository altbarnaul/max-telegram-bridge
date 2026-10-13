"""HTTP API for Chrome extension pairing."""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from aiohttp import web

from .db import Database
from .crypto import TokenCrypto

log = logging.getLogger("max_bridge.api")

PAIRING_CODE_TTL = timedelta(minutes=5)


def _parse_created_at(value) -> datetime | None:
    """Parse a pairing code's created_at into an aware UTC datetime.

    SQLite writes CURRENT_TIMESTAMP in UTC without a timezone suffix, so a
    naive value is interpreted as UTC rather than as local time.
    """
    if isinstance(value, datetime):
        created = value
    elif isinstance(value, str):
        try:
            created = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if created.tzinfo is None:
        return created.replace(tzinfo=timezone.utc)
    return created.astimezone(timezone.utc)


def create_api_app(db: Database, crypto: TokenCrypto,
                   session_manager=None) -> web.Application:
    app = web.Application()
    app["db"] = db
    app["crypto"] = crypto
    app["session_manager"] = session_manager

    app.router.add_post("/api/pair", handle_pair)
    app.router.add_get("/api/health", handle_health)

    return app


async def handle_pair(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    crypto: TokenCrypto = request.app["crypto"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    code = body.get("code")
    token = body.get("token")

    if not all(isinstance(v, str) for v in (code, token)):
        return web.json_response(
            {"ok": False, "error": "Missing required fields: code, token"},
            status=400,
        )

    code = code.strip()
    token = token.strip()

    if not code or not token:
        return web.json_response(
            {"ok": False, "error": "Missing required fields: code, token"},
            status=400,
        )

    # The extension also sends the browser's deviceId, but MAX keeps one
    # session per device: logging in under it made every web.max.ru re-login
    # revoke the bridge's token (2026-10-09). A token is accepted from a new
    # device, so the bridge logs in as a device of its own.
    device_id = str(uuid.uuid4())

    # Validate pairing code
    pairing = await db.get_pairing_code(code)
    if not pairing:
        return web.json_response(
            {"ok": False, "error": "Invalid or expired pairing code"},
            status=400,
        )

    # Check TTL (created_at is stored in UTC by SQLite)
    created = _parse_created_at(pairing["created_at"])
    if created is None or datetime.now(timezone.utc) - created > PAIRING_CODE_TTL:
        await db.delete_pairing_code(code)
        return web.json_response(
            {"ok": False, "error": "Invalid or expired pairing code"},
            status=400,
        )

    telegram_id = pairing["telegram_id"]
    encrypted_token = crypto.encrypt(token)

    # Create or update user
    existing = await db.get_user_by_telegram_id(telegram_id)
    if existing:
        await db.update_user_token(existing["id"], encrypted_token, device_id)
        if await db.get_user_group(existing["id"]):
            # Re-pairing after MAX revoked the token: the group and topics
            # are still there, so resume forwarding right away instead of
            # parking the user in "pending" until a /setup that never comes.
            await db.update_user_status(existing["id"], "active")
            sm = request.app["session_manager"]
            if sm:
                await sm.stop_session(existing["id"])
                await sm.start_session(existing["id"])
        else:
            await db.update_user_status(existing["id"], "pending")
        log.info(f"Updated token for user {existing['id']} (TG:{telegram_id})")
    else:
        user_id = await db.create_user(
            telegram_id=telegram_id,
            max_token_encrypted=encrypted_token,
            max_device_id=device_id,
        )
        log.info(f"Created user {user_id} (TG:{telegram_id})")

    # Clean up pairing code
    await db.delete_pairing_code(code)

    return web.json_response({"ok": True, "message": "Token paired successfully"})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})
