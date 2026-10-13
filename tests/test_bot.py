"""Tests for aiogram Telegram bot."""
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from max_bridge.bot import BotService
from max_bridge.qr_login import QrExpiredError, QrLoginError


class TestBotService:
    @pytest.fixture
    def bot_service(self):
        db = AsyncMock()
        crypto = MagicMock()
        session_manager = AsyncMock()
        svc = BotService(
            bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz-1234567890",
            db=db,
            crypto=crypto,
            session_manager=session_manager,
        )
        svc.bot = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_handle_start_new_user_sends_step1(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=111111111)
        message.chat.id = 111111111
        message.chat.type = "private"

        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value=None)

        await bot_service.handle_start(message)

        # Full guide + step 1 install instructions
        bot_service.bot.send_message.assert_called_once()
        call_text = bot_service.bot.send_message.call_args[0][1]
        assert "MAX Bridge" in call_text
        assert "QR" in call_text
        assert "Шаг 1" in call_text

    @pytest.mark.asyncio
    async def test_handle_start_pending_user_sends_group_step(self, bot_service):
        """Logged in to MAX but no group yet → the group step, not a new login."""
        message = AsyncMock()
        message.from_user = MagicMock(id=111111111)
        message.chat.id = 111111111
        message.chat.type = "private"

        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value={
            "id": 1, "status": "pending", "telegram_id": 111111111,
        })
        bot_service.db.get_user_group = AsyncMock(return_value=None)

        await bot_service.handle_start(message)

        call_text = bot_service.bot.send_message.call_args[0][1]
        assert "Шаг 2" in call_text

    @pytest.mark.asyncio
    async def test_handle_start_expired_user_gets_qr_login(self, bot_service):
        """After MAX revokes the login, /start logs in again, not restarts onboarding."""
        message = AsyncMock()
        message.from_user = MagicMock(id=111111111)
        message.chat.id = 111111111
        message.chat.type = "private"

        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value={
            "id": 1, "status": "expired", "telegram_id": 111111111,
        })
        bot_service._run_qr_login = AsyncMock()

        await bot_service.handle_start(message)

        bot_service._run_qr_login.assert_awaited_once_with(111111111, 111111111)

    @pytest.mark.asyncio
    async def test_notify_auth_failed_messages_user(self, bot_service):
        bot_service.db.get_user = AsyncMock(return_value={"id": 1, "telegram_id": 42})

        await bot_service.notify_auth_failed(1)

        chat_id, text = bot_service.bot.send_message.call_args[0][:2]
        assert chat_id == 42
        assert "/login" in text

    @pytest.mark.asyncio
    async def test_handle_start_active_user_with_group(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=111111111)
        message.chat.type = "private"

        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value={
            "id": 1, "status": "active", "telegram_id": 111111111,
        })
        bot_service.db.get_user_group = AsyncMock(return_value={
            "chat_id": -100123, "topic_dm": 1,
        })

        await bot_service.handle_start(message)

        message.answer.assert_called_once()
        call_text = message.answer.call_args[0][0]
        assert "работает" in call_text.lower()

    @pytest.mark.asyncio
    async def test_handle_start_active_user_no_group_sends_step3(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=111111111)
        message.chat.id = 111111111
        message.chat.type = "private"

        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value={
            "id": 1, "status": "active", "telegram_id": 111111111,
        })
        bot_service.db.get_user_group = AsyncMock(return_value=None)

        await bot_service.handle_start(message)

        call_text = bot_service.bot.send_message.call_args[0][1]
        assert "Шаг 2" in call_text

    @pytest.mark.asyncio
    async def test_handle_status_no_user(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=999)
        message.chat.type = "private"

        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value=None)

        await bot_service.handle_status(message)

        message.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_stop_deactivates(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=111111111)
        message.chat.type = "private"

        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value={
            "id": 1, "status": "active", "telegram_id": 111111111,
        })

        await bot_service.handle_stop(message)

        bot_service.db.update_user_status.assert_called_once_with(1, "paused")
        bot_service.session_manager.stop_session.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_handle_setup_creates_topics(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=111111111)
        message.chat.id = -100123
        message.chat.type = "supergroup"

        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value={
            "id": 1, "status": "active", "telegram_id": 111111111,
        })
        bot_service.db.get_user_group = AsyncMock(return_value=None)
        bot_service.db.get_user_by_tg_chat = AsyncMock(return_value=None)

        topic_mock = MagicMock()
        topic_mock.message_thread_id = 42
        bot_service.bot.create_forum_topic = AsyncMock(return_value=topic_mock)

        await bot_service.handle_setup(message)

        assert bot_service.bot.create_forum_topic.call_count == 4
        bot_service.db.save_user_group.assert_called_once()
        bot_service.db.update_user_status.assert_called_once_with(1, "active")

    @pytest.mark.asyncio
    async def test_handle_setup_already_exists(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=111111111)
        message.chat.id = -100123
        message.chat.type = "supergroup"

        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value={
            "id": 1, "status": "active", "telegram_id": 111111111,
        })
        bot_service.db.get_user_group = AsyncMock(return_value={"chat_id": -100123})

        await bot_service.handle_setup(message)

        # Should not create topics
        bot_service.bot.create_forum_topic.assert_not_called()
        message.reply.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_setup_rejects_group_of_another_user(self, bot_service):
        """A group already bound to someone else must not be re-bound."""
        message = AsyncMock()
        message.from_user = MagicMock(id=555)
        message.chat.id = -100123
        message.chat.type = "supergroup"

        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value={
            "id": 2, "status": "active", "telegram_id": 555,
        })
        bot_service.db.get_user_group = AsyncMock(return_value=None)
        # Chat already belongs to user id 1
        bot_service.db.get_user_by_tg_chat = AsyncMock(return_value={
            "id": 1, "status": "active", "telegram_id": 111111111,
        })

        await bot_service.handle_setup(message)

        bot_service.bot.create_forum_topic.assert_not_called()
        bot_service.db.save_user_group.assert_not_called()
        message.reply.assert_called_once()
        assert "другому" in message.reply.call_args[0][0]

    @pytest.mark.asyncio
    async def test_concurrent_setup_creates_topics_once(self, bot_service):
        """/setup racing with the "bot added" event must create one topic set."""
        message = AsyncMock()
        message.from_user = MagicMock(id=111111111)
        message.chat.id = -100123
        message.chat.type = "supergroup"

        event = MagicMock()
        event.chat.id = -100123
        event.from_user = MagicMock(id=111111111)

        user = {"id": 1, "status": "active", "telegram_id": 111111111}
        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value=user)
        bot_service.db.get_user_by_tg_chat = AsyncMock(return_value=None)

        # get_user_group reports "no group" until save_user_group runs
        saved = []

        async def get_user_group(_user_id):
            return saved[0] if saved else None

        async def save_user_group(**kwargs):
            await asyncio.sleep(0)  # yield, as a real DB write would
            saved.append(kwargs)

        bot_service.db.get_user_group = AsyncMock(side_effect=get_user_group)
        bot_service.db.save_user_group = AsyncMock(side_effect=save_user_group)

        topic_mock = MagicMock()
        topic_mock.message_thread_id = 42

        async def create_topic(*args, **kwargs):
            await asyncio.sleep(0)  # yield, as a real API call would
            return topic_mock

        bot_service.bot.create_forum_topic = AsyncMock(side_effect=create_topic)

        await asyncio.gather(
            bot_service.handle_setup(message),
            bot_service.handle_bot_added(event),
        )

        assert bot_service.bot.create_forum_topic.call_count == 4
        assert bot_service.db.save_user_group.call_count == 1

    @pytest.mark.asyncio
    async def test_callback_ext_installed(self, bot_service):
        callback = AsyncMock()
        callback.from_user = MagicMock(id=111111111)
        callback.data = "ext_installed"
        callback.message.chat.id = 111111111
        bot_service._run_qr_login = AsyncMock()

        await bot_service.handle_callback(callback)

        callback.answer.assert_called_once()
        # A button left from the extension onboarding now starts a QR login
        bot_service._run_qr_login.assert_awaited_once_with(111111111, 111111111)

    @pytest.mark.asyncio
    async def test_callback_qr_login(self, bot_service):
        callback = AsyncMock()
        callback.from_user = MagicMock(id=111111111)
        callback.data = "qr_login"
        callback.message.chat.id = 111111111
        bot_service._run_qr_login = AsyncMock()

        await bot_service.handle_callback(callback)

        callback.answer.assert_called_once()
        bot_service._run_qr_login.assert_awaited_once_with(111111111, 111111111)

    @pytest.mark.asyncio
    async def test_handle_start_tracks_onboarding(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=999, username="newuser", full_name="New User")
        message.chat.id = 999
        message.chat.type = "private"
        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value=None)

        with patch("max_bridge.bot.analytics") as mock_analytics:
            await bot_service.handle_start(message)
            mock_analytics.track.assert_called()
            mock_analytics.identify.assert_called_once()


