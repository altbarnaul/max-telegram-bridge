"""Tests for PostHog analytics integration."""
import os
import pytest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import CallbackQuery, Message

from max_bridge import analytics


class TestAnalyticsInit:
    """Test analytics.init()."""

    def test_init_with_api_key(self, monkeypatch):
        """Initialize PostHog when API key is provided."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")
        monkeypatch.setenv("POSTHOG_HOST", "https://test.posthog.com")

        with patch("max_bridge.analytics.Posthog") as mock_posthog:
            analytics.init("test_bot")

            mock_posthog.assert_called_once_with(
                "test_key",
                host="https://test.posthog.com"
            )
            assert analytics.BOT_NAME == "test_bot"

    def test_init_without_api_key(self, monkeypatch):
        """No-op when API key is not provided."""
        monkeypatch.delenv("POSTHOG_API_KEY", raising=False)

        with patch("max_bridge.analytics.Posthog") as mock_posthog:
            analytics.init("test_bot")

            mock_posthog.assert_not_called()
            assert analytics.BOT_NAME == "test_bot"

    def test_init_with_empty_api_key(self, monkeypatch):
        """No-op when API key is empty string."""
        monkeypatch.setenv("POSTHOG_API_KEY", "")

        with patch("max_bridge.analytics.Posthog") as mock_posthog:
            analytics.init("test_bot")

            mock_posthog.assert_not_called()
            assert analytics.BOT_NAME == "test_bot"

    def test_init_uses_default_host(self, monkeypatch):
        """Use default PostHog host when POSTHOG_HOST not set."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")
        monkeypatch.delenv("POSTHOG_HOST", raising=False)

        with patch("max_bridge.analytics.Posthog") as mock_posthog:
            analytics.init("test_bot")

            mock_posthog.assert_called_once_with(
                "test_key",
                host="https://eu.i.posthog.com"
            )

    def test_init_uses_analytics_bot_name_fallback(self, monkeypatch):
        """Use ANALYTICS_BOT_NAME env var as fallback."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")
        monkeypatch.setenv("ANALYTICS_BOT_NAME", "fallback_bot")

        with patch("max_bridge.analytics.Posthog") as mock_posthog:
            analytics.init(None)

            assert analytics.BOT_NAME == "fallback_bot"


class TestAnalyticsTrack:
    """Test analytics.track()."""

    def test_track_when_initialized(self, monkeypatch):
        """Send event when PostHog is initialized."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")

        mock_ph = Mock()
        with patch("max_bridge.analytics.Posthog", return_value=mock_ph):
            analytics.init("test_bot")
            analytics.track(123456, "button_clicked", {"button": "start"})

            mock_ph.capture.assert_called_once_with(
                distinct_id="123456",
                event="button_clicked",
                properties={"button": "start", "bot_name": "test_bot"}
            )

    def test_track_when_not_initialized(self):
        """No-op when PostHog is not initialized."""
        analytics._ph = None
        analytics.BOT_NAME = "test_bot"

        # Should not raise
        analytics.track(123456, "button_clicked", {"button": "start"})

    def test_track_converts_user_id_to_string(self, monkeypatch):
        """User ID must be converted to string."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")

        mock_ph = Mock()
        with patch("max_bridge.analytics.Posthog", return_value=mock_ph):
            analytics.init("test_bot")
            analytics.track(123456, "test_event", {})

            # distinct_id should be string
            call_kwargs = mock_ph.capture.call_args.kwargs
            assert isinstance(call_kwargs["distinct_id"], str)
            assert call_kwargs["distinct_id"] == "123456"

    def test_track_adds_bot_name_to_properties(self, monkeypatch):
        """Bot name should be added to properties."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")

        mock_ph = Mock()
        with patch("max_bridge.analytics.Posthog", return_value=mock_ph):
            analytics.init("test_bot")
            analytics.track(123, "test_event", {"key": "value"})

            # properties should include bot_name
            call_kwargs = mock_ph.capture.call_args.kwargs
            assert call_kwargs["properties"] == {"key": "value", "bot_name": "test_bot"}


