"""PostHog analytics integration for MAX Bridge bot.

Provides reusable functions to track events and identify users.
Automatically includes bot_name in all events and properties.
"""
import os
from typing import Any, Callable, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from posthog import Posthog


# Global state
BOT_NAME: str = ""
_ph: Optional[Posthog] = None


def init(bot_name: Optional[str] = None) -> None:
    """Initialize PostHog client.

    Args:
        bot_name: Name of the bot (used in all events). Falls back to ANALYTICS_BOT_NAME env var.

    Environment variables:
        POSTHOG_API_KEY: PostHog API key (required for initialization)
        POSTHOG_HOST: PostHog host URL (default: https://eu.i.posthog.com)
        ANALYTICS_BOT_NAME: Fallback bot name if bot_name parameter is None

    If POSTHOG_API_KEY is empty or not set, this function is a no-op.
    """
    global BOT_NAME, _ph

    # Set bot name
    BOT_NAME = bot_name or os.getenv("ANALYTICS_BOT_NAME", "")

    # Check for API key
    api_key = os.getenv("POSTHOG_API_KEY", "")
    if not api_key:
        return

    # Initialize PostHog client
    host = os.getenv("POSTHOG_HOST", "https://eu.i.posthog.com")
    _ph = Posthog(api_key, host=host)


def track(user_id: int, event: str, properties: dict[str, Any]) -> None:
    """Track an event.

    Args:
        user_id: Telegram user ID
        event: Event name (e.g., "button_clicked")
        properties: Event properties

    Automatically adds bot_name to properties.
    No-op if PostHog is not initialized.
    """
    if _ph is None:
        return

    props = {**properties, "bot_name": BOT_NAME}
    _ph.capture(distinct_id=str(user_id), event=event, properties=props)


def identify(user_id: int, username: Optional[str], full_name: Optional[str]) -> None:
    """Set user properties.

    Args:
        user_id: Telegram user ID
        username: Telegram username (without @)
        full_name: User's full name

    Automatically adds bot_name to properties.
    No-op if PostHog is not initialized.
    """
    if _ph is None:
        return

    properties = {
        "username": username,
        "full_name": full_name,
        "bot_name": BOT_NAME
    }
    _ph.identify(distinct_id=str(user_id), properties=properties)


def shutdown() -> None:
    """Flush and close PostHog client.

    No-op if PostHog is not initialized.
    """
    if _ph is None:
        return

    _ph.flush()
    _ph.shutdown()


class AnalyticsMiddleware(BaseMiddleware):
    """Aiogram middleware for automatic event tracking.

    Tracks:
    - Commands (messages starting with /)
    - Regular messages
    - Callback queries
    - Errors raised by handlers
    """

    async def __call__(
        self,
        handler: Callable,
        event: Message | CallbackQuery,
        data: dict[str, Any]
    ) -> Any:
        """Process event and track analytics.

        Args:
            handler: Next handler in the chain
            event: Telegram update event (Message or CallbackQuery)
            data: Handler data dictionary

        Returns:
            Result from handler

        Raises:
            Exception: Re-raises any exception from handler after tracking it
        """
        try:
            result = await handler(event, data)
            self._track_event(event)
            return result
        except Exception as e:
            # Track error before re-raising
            user_id = getattr(getattr(event, "from_user", None), "id", 0)
            track(user_id, "error", {
                "error_type": type(e).__name__,
                "error_message": str(e)[:200],
            })
            raise

    def _track_event(self, event: Message | CallbackQuery) -> None:
        """Track event based on its type.

        Args:
            event: Telegram event to track
        """
        if isinstance(event, Message):
            user_id = event.from_user.id
            if event.text and event.text.startswith("/"):
                # Extract command name (strip @botname if present)
                cmd = event.text.split()[0].split("@")[0]
                track(user_id, "command", {
                    "command_name": cmd,
                    "chat_type": event.chat.type
                })
            else:
                track(user_id, "message", {
                    "chat_type": event.chat.type,
                    "has_text": bool(event.text)
                })
        elif isinstance(event, CallbackQuery):
            track(event.from_user.id, "callback", {
                "callback_data": event.data
            })
