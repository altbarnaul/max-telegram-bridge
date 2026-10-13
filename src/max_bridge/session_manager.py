"""Manages per-user MaxClient WebSocket sessions."""
import asyncio
import logging
from typing import Callable

from .db import Database
from .crypto import TokenCrypto
from .max_client import MaxClient

log = logging.getLogger("max_bridge.session_manager")


class SessionManager:
    def __init__(self, db: Database, crypto: TokenCrypto,
                 on_message: Callable | None = None,
                 on_auth_failed: Callable | None = None):
        self.db = db
        self.crypto = crypto
        self.on_message = on_message
        self.on_auth_failed = on_auth_failed
        self.sessions: dict[int, dict] = {}  # user_id -> {client, task}
        # Strong references to fire-and-forget tasks: the event loop only
        # keeps weak ones, so without this a task can be garbage collected
        # mid-execution and the message it was delivering is lost.
        self._pending: set[asyncio.Task] = set()

    def _spawn(self, coro, label: str) -> None:
        task = asyncio.create_task(coro)
        self._pending.add(task)

        def _done(t: asyncio.Task) -> None:
            self._pending.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                log.error(f"{label} failed: {exc}", exc_info=exc)

        task.add_done_callback(_done)

    async def start_session(self, user_id: int):
        if user_id in self.sessions:
            log.warning(f"Session already exists for user {user_id}")
            return

        user = await self.db.get_user(user_id)
        if not user:
            log.error(f"User {user_id} not found")
            return

        token = self.crypto.decrypt(user["max_token_encrypted"])
        device_id = user["max_device_id"]

        client = MaxClient(token=token, device_id=device_id)

        # Wire callbacks
        def on_msg(msg):
            if self.on_message:
                self._spawn(self.on_message(user_id, msg), f"on_message(user={user_id})")

        def on_ready(data):
            log.info(f"User {user_id} MAX session ready, my_id={data.get('my_id')}")
            self._spawn(self._on_user_ready(user_id, data), f"on_user_ready(user={user_id})")

        def on_token(new_token):
            self._spawn(self._save_token(user_id, new_token, device_id),
                        f"save_token(user={user_id})")

        def on_auth_failed(payload):
            self._spawn(self._on_auth_failed(user_id), f"on_auth_failed(user={user_id})")

        client.on_message = on_msg
        client.on_ready = on_ready
        client.on_token_refreshed = on_token
        client.on_auth_failed = on_auth_failed

        task = asyncio.create_task(self._run_session(user_id, client))
        self.sessions[user_id] = {"client": client, "task": task}
        log.info(f"Started session for user {user_id}")

    async def _run_session(self, user_id: int, client: MaxClient):
        try:
            await client.connect()
        except asyncio.CancelledError:
            log.info(f"Session {user_id} cancelled")
        except Exception as e:
            log.error(f"Session {user_id} error: {e}", exc_info=True)
            await self.db.update_user_status(user_id, "error")
        finally:
            self.sessions.pop(user_id, None)

    async def _on_user_ready(self, user_id: int, data: dict):
        max_user_id = data.get("my_id")
        if max_user_id:
            await self.db.update_user_max_id(user_id, max_user_id)

        # Save chat types from the client
        client_data = self.sessions.get(user_id)
        if client_data:
            client = client_data["client"]
            for chat_id, chat_type in client.chat_types.items():
                title = client.chat_titles.get(chat_id, "")
                await self.db.save_max_chat(user_id, chat_id, chat_type, title)

    async def _save_token(self, user_id: int, token: str, device_id: str):
        await self.db.update_user_token(user_id, self.crypto.encrypt(token), device_id)
        log.info(f"Saved rotated MAX token for user {user_id}")

    async def _on_auth_failed(self, user_id: int):
        log.error(f"MAX rejected the token of user {user_id}, session stopped")
        await self.db.update_user_status(user_id, "expired")
        await self.stop_session(user_id)
        if self.on_auth_failed:
            await self.on_auth_failed(user_id)

    async def stop_session(self, user_id: int):
        session = self.sessions.pop(user_id, None)
        if not session:
            return
        session["client"].stop()
        session["task"].cancel()
        log.info(f"Stopped session for user {user_id}")

    async def send_message(self, user_id: int, chat_id: int, text: str) -> bool:
        session = self.sessions.get(user_id)
        if not session:
            log.warning(f"No session for user {user_id}")
            return False
        return await session["client"].send_message(chat_id, text)

    def get_client(self, user_id: int) -> MaxClient | None:
        session = self.sessions.get(user_id)
        return session["client"] if session else None

    async def start_all(self):
        """Start sessions for all active users."""
        users = await self.db.get_active_users()
        log.info(f"Starting {len(users)} active sessions...")
        for user in users:
            await self.start_session(user["id"])

    async def stop_all(self):
        """Stop all sessions."""
        user_ids = list(self.sessions.keys())
        for uid in user_ids:
            await self.stop_session(uid)
