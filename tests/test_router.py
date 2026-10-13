"""Tests for chat type → topic routing."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from max_bridge.router import MessageRouter


@pytest.fixture
def deps():
    db = AsyncMock()
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=500))
    session_manager = MagicMock()
    return db, bot, session_manager


class TestMessageRouter:
    @pytest.mark.asyncio
    async def test_route_dm_to_personal_topic(self, deps):
        db, bot, sm = deps
        db.get_user_group = AsyncMock(return_value={
            "chat_id": -100123456,
            "topic_dm": 2, "topic_groups": 3,
            "topic_channels": 4, "topic_comments": 5,
        })
        db.get_max_chat = AsyncMock(return_value={"chat_type": "DIALOG", "title": ""})
        sm.get_client = MagicMock(return_value=MagicMock(
            contacts={300: "Ivan Petrov"}, my_id=100,
        ))

        router = MessageRouter(db=db, bot=bot, session_manager=sm)
        msg = {"chat_id": 200, "sender_id": 300, "text": "Hello", "message_id": "m1"}

        await router.route_message(user_id=1, msg=msg)

        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs["chat_id"] == -100123456
        assert call_kwargs["message_thread_id"] == 2  # topic_dm
        assert "Ivan Petrov" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_route_group_to_groups_topic(self, deps):
        db, bot, sm = deps
        db.get_user_group = AsyncMock(return_value={
            "chat_id": -100123456,
            "topic_dm": 2, "topic_groups": 3,
            "topic_channels": 4, "topic_comments": 5,
        })
        db.get_max_chat = AsyncMock(return_value={"chat_type": "GROUP", "title": "Work Chat"})
        sm.get_client = MagicMock(return_value=MagicMock(
            contacts={300: "Ivan"}, my_id=100,
        ))

        router = MessageRouter(db=db, bot=bot, session_manager=sm)
        msg = {"chat_id": 201, "sender_id": 300, "text": "Meeting at 3", "message_id": "m2"}

        await router.route_message(user_id=1, msg=msg)

        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs["message_thread_id"] == 3  # topic_groups
        assert "Work Chat" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_route_channel_to_channels_topic(self, deps):
        db, bot, sm = deps
        db.get_user_group = AsyncMock(return_value={
            "chat_id": -100123456,
            "topic_dm": 2, "topic_groups": 3,
            "topic_channels": 4, "topic_comments": 5,
        })
        db.get_max_chat = AsyncMock(return_value={"chat_type": "CHANNEL", "title": "News"})
        sm.get_client = MagicMock(return_value=MagicMock(
            contacts={300: "Admin"}, my_id=100,
        ))

        router = MessageRouter(db=db, bot=bot, session_manager=sm)
        msg = {"chat_id": 202, "sender_id": 300, "text": "Breaking news", "message_id": "m3"}

        await router.route_message(user_id=1, msg=msg)

        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs["message_thread_id"] == 4  # topic_channels

    @pytest.mark.asyncio
    async def test_empty_text_skipped(self, deps):
        db, bot, sm = deps
        router = MessageRouter(db=db, bot=bot, session_manager=sm)
        msg = {"chat_id": 200, "sender_id": 300, "text": "", "message_id": "m1"}

        await router.route_message(user_id=1, msg=msg)

        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_user_group_skipped(self, deps):
        db, bot, sm = deps
        db.get_user_group = AsyncMock(return_value=None)

        router = MessageRouter(db=db, bot=bot, session_manager=sm)
        msg = {"chat_id": 200, "sender_id": 300, "text": "Hello", "message_id": "m1"}

        await router.route_message(user_id=1, msg=msg)

        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_saves_mapping_after_send(self, deps):
        db, bot, sm = deps
        db.get_user_group = AsyncMock(return_value={
            "chat_id": -100123456,
            "topic_dm": 2, "topic_groups": 3,
            "topic_channels": 4, "topic_comments": 5,
        })
        db.get_max_chat = AsyncMock(return_value={"chat_type": "DIALOG", "title": ""})
        sm.get_client = MagicMock(return_value=MagicMock(
            contacts={300: "Ivan"}, my_id=100,
        ))

        router = MessageRouter(db=db, bot=bot, session_manager=sm)
        msg = {"chat_id": 200, "sender_id": 300, "text": "Hi", "message_id": "m1"}

        await router.route_message(user_id=1, msg=msg)

        db.save_mapping.assert_called_once()

    def test_format_dm(self):
        text = MessageRouter.format_message(sender_name="Ivan Petrov", text="Hello", chat_title=None)
        assert text == "Ivan Petrov:\nHello"

    def test_format_group(self):
        text = MessageRouter.format_message(sender_name="Ivan", text="Hi", chat_title="Work Chat")
        assert text == "[Work Chat]\nIvan:\nHi"

    def test_format_truncates_long_text(self):
        text = MessageRouter.format_message(sender_name="A", text="x" * 5000, chat_title=None)
        assert len(text) <= 4096


class TestHtmlSafety:
    @pytest.mark.asyncio
    async def test_message_with_angle_brackets_not_sent_as_html(self, deps):
        """With parse_mode=HTML Telegram rejects "<" and the message is lost."""
        db, bot, sm = deps
        db.get_user_group = AsyncMock(return_value={
            "chat_id": -100123456,
            "topic_dm": 2, "topic_groups": 3,
            "topic_channels": 4, "topic_comments": 5,
        })
        db.get_max_chat = AsyncMock(return_value={"chat_type": "DIALOG", "title": ""})
        sm.get_client = MagicMock(return_value=MagicMock(
            contacts={300: "Ivan <dev>"}, my_id=100,
        ))

        router = MessageRouter(db=db, bot=bot, session_manager=sm)
        msg = {"chat_id": 200, "sender_id": 300, "text": "if a < b and c > d",
               "message_id": "m1"}

        await router.route_message(user_id=1, msg=msg)

        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs.get("parse_mode") is None
        # Text reaches Telegram verbatim
        assert "if a < b and c > d" in call_kwargs["text"]
        assert "Ivan <dev>" in call_kwargs["text"]
        db.save_mapping.assert_called_once()