class TestAnalyticsIdentify:
    """Test analytics.identify()."""

    def test_identify_when_initialized(self, monkeypatch):
        """Set user properties when PostHog is initialized."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")

        mock_ph = Mock()
        with patch("max_bridge.analytics.Posthog", return_value=mock_ph):
            analytics.init("test_bot")
            analytics.identify(123456, "john_doe", "John Doe")

            mock_ph.identify.assert_called_once_with(
                distinct_id="123456",
                properties={
                    "username": "john_doe",
                    "full_name": "John Doe",
                    "bot_name": "test_bot"
                }
            )

    def test_identify_when_not_initialized(self):
        """No-op when PostHog is not initialized."""
        analytics._ph = None
        analytics.BOT_NAME = "test_bot"

        # Should not raise
        analytics.identify(123456, "john_doe", "John Doe")

    def test_identify_converts_user_id_to_string(self, monkeypatch):
        """User ID must be converted to string."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")

        mock_ph = Mock()
        with patch("max_bridge.analytics.Posthog", return_value=mock_ph):
            analytics.init("test_bot")
            analytics.identify(123456, "test_user", "Test User")

            # distinct_id should be string
            call_kwargs = mock_ph.identify.call_args.kwargs
            assert isinstance(call_kwargs["distinct_id"], str)
            assert call_kwargs["distinct_id"] == "123456"