class TestSupport:
    @pytest.fixture
    def bot_service(self):
        db = AsyncMock()
        crypto = MagicMock()
        session_manager = AsyncMock()
        svc = BotService(
            bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz-1234567890",
            db=db,
            crypto=crypto,
            session_manager=session_manager,
            admin_tg_id=111111111,
        )
        svc.bot = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_support_no_text(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=999)
        message.text = "/support"
        message.chat.type = "private"

        await bot_service.handle_support(message)

        message.answer.assert_called_once()
        call_text = message.answer.call_args[0][0]
        assert "/support" in call_text

    @pytest.mark.asyncio
    async def test_support_forwards_to_admin(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=999, username="testuser", full_name="Test User")
        message.text = "/support Help me please"
        message.chat.type = "private"

        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot_service.bot.send_message = AsyncMock(return_value=sent_msg)

        await bot_service.handle_support(message)

        # Should send to admin
        bot_service.bot.send_message.assert_called_once()
        call_args = bot_service.bot.send_message.call_args
        assert call_args[0][0] == 111111111  # admin_tg_id
        assert "Help me please" in call_args[0][1]
        assert "@testuser" in call_args[0][1]

        # Should save ticket
        bot_service.db.save_support_ticket.assert_called_once_with(999, 42, "Help me please")

        # Should confirm to user
        message.answer.assert_called_once()
        assert "отправлено" in message.answer.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_support_disabled_without_admin(self, bot_service):
        bot_service.admin_tg_id = 0
        message = AsyncMock()
        message.from_user = MagicMock(id=999)
        message.text = "/support Help me please"

        await bot_service.handle_support(message)

        bot_service.bot.send_message.assert_not_called()
        assert "не настроена" in message.answer.call_args[0][0]

    @pytest.mark.asyncio
    async def test_support_no_username(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=999, username="", full_name="No Username")
        message.text = "/support Hello"
        message.chat.type = "private"

        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot_service.bot.send_message = AsyncMock(return_value=sent_msg)

        await bot_service.handle_support(message)

        call_text = bot_service.bot.send_message.call_args[0][1]
        assert "No Username" in call_text
        assert "@" not in call_text  # no username shown

    @pytest.mark.asyncio
    async def test_admin_reply_sends_to_user(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=111111111)  # admin
        message.chat.type = "private"
        message.text = "We'll fix it soon"
        message.reply_to_message = MagicMock()
        message.reply_to_message.message_id = 42

        bot_service.db.get_support_ticket = AsyncMock(return_value={
            "user_telegram_id": 999,
            "admin_message_id": 42,
            "user_message_text": "Help me",
        })

        await bot_service.handle_admin_reply(message)

        # Should send reply to user
        bot_service.bot.send_message.assert_called_once()
        call_args = bot_service.bot.send_message.call_args
        assert call_args[0][0] == 999
        assert "fix it soon" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_admin_reply_no_ticket(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=111111111)
        message.chat.type = "private"
        message.text = "Random reply"
        message.reply_to_message = MagicMock()
        message.reply_to_message.message_id = 999

        bot_service.db.get_support_ticket = AsyncMock(return_value=None)

        await bot_service.handle_admin_reply(message)

        # Should not send anything
        bot_service.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_admin_reply_ignored(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=555)  # not admin
        message.chat.type = "private"
        message.text = "some reply"
        message.reply_to_message = MagicMock()
        message.reply_to_message.message_id = 42

        await bot_service.handle_admin_reply(message)

        # Should not look up ticket
        bot_service.db.get_support_ticket.assert_not_called()

    @pytest.mark.asyncio
    async def test_support_escapes_html_in_user_data(self, bot_service):
        """Angle brackets in name/text must be escaped, not sent raw."""
        message = AsyncMock()
        message.from_user = MagicMock(
            id=999, username="user<b>", full_name="Ivan <script>",
        )
        message.text = "/support 1 < 2 & 3 > 2"
        message.chat.type = "private"

        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot_service.bot.send_message = AsyncMock(return_value=sent_msg)

        await bot_service.handle_support(message)

        call_args = bot_service.bot.send_message.call_args
        sent_text = call_args[0][1]
        assert call_args[1]["parse_mode"] == "HTML"
        assert "<script>" not in sent_text
        assert "&lt;script&gt;" in sent_text
        assert "1 &lt; 2 &amp; 3 &gt; 2" in sent_text
        # Only our own markup tags remain
        assert sent_text.count("<b>") == 1
        # The ticket stores the original, unescaped text
        bot_service.db.save_support_ticket.assert_called_once_with(
            999, 42, "1 < 2 & 3 > 2",
        )

    @pytest.mark.asyncio
    async def test_admin_reply_escapes_html(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=111111111)
        message.chat.type = "private"
        message.text = "use <b>bold</b> & fix it"
        message.reply_to_message = MagicMock()
        message.reply_to_message.message_id = 42

        bot_service.db.get_support_ticket = AsyncMock(return_value={
            "user_telegram_id": 999,
            "admin_message_id": 42,
            "user_message_text": "Help me",
        })

        await bot_service.handle_admin_reply(message)

        sent_text = bot_service.bot.send_message.call_args[0][1]
        assert "&lt;b&gt;bold&lt;/b&gt; &amp; fix it" in sent_text

    @pytest.mark.asyncio
    async def test_support_tracks_event(self, bot_service):
        message = AsyncMock()
        message.from_user = MagicMock(id=999, username="u", full_name="U")
        message.text = "/support help"
        message.chat.type = "private"
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot_service.bot.send_message = AsyncMock(return_value=sent_msg)

        with patch("max_bridge.bot.analytics") as mock_analytics:
            await bot_service.handle_support(message)
            mock_analytics.track.assert_called_with(999, "support_sent", {})


