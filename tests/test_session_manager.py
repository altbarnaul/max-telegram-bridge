"""Tests for per-user session manager."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from max_bridge.session_manager import SessionManager


@pytest.fixture
def deps():
    db = AsyncMock()
    crypto = MagicMock()
    crypto.decrypt = MagicMock(return_value="decrypted_token")
    on_message = AsyncMock()
    return db, crypto, on_message


class TestSessionManager:
    def test_init(self, deps):
        db, crypto, on_message = deps
        sm = SessionManager(db=db, crypto=crypto, on_message=on_message)
        assert len(sm.sessions) == 0

    @pytest.mark.asyncio
    async def test_start_session_creates_client(self, deps):
        db, crypto, on_message = deps
        db.get_user = AsyncMock(return_value={
            "id": 1,
            "max_token_encrypted": "enc_token",
            "max_device_id": "dev123",
            "status": "active",
        })

        sm = SessionManager(db=db, crypto=crypto, on_message=on_message)

        with patch("max_bridge.session_manager.MaxClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.connect = AsyncMock()
            MockClient.return_value = mock_instance

            await sm.start_session(1)

            MockClient.assert_called_once_with(token="decrypted_token", device_id="dev123")
            assert 1 in sm.sessions

    @pytest.mark.asyncio
    async def test_stop_session(self, deps):
        db, crypto, on_message = deps
        sm = SessionManager(db=db, crypto=crypto, on_message=on_message)

        # Manually add a session
        mock_client = MagicMock()
        mock_task = MagicMock()
        mock_task.cancel = MagicMock()
        sm.sessions[1] = {"client": mock_client, "task": mock_task}

        await sm.stop_session(1)

        mock_client.stop.assert_called_once()
        mock_task.cancel.assert_called_once()
        assert 1 not in sm.sessions

    @pytest.mark.asyncio
    async def test_stop_nonexistent_session(self, deps):
        db, crypto, on_message = deps
        sm = SessionManager(db=db, crypto=crypto, on_message=on_message)
        await sm.stop_session(999)  # Should not raise

    @pytest.mark.asyncio
    async def test_send_message(self, deps):
        db, crypto, on_message = deps
        sm = SessionManager(db=db, crypto=crypto, on_message=on_message)

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock(return_value=True)
        sm.sessions[1] = {"client": mock_client, "task": MagicMock()}

        result = await sm.send_message(1, chat_id=200, text="Hello")
        assert result is True
        mock_client.send_message.assert_called_once_with(200, "Hello")

    @pytest.mark.asyncio
    async def test_send_message_no_session(self, deps):
        db, crypto, on_message = deps
        sm = SessionManager(db=db, crypto=crypto, on_message=on_message)

        result = await sm.send_message(999, chat_id=200, text="Hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_start_all_active(self, deps):
        db, crypto, on_message = deps
        db.get_active_users = AsyncMock(return_value=[
            {"id": 1, "max_token_encrypted": "t1", "max_device_id": "d1", "status": "active"},
            {"id": 2, "max_token_encrypted": "t2", "max_device_id": "d2", "status": "active"},
        ])
        db.get_user = AsyncMock(side_effect=lambda uid: {
            1: {"id": 1, "max_token_encrypted": "t1", "max_device_id": "d1", "status": "active"},
            2: {"id": 2, "max_token_encrypted": "t2", "max_device_id": "d2", "status": "active"},
        }[uid])

        sm = SessionManager(db=db, crypto=crypto, on_message=on_message)

        with patch("max_bridge.session_manager.MaxClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.connect = AsyncMock()
            MockClient.return_value = mock_instance

            await sm.start_all()

            assert len(sm.sessions) == 2


class TestCallbackTasks:
    @pytest.mark.asyncio
    async def test_on_message_task_is_kept_referenced_and_awaited(self, deps):
        """create_task without a strong reference can be collected mid-flight."""
        import asyncio
        import gc

        db, crypto, _ = deps
        db.get_user = AsyncMock(return_value={
            "id": 1, "max_token_encrypted": "enc", "max_device_id": "dev",
            "status": "active",
        })

        delivered = []
        started = asyncio.Event()

        async def on_message(user_id, msg):
            started.set()
            await asyncio.sleep(0.01)  # a real route_message awaits Telegram
            delivered.append((user_id, msg))

        sm = SessionManager(db=db, crypto=crypto, on_message=on_message)

        with patch("max_bridge.session_manager.MaxClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.connect = AsyncMock()
            MockClient.return_value = mock_instance
            await sm.start_session(1)

        # Fire the client's message callback, then drop every local reference
        # and force a collection before the task gets a chance to finish.
        mock_instance.on_message({"text": "hi"})
        await started.wait()
        assert len(sm._pending) == 1
        gc.collect()

        await asyncio.sleep(0.05)
        assert delivered == [(1, {"text": "hi"})]
        assert len(sm._pending) == 0

    @pytest.mark.asyncio
    async def test_callback_exception_is_logged_not_swallowed(self, deps, caplog):
        import asyncio
        import logging

        db, crypto, _ = deps
        db.get_user = AsyncMock(return_value={
            "id": 1, "max_token_encrypted": "enc", "max_device_id": "dev",
            "status": "active",
        })

        async def on_message(user_id, msg):
            raise RuntimeError("boom")

        sm = SessionManager(db=db, crypto=crypto, on_message=on_message)

        with patch("max_bridge.session_manager.MaxClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.connect = AsyncMock()
            MockClient.return_value = mock_instance
            await sm.start_session(1)

        with caplog.at_level(logging.ERROR, logger="max_bridge.session_manager"):
            mock_instance.on_message({"text": "hi"})
            await asyncio.sleep(0.01)

        assert "boom" in caplog.text
        assert len(sm._pending) == 0
