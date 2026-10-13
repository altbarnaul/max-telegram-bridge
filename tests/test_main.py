"""Tests for application wiring."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestAppStartup:
    @pytest.mark.asyncio
    async def test_creates_all_components(self):
        with patch("max_bridge.__main__.Database") as MockDB, \
             patch("max_bridge.__main__.TokenCrypto") as MockCrypto, \
             patch("max_bridge.__main__.BotService") as MockBot, \
             patch("max_bridge.__main__.SessionManager") as MockSM, \
             patch("max_bridge.__main__.MessageRouter") as MockRouter, \
             patch("max_bridge.__main__.create_api_app") as MockAPI:

            mock_db = AsyncMock()
            MockDB.return_value = mock_db

            from max_bridge.__main__ import create_app
            components = await create_app(
                tg_bot_token="test", encryption_key="key",
                db_path=":memory:", api_host="0.0.0.0", api_port=8080,
            )

            assert "db" in components
            assert "bot" in components
            assert "session_manager" in components
            assert "router" in components
            mock_db.init.assert_called_once()