class TestGroupReply:
    @pytest.fixture
    def bot_service(self):
        db = AsyncMock()
        crypto = MagicMock()
        session_manager = AsyncMock()
        svc = BotService(
            bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz-1234567890",
            db=db, crypto=crypto, session_manager=session_manager,
        )
        svc.bot = AsyncMock()
        return svc

    @staticmethod
    def _reply_message():
        message = AsyncMock()
        message.chat.id = -100123
        message.chat.type = "supergroup"
        message.message_thread_id = 2
        message.text = "Hi back"
        message.reply_to_message = MagicMock()
        message.reply_to_message.message_id = 500
        return message

    @pytest.mark.asyncio
    async def test_reply_routed_to_max(self, bot_service):
        message = self._reply_message()
        bot_service.db.get_user_by_tg_chat = AsyncMock(return_value={"id": 1})
        bot_service.db.get_mapping_by_tg_message = AsyncMock(
            return_value={"max_chat_id": 200}
        )
        bot_service.session_manager.send_message = AsyncMock(return_value=True)

        await bot_service.handle_group_reply(message)

        bot_service.session_manager.send_message.assert_called_once_with(1, 200, "Hi back")
        message.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_told_when_max_send_fails(self, bot_service):
        """A reply that never reached MAX must not disappear silently."""
        message = self._reply_message()
        bot_service.db.get_user_by_tg_chat = AsyncMock(return_value={"id": 1})
        bot_service.db.get_mapping_by_tg_message = AsyncMock(
            return_value={"max_chat_id": 200}
        )
        bot_service.session_manager.send_message = AsyncMock(return_value=False)

        await bot_service.handle_group_reply(message)

        message.reply.assert_called_once()
        assert "MAX" in message.reply.call_args[0][0]