class TestAnalyticsShutdown:
    """Test analytics.shutdown()."""

    def test_shutdown_when_initialized(self, monkeypatch):
        """Flush and close when PostHog is initialized."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")

        mock_ph = Mock()
        with patch("max_bridge.analytics.Posthog", return_value=mock_ph):
            analytics.init("test_bot")
            analytics.shutdown()

            mock_ph.flush.assert_called_once()
            mock_ph.shutdown.assert_called_once()

    def test_shutdown_when_not_initialized(self):
        """No-op when PostHog is not initialized."""
        analytics._ph = None

        # Should not raise
        analytics.shutdown()


class TestAnalyticsMiddleware:
    """Test AnalyticsMiddleware."""

    @pytest.mark.asyncio
    async def test_tracks_command(self, monkeypatch):
        """Track command events when message starts with /."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")

        mock_ph = Mock()
        with patch("max_bridge.analytics.Posthog", return_value=mock_ph):
            analytics.init("test_bot")

            # Create mock message with /start command
            message = Mock()
            message.__class__ = Message
            message.from_user = Mock()
            message.from_user.id = 123456
            message.text = "/start"
            message.chat = Mock()
            message.chat.type = "private"

            # Create middleware and handler
            middleware = analytics.AnalyticsMiddleware()
            handler = AsyncMock(return_value=None)

            # Call middleware
            await middleware(handler, message, {})

            # Verify handler was called
            handler.assert_called_once_with(message, {})

            # Verify analytics tracked command
            mock_ph.capture.assert_called_once_with(
                distinct_id="123456",
                event="command",
                properties={"command_name": "/start", "chat_type": "private", "bot_name": "test_bot"}
            )

    @pytest.mark.asyncio
    async def test_tracks_command_with_bot_mention(self, monkeypatch):
        """Strip @botname from command before tracking."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")

        mock_ph = Mock()
        with patch("max_bridge.analytics.Posthog", return_value=mock_ph):
            analytics.init("test_bot")

            # Create mock message with /start@my_bridge_bot
            message = Mock()
            message.__class__ = Message
            message.from_user = Mock()
            message.from_user.id = 123456
            message.text = "/start@my_bridge_bot some args"
            message.chat = Mock()
            message.chat.type = "group"

            middleware = analytics.AnalyticsMiddleware()
            handler = AsyncMock(return_value=None)

            await middleware(handler, message, {})

            # Verify command_name is /start (without @botname)
            call_kwargs = mock_ph.capture.call_args.kwargs
            assert call_kwargs["properties"]["command_name"] == "/start"

    @pytest.mark.asyncio
    async def test_tracks_regular_message(self, monkeypatch):
        """Track regular text messages."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")

        mock_ph = Mock()
        with patch("max_bridge.analytics.Posthog", return_value=mock_ph):
            analytics.init("test_bot")

            # Create mock message with regular text
            message = Mock()
            message.__class__ = Message
            message.from_user = Mock()
            message.from_user.id = 123456
            message.text = "hello world"
            message.chat = Mock()
            message.chat.type = "private"

            middleware = analytics.AnalyticsMiddleware()
            handler = AsyncMock(return_value=None)

            await middleware(handler, message, {})

            # Verify analytics tracked message
            mock_ph.capture.assert_called_once_with(
                distinct_id="123456",
                event="message",
                properties={"chat_type": "private", "has_text": True, "bot_name": "test_bot"}
            )

    @pytest.mark.asyncio
    async def test_tracks_callback(self, monkeypatch):
        """Track callback query events."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")

        mock_ph = Mock()
        with patch("max_bridge.analytics.Posthog", return_value=mock_ph):
            analytics.init("test_bot")

            # Create mock callback query
            callback = Mock()
            callback.__class__ = CallbackQuery
            callback.from_user = Mock()
            callback.from_user.id = 123456
            callback.data = "button_1"

            middleware = analytics.AnalyticsMiddleware()
            handler = AsyncMock(return_value=None)

            await middleware(handler, callback, {})

            # Verify analytics tracked callback
            mock_ph.capture.assert_called_once_with(
                distinct_id="123456",
                event="callback",
                properties={"callback_data": "button_1", "bot_name": "test_bot"}
            )

    @pytest.mark.asyncio
    async def test_tracks_error_and_reraises(self, monkeypatch):
        """Track errors and re-raise exceptions."""
        monkeypatch.setenv("POSTHOG_API_KEY", "test_key")

        mock_ph = Mock()
        with patch("max_bridge.analytics.Posthog", return_value=mock_ph):
            analytics.init("test_bot")

            # Create mock message
            message = Mock()
            message.__class__ = Message
            message.from_user = Mock()
            message.from_user.id = 123456
            message.text = "/start"
            message.chat = Mock()
            message.chat.type = "private"

            # Create handler that raises an error
            error = ValueError("Something went wrong with a very long message " * 10)
            handler = AsyncMock(side_effect=error)

            middleware = analytics.AnalyticsMiddleware()

            # Call middleware and expect exception
            with pytest.raises(ValueError, match="Something went wrong"):
                await middleware(handler, message, {})

            # Verify analytics tracked error
            mock_ph.capture.assert_called_once()
            call_kwargs = mock_ph.capture.call_args.kwargs
            assert call_kwargs["distinct_id"] == "123456"
            assert call_kwargs["event"] == "error"
            assert call_kwargs["properties"]["error_type"] == "ValueError"
            # Verify message is truncated to 200 chars
            assert len(call_kwargs["properties"]["error_message"]) <= 200

    @pytest.mark.asyncio
    async def test_handler_still_called_when_analytics_disabled(self):
        """Handler should be called even when analytics is disabled."""
        analytics._ph = None

        # Create mock message
        message = Mock()
        message.__class__ = Message
        message.from_user = Mock()
        message.from_user.id = 123456
        message.text = "/start"
        message.chat = Mock()
        message.chat.type = "private"

        # Create handler
        handler = AsyncMock(return_value="result")

        middleware = analytics.AnalyticsMiddleware()
        result = await middleware(handler, message, {})

        # Verify handler was called and result returned
        handler.assert_called_once_with(message, {})
        assert result == "result"
