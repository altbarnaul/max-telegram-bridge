"""Tests for HTTP pairing API."""
import json
import os
import time
import uuid
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from aiohttp import web

from max_bridge.api import create_api_app


def sqlite_timestamp(offset: timedelta = timedelta()) -> str:
    """Reproduce what SQLite's CURRENT_TIMESTAMP writes: naive UTC."""
    return (datetime.now(timezone.utc) + offset).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def moscow_tz():
    """Run the process in the owner's timezone (UTC+3), as a deploy may."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Moscow"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.fixture
def deps():
    db = AsyncMock()
    crypto = MagicMock()
    crypto.encrypt = MagicMock(return_value="encrypted_token")
    session_manager = AsyncMock()
    return db, crypto, session_manager


@pytest.fixture
def app(deps):
    db, crypto, sm = deps
    return create_api_app(db=db, crypto=crypto, session_manager=sm)


class TestPairEndpoint:
    @pytest.mark.asyncio
    async def test_pair_success(self, aiohttp_client, deps, app):
        db, crypto, sm = deps
        now = sqlite_timestamp()
        db.get_pairing_code = AsyncMock(return_value={
            "code": "123456",
            "telegram_id": 111111111,
            "created_at": now,
        })
        db.get_user_by_telegram_id = AsyncMock(return_value=None)

        client = await aiohttp_client(app)
        resp = await client.post("/api/pair", json={
            "code": "123456",
            "token": "max_token_value",
            "device_id": "device123",
        })

        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        db.create_user.assert_called_once()
        db.delete_pairing_code.assert_called_once_with("123456")

    @pytest.mark.asyncio
    async def test_pair_invalid_code(self, aiohttp_client, deps, app):
        db, crypto, sm = deps
        db.get_pairing_code = AsyncMock(return_value=None)

        client = await aiohttp_client(app)
        resp = await client.post("/api/pair", json={
            "code": "999999",
            "token": "token",
            "device_id": "dev",
        })

        assert resp.status == 400
        data = await resp.json()
        assert "invalid" in data["error"].lower() or "expired" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_pair_missing_fields(self, aiohttp_client, deps, app):
        db, crypto, sm = deps
        client = await aiohttp_client(app)
        resp = await client.post("/api/pair", json={"code": "123456"})

        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_pair_existing_user_updates_token(self, aiohttp_client, deps, app):
        db, crypto, sm = deps
        now = sqlite_timestamp()
        db.get_pairing_code = AsyncMock(return_value={
            "code": "123456", "telegram_id": 111111111,
            "created_at": now,
        })
        db.get_user_by_telegram_id = AsyncMock(return_value={
            "id": 1, "telegram_id": 111111111, "status": "paused",
        })

        client = await aiohttp_client(app)
        resp = await client.post("/api/pair", json={
            "code": "123456", "token": "new_token", "device_id": "new_dev",
        })

        assert resp.status == 200
        db.update_user_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_pair_uses_own_device_not_browsers(self, aiohttp_client, deps, app):
        """Sharing the browser's deviceId let a web.max.ru re-login revoke
        the bridge's token (2026-10-09), so the bridge logs in as its own device."""
        db, crypto, sm = deps
        db.get_pairing_code = AsyncMock(return_value={
            "code": "123456", "telegram_id": 111111111,
            "created_at": sqlite_timestamp(),
        })
        db.get_user_by_telegram_id = AsyncMock(return_value=None)

        client = await aiohttp_client(app)
        resp = await client.post("/api/pair", json={
            "code": "123456", "token": "tok", "device_id": "browser-dev",
        })

        assert resp.status == 200
        device_id = db.create_user.call_args.kwargs["max_device_id"]
        assert device_id != "browser-dev"
        uuid.UUID(device_id)

    @pytest.mark.asyncio
    async def test_repair_moves_off_browser_device(self, aiohttp_client, deps, app):
        db, crypto, sm = deps
        db.get_pairing_code = AsyncMock(return_value={
            "code": "123456", "telegram_id": 111111111,
            "created_at": sqlite_timestamp(),
        })
        db.get_user_by_telegram_id = AsyncMock(return_value={
            "id": 1, "telegram_id": 111111111, "status": "expired",
        })

        client = await aiohttp_client(app)
        resp = await client.post("/api/pair", json={
            "code": "123456", "token": "tok", "device_id": "browser-dev",
        })

        assert resp.status == 200
        _, _, device_id = db.update_user_token.call_args.args
        assert device_id != "browser-dev"
        uuid.UUID(device_id)

    @pytest.mark.asyncio
    async def test_pair_without_device_id(self, aiohttp_client, deps, app):
        db, crypto, sm = deps
        db.get_pairing_code = AsyncMock(return_value={
            "code": "123456", "telegram_id": 111111111,
            "created_at": sqlite_timestamp(),
        })
        db.get_user_by_telegram_id = AsyncMock(return_value=None)

        client = await aiohttp_client(app)
        resp = await client.post("/api/pair", json={"code": "123456", "token": "tok"})

        assert resp.status == 200
        db.create_user.assert_called_once()

    @pytest.mark.asyncio
    async def test_health_endpoint(self, aiohttp_client, deps, app):
        client = await aiohttp_client(app)
        resp = await client.get("/api/health")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_pair_accepts_fresh_code_in_non_utc_timezone(
        self, aiohttp_client, deps, app, moscow_tz,
    ):
        """created_at is UTC; comparing it to local time expired every code."""
        db, crypto, sm = deps
        db.get_pairing_code = AsyncMock(return_value={
            "code": "123456",
            "telegram_id": 111111111,
            "created_at": sqlite_timestamp(),
        })
        db.get_user_by_telegram_id = AsyncMock(return_value=None)

        client = await aiohttp_client(app)
        resp = await client.post("/api/pair", json={
            "code": "123456", "token": "tok", "device_id": "dev",
        })

        assert resp.status == 200
        assert (await resp.json())["ok"] is True
        db.create_user.assert_called_once()

    @pytest.mark.asyncio
    async def test_pair_rejects_expired_code(self, aiohttp_client, deps, app):
        db, crypto, sm = deps
        db.get_pairing_code = AsyncMock(return_value={
            "code": "123456",
            "telegram_id": 111111111,
            "created_at": sqlite_timestamp(timedelta(minutes=-6)),
        })

        client = await aiohttp_client(app)
        resp = await client.post("/api/pair", json={
            "code": "123456", "token": "tok", "device_id": "dev",
        })

        assert resp.status == 400
        db.create_user.assert_not_called()
        db.delete_pairing_code.assert_called_once_with("123456")

    @pytest.mark.asyncio
    async def test_pair_rejects_unparsable_created_at(self, aiohttp_client, deps, app):
        db, crypto, sm = deps
        db.get_pairing_code = AsyncMock(return_value={
            "code": "123456", "telegram_id": 111111111, "created_at": "not-a-date",
        })

        client = await aiohttp_client(app)
        resp = await client.post("/api/pair", json={
            "code": "123456", "token": "tok", "device_id": "dev",
        })

        assert resp.status == 400
        db.create_user.assert_not_called()

    @pytest.mark.asyncio
    async def test_pair_non_object_body(self, aiohttp_client, deps, app):
        """A JSON string/array body must be a 400, not a 500."""
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/pair", data=json.dumps("just a string"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_pair_non_string_fields(self, aiohttp_client, deps, app):
        client = await aiohttp_client(app)
        resp = await client.post("/api/pair", json={
            "code": 123456, "token": ["t"], "device_id": None,
        })
        assert resp.status == 400
