"""Integration tests for multi-user flow."""
import os
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from max_bridge.db import Database
from max_bridge.crypto import TokenCrypto
from max_bridge.session_manager import SessionManager
from max_bridge.router import MessageRouter


@pytest.fixture
async def db():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        path = f.name
    d = Database(path)
    await d.init()
    yield d
    await d.close()
    os.unlink(path)


@pytest.fixture
def crypto():
    return TokenCrypto.generate()


class TestFullOnboardingFlow:
    @pytest.mark.asyncio
    async def test_pair_and_route_message(self, db, crypto):
        """Full flow: create user → save group → route message."""
        # 1. Simulate pairing
        encrypted = crypto.encrypt("max_token_123")
        user_id = await db.create_user(
            telegram_id=111111111,
            max_token_encrypted=encrypted,
            max_device_id="device_abc",
        )

        # 2. Simulate group setup
        await db.save_user_group(
            user_id=user_id, chat_id=-100123456,
            topic_dm=2, topic_groups=3, topic_channels=4, topic_comments=5,
        )
        await db.update_user_status(user_id, "active")

        # 3. Save chat type
        await db.save_max_chat(user_id, 200, "DIALOG", "")
        await db.save_max_chat(user_id, 201, "GROUP", "Work Chat")

        # 4. Route a DM message
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=500))

        sm = MagicMock()
        sm.get_client = MagicMock(return_value=MagicMock(
            contacts={300: "Ivan Petrov"}, my_id=100,
        ))

        router = MessageRouter(db=db, bot=bot, session_manager=sm)

        msg = {"chat_id": 200, "sender_id": 300, "text": "Hello!", "message_id": "m1"}
        await router.route_message(user_id, msg)

        # Verify sent to DM topic
        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs["message_thread_id"] == 2  # topic_dm
        assert "Ivan Petrov" in call_kwargs["text"]

        # 5. Route a group message
        bot.reset_mock()
        msg2 = {"chat_id": 201, "sender_id": 300, "text": "Meeting", "message_id": "m2"}
        await router.route_message(user_id, msg2)

        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs["message_thread_id"] == 3  # topic_groups
        assert "Work Chat" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_token_encryption_roundtrip(self, db, crypto):
        """Token is encrypted in DB and decryptable."""
        token = "super_secret_max_token"
        encrypted = crypto.encrypt(token)
        user_id = await db.create_user(
            telegram_id=100,
            max_token_encrypted=encrypted,
            max_device_id="dev",
        )

        user = await db.get_user(user_id)
        decrypted = crypto.decrypt(user["max_token_encrypted"])
        assert decrypted == token

    @pytest.mark.asyncio
    async def test_reverse_reply_mapping(self, db, crypto):
        """Reply lookup finds correct MAX chat."""
        uid = await db.create_user(telegram_id=100, max_token_encrypted="t", max_device_id="d")
        await db.save_mapping(
            user_id=uid, tg_message_id=500, tg_chat_id=-100123456,
            max_chat_id=200, max_sender_id=300, sender_name="Ivan",
        )

        mapping = await db.get_mapping_by_tg_message(500, -100123456)
        assert mapping is not None
        assert mapping["max_chat_id"] == 200
        assert mapping["user_id"] == uid


class TestMultiUserIsolation:
    @pytest.mark.asyncio
    async def test_messages_isolated_per_user(self, db, crypto):
        """Messages from different users don't mix."""
        # Create 2 users
        user1_id = await db.create_user(
            telegram_id=100,
            max_token_encrypted=crypto.encrypt("token1"),
            max_device_id="dev1",
        )
        user2_id = await db.create_user(
            telegram_id=200,
            max_token_encrypted=crypto.encrypt("token2"),
            max_device_id="dev2",
        )

        # Save message for user1
        await db.save_mapping(
            user_id=user1_id, tg_message_id=1, tg_chat_id=-100,
            max_chat_id=10, max_sender_id=50, sender_name="Alice",
        )

        # Verify user2 doesn't see it
        mapping1 = await db.get_mapping_by_tg_message(1, -100)
        assert mapping1["user_id"] == user1_id

        # Save different message for user2
        await db.save_mapping(
            user_id=user2_id, tg_message_id=2, tg_chat_id=-200,
            max_chat_id=20, max_sender_id=60, sender_name="Bob",
        )

        mapping2 = await db.get_mapping_by_tg_message(2, -200)
        assert mapping2["user_id"] == user2_id

        # Verify isolation
        assert mapping1["user_id"] != mapping2["user_id"]


class TestChannelRouting:
    @pytest.mark.asyncio
    async def test_channel_to_topic(self, db, crypto):
        """Channel posts route to topic_channels."""
        user_id = await db.create_user(
            telegram_id=100,
            max_token_encrypted=crypto.encrypt("token"),
            max_device_id="dev",
        )

        await db.save_user_group(
            user_id=user_id, chat_id=-100123456,
            topic_dm=2, topic_groups=3, topic_channels=4, topic_comments=5,
        )
        await db.update_user_status(user_id, "active")
        await db.save_max_chat(user_id, 300, "CHANNEL", "News")

        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=600))

        sm = MagicMock()
        sm.get_client = MagicMock(return_value=MagicMock(
            contacts={400: "Bob"}, my_id=100,
        ))

        router = MessageRouter(db=db, bot=bot, session_manager=sm)

        # Channel message
        msg = {
            "chat_id": 300,
            "sender_id": 400,
            "text": "Breaking news!",
            "message_id": "m_channel",
        }
        await router.route_message(user_id, msg)

        # Verify sent to channels topic
        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs["message_thread_id"] == 4  # topic_channels
        assert "News" in call_kwargs["text"]
        assert "Bob" in call_kwargs["text"]


class TestPairingCodeExpiry:
    @pytest.mark.asyncio
    async def test_pairing_code_lifecycle(self, db, crypto):
        """Create code, verify it, delete it."""
        code = "123456"
        tg_id = 111111111

        # Create code
        await db.create_pairing_code(code, telegram_id=tg_id)

        # Verify exists
        result = await db.get_pairing_code(code)
        assert result is not None
        assert result["telegram_id"] == tg_id

        # Delete code
        await db.delete_pairing_code(code)

        # Verify deleted
        result = await db.get_pairing_code(code)
        assert result is None