class TestQrLogin:
    @pytest.fixture
    def bot_service(self):
        db = AsyncMock()
        crypto = MagicMock()
        crypto.encrypt = MagicMock(return_value="enc")
        session_manager = AsyncMock()
        svc = BotService(
            bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz-1234567890",
            db=db,
            crypto=crypto,
            session_manager=session_manager,
        )
        svc.bot = AsyncMock()
        svc.bot.send_photo = AsyncMock(return_value=MagicMock(message_id=77))
        return svc

    @staticmethod
    def approved(token="own_token"):
        async def fake_qr_login(device_id, on_qr):
            await on_qr("https://max.ru/:auth/abc")
            return token
        return fake_qr_login

    @pytest.mark.asyncio
    async def test_relogin_resumes_existing_group(self, bot_service):
        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value={
            "id": 1, "telegram_id": 42, "status": "expired",
        })
        bot_service.db.get_user_group = AsyncMock(return_value={"chat_id": -100})

        with patch("max_bridge.bot.qr_login", new=self.approved()):
            await bot_service._run_qr_login(42, 42)

        user_id, encrypted, device_id = bot_service.db.update_user_token.call_args.args
        assert (user_id, encrypted) == (1, "enc")
        bot_service.crypto.encrypt.assert_called_once_with("own_token")
        bot_service.db.update_user_status.assert_called_with(1, "active")
        bot_service.session_manager.start_session.assert_awaited_once_with(1)
        # The QR is sent as a picture and removed once used
        bot_service.bot.send_photo.assert_awaited_once()
        bot_service.bot.delete_message.assert_awaited_once_with(42, 77)

    @pytest.mark.asyncio
    async def test_new_user_created_and_sent_to_group_step(self, bot_service):
        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value=None)
        bot_service.db.create_user = AsyncMock(return_value=5)
        bot_service.db.get_user_group = AsyncMock(return_value=None)

        with patch("max_bridge.bot.qr_login", new=self.approved()):
            await bot_service._run_qr_login(42, 42)

        assert bot_service.db.create_user.call_args.kwargs["telegram_id"] == 42
        bot_service.db.update_user_status.assert_called_with(5, "pending")
        bot_service.session_manager.start_session.assert_not_called()
        assert "Шаг 2" in bot_service.bot.send_message.call_args[0][1]

    @pytest.mark.asyncio
    async def test_failure_reported_and_guard_released(self, bot_service):
        async def failing(device_id, on_qr):
            await on_qr("https://max.ru/:auth/abc")
            raise QrExpiredError("QR expired")

        with patch("max_bridge.bot.qr_login", new=failing):
            await bot_service._run_qr_login(42, 42)

        text = bot_service.bot.send_message.call_args[0][1]
        assert "истёк" in text and "/login" in text
        bot_service.db.update_user_token.assert_not_called()
        bot_service.bot.delete_message.assert_awaited_once_with(42, 77)
        assert 42 not in bot_service._qr_logins

    @pytest.mark.asyncio
    async def test_second_login_while_one_is_running(self, bot_service):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(device_id, on_qr):
            started.set()
            await release.wait()
            return "tok"

        bot_service.db.get_user_by_telegram_id = AsyncMock(return_value=None)
        bot_service.db.get_user_group = AsyncMock(return_value=None)
        with patch("max_bridge.bot.qr_login", new=slow) as fake:
            first = asyncio.create_task(bot_service._run_qr_login(42, 42))
            await started.wait()
            await bot_service._run_qr_login(42, 42)
            release.set()
            await first

        assert bot_service.db.create_user.await_count == 1
        assert "уже отправлен" in bot_service.bot.send_message.call_args_list[0][0][1]

    @pytest.mark.asyncio
    async def test_refusal_reason_shown(self, bot_service):
        async def refused(device_id, on_qr):
            raise QrLoginError("qr login disabled")

        with patch("max_bridge.bot.qr_login", new=refused):
            await bot_service._run_qr_login(42, 42)

        text = bot_service.bot.send_message.call_args[0][1]
        assert "qr login disabled" in text and "/login" in text
