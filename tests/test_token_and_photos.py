"""Token rotation, revoked-token handling and photo forwarding."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import InputMediaPhoto

from max_bridge.api import create_api_app
from max_bridge.crypto import TokenCrypto
from max_bridge.max_client import MaxClient, OpCode
from max_bridge.router import MessageRouter
from max_bridge.session_manager import SessionManager


def auth_frame(cmd, payload):
    return {"ver": 11, "cmd": cmd, "seq": 1, "opcode": OpCode.AUTH_SNAPSHOT, "payload": payload}


class TestPhotoParsing:
    def test_photo_with_base_url_is_collected_not_stubbed(self):
        client = MaxClient(token="t", device_id="d")
        msg = client._parse_message({"chatId": 1, "message": {
            "sender": 2, "id": "m", "text": "",
            "attaches": [
                {"_type": "PHOTO", "baseUrl": "https://i.oneme.ru/i?r=a", "width": 10},
                {"_type": "PHOTO", "baseUrl": "https://i.oneme.ru/i?r=b"},
            ],
        }})
        assert msg["photos"] == ["https://i.oneme.ru/i?r=a", "https://i.oneme.ru/i?r=b"]
        assert msg["text"] == ""

    def test_photo_without_url_falls_back_to_placeholder(self):
        client = MaxClient(token="t", device_id="d")
        msg = client._parse_message({"chatId": 1, "message": {
            "sender": 2, "id": "m", "attaches": [{"_type": "PHOTO"}],
        }})
        assert msg["photos"] == []
        assert msg["text"] == "[Изображение]"


class TestTokenRotation:
    @pytest.mark.asyncio
    async def test_new_token_from_snapshot_is_adopted_and_reported(self):
        client = MaxClient(token="old", device_id="d")
        client.on_token_refreshed = MagicMock()
        await client._handle_frame(auth_frame(1, {"profile": {"id": 7}, "token": "new"}))
        assert client.token == "new"
        client.on_token_refreshed.assert_called_once_with("new")

    @pytest.mark.asyncio
    async def test_same_token_is_not_reported(self):
        client = MaxClient(token="same", device_id="d")
        client.on_token_refreshed = MagicMock()
        await client._handle_frame(auth_frame(1, {"profile": {"id": 7}, "token": "same"}))
        client.on_token_refreshed.assert_not_called()

    @pytest.mark.asyncio
    async def test_revoked_token_stops_client_and_reports(self):
        client = MaxClient(token="dead", device_id="d")
        client._running = True
        client.on_auth_failed = MagicMock()
        await client._handle_frame(auth_frame(3, {"error": "login.token", "message": "FAIL_LOGIN_TOKEN"}))
        assert client._running is False
        client.on_auth_failed.assert_called_once()

    @pytest.mark.asyncio
    async def test_other_auth_errors_keep_reconnecting(self):
        client = MaxClient(token="t", device_id="d")
        client._running = True
        client.on_auth_failed = MagicMock()
        await client._handle_frame(auth_frame(3, {"error": "too.many.requests"}))
        assert client._running is True
        client.on_auth_failed.assert_not_called()


class TestSessionManagerTokenHandling:
    @pytest.fixture
    def sm(self):
        db = AsyncMock()
        crypto = TokenCrypto.generate()
        return SessionManager(db=db, crypto=crypto), db, crypto

    @pytest.mark.asyncio
    async def test_rotated_token_is_persisted_encrypted(self, sm):
        manager, db, crypto = sm
        await manager._save_token(1, "fresh", "dev")
        user_id, encrypted, device = db.update_user_token.call_args[0]
        assert (user_id, device) == (1, "dev")
        assert crypto.decrypt(encrypted) == "fresh"

    @pytest.mark.asyncio
    async def test_revoked_token_marks_user_expired_and_notifies(self, sm):
        manager, db, _ = sm
        manager.on_auth_failed = AsyncMock()
        await manager._on_auth_failed(1)
        db.update_user_status.assert_called_once_with(1, "expired")
        manager.on_auth_failed.assert_awaited_once_with(1)


class TestRePairing:
    @pytest.mark.asyncio
    async def test_existing_user_with_group_resumes_immediately(self, aiohttp_client):
        db = AsyncMock()
        db.get_pairing_code = AsyncMock(return_value={
            "telegram_id": 42, "created_at": "2099-01-01 00:00:00",
        })
        db.get_user_by_telegram_id = AsyncMock(return_value={"id": 1, "status": "expired"})
        db.get_user_group = AsyncMock(return_value={"chat_id": -100})
        sm = AsyncMock()
        app = create_api_app(db=db, crypto=TokenCrypto.generate(), session_manager=sm)
        client = await aiohttp_client(app)

        resp = await client.post("/api/pair", json={"code": "123456", "token": "t", "device_id": "d"})

        assert (await resp.json())["ok"] is True
        db.update_user_status.assert_called_once_with(1, "active")
        sm.start_session.assert_awaited_once_with(1)


class TestPhotoRouting:
    @pytest.fixture
    def router(self):
        db = AsyncMock()
        db.get_user_group = AsyncMock(return_value={
            "chat_id": -100, "topic_dm": 2, "topic_groups": 3,
            "topic_channels": 4, "topic_comments": 5,
        })
        db.get_max_chat = AsyncMock(return_value={"chat_type": "DIALOG", "title": ""})
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        bot.send_photo = AsyncMock(return_value=MagicMock(message_id=2))
        bot.send_media_group = AsyncMock(side_effect=lambda **kw: [
            MagicMock(message_id=100 + i) for i in range(len(kw["media"]))
        ])
        sm = MagicMock()
        sm.get_client = MagicMock(return_value=MagicMock(contacts={300: "Мария"}))
        r = MessageRouter(db=db, bot=bot, session_manager=sm)
        r.download = AsyncMock(return_value=b"jpeg")
        return r, db, bot

    def msg(self, n):
        return {"chat_id": 9, "sender_id": 300, "text": "", "message_id": "m",
                "photos": [f"https://i.oneme.ru/i?r={i}" for i in range(n)]}

    @pytest.mark.asyncio
    async def test_album_is_sent_with_sender_caption(self, router):
        r, db, bot = router
        await r.route_message(1, self.msg(2))
        media = bot.send_media_group.call_args.kwargs["media"]
        assert len(media) == 2 and all(isinstance(m, InputMediaPhoto) for m in media)
        assert media[0].caption == "Мария:"
        assert bot.send_media_group.call_args.kwargs["message_thread_id"] == 2
        bot.send_message.assert_not_called()
        assert db.save_mapping.call_args.kwargs["tg_message_id"] == 100

    @pytest.mark.asyncio
    async def test_more_than_ten_photos_are_split_into_albums(self, router):
        r, _, bot = router
        await r.route_message(1, self.msg(12))
        sizes = [len(c.kwargs["media"]) for c in bot.send_media_group.call_args_list]
        assert sizes == [10, 2]

    @pytest.mark.asyncio
    async def test_single_photo_uses_send_photo(self, router):
        r, _, bot = router
        await r.route_message(1, self.msg(1))
        bot.send_photo.assert_awaited_once()
        bot.send_media_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_downloads_fall_back_to_text(self, router):
        r, _, bot = router
        r.download = AsyncMock(side_effect=RuntimeError("403"))
        await r.route_message(1, self.msg(2))
        text = bot.send_message.call_args.kwargs["text"]
        assert text.count("[Изображение]") == 2
