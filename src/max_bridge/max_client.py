"""MAX messenger WebSocket client."""
import asyncio
import json
import logging
from enum import IntEnum
from typing import Callable

import time

import aiohttp

log = logging.getLogger("max_bridge.max_client")


class OpCode(IntEnum):
    HEARTBEAT = 1
    PONG = 5
    HANDSHAKE = 6
    AUTH_SNAPSHOT = 19
    CONTACT_INFO = 32
    CONTACT_INFO_BY_PHONE = 46
    CHAT_INFO = 48
    MESSAGE = 64
    MESSAGE_STATUS = 65
    DISPATCH = 128
    PRESENCE = 132
    GET_QR = 288
    GET_QR_STATUS = 289
    LOGIN_BY_QR = 291


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

WS_HEADERS = {
    "Origin": "https://web.max.ru",
    "User-Agent": BROWSER_USER_AGENT,
    "Cache-Control": "no-cache",
}

# MAX refuses QR login ("qr_login.disabled") to a bare userAgent with an old
# appVersion, so the bridge presents itself as a current web client.
WEB_USER_AGENT = {
    "deviceType": "WEB",
    "locale": "ru",
    "deviceLocale": "ru",
    "osVersion": "macOS",
    "deviceName": "Chrome",
    "headerUserAgent": BROWSER_USER_AGENT,
    "appVersion": "26.8.4",
    "screen": "1080x1920 1.0x",
    "timezone": "Europe/Moscow",
}


def web_handshake_payload(device_id: str) -> dict:
    return {"deviceId": device_id, "userAgent": WEB_USER_AGENT}


# Service events MAX sends as a CONTROL attach instead of text
CONTROL_EVENTS = {
    "new": "создан чат",
    "add": "добавлены участники",
    "remove": "участник удалён",
    "leave": "участник вышел",
    "joinByLink": "вступление по ссылке",
    "title": "изменено название",
    "icon": "изменена аватарка",
}


class MaxRequestError(Exception):
    """MAX answered a request with an error frame."""

    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__(
            payload.get("localizedMessage") or payload.get("message")
            or payload.get("error") or "MAX request failed"
        )


class MaxClient:
    """WebSocket client for MAX messenger user API."""

    WS_URL = "wss://ws-api.oneme.ru/websocket"
    WS_PING_INTERVAL = 30
    SESSION_MAX_AGE = 24 * 3600
    REQUEST_TIMEOUT = 15

    def __init__(self, token: str, device_id: str):
        self.token = token
        self.device_id = device_id
        self.my_id: int | None = None
        self.contacts: dict[int, str] = {}
        self.chat_types: dict[int, str] = {}
        self.chat_titles: dict[int, str] = {}
        self._seq = 0
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False
        self._authenticated = False
        # seq -> (opcode, future) of requests waiting for MAX's answer
        self._requests: dict[int, tuple[int, asyncio.Future]] = {}

        # Callbacks — set by the bridge
        self.on_message: Callable | None = None
        self.on_ready: Callable | None = None
        self.on_auth_failed: Callable | None = None
        self.on_token_refreshed: Callable | None = None

    def _build_frame(self, opcode: int, payload: dict) -> dict:
        frame = {
            "ver": 11,
            "cmd": 0,
            "seq": self._seq,
            "opcode": int(opcode),
            "payload": payload,
        }
        self._seq += 1
        return frame

    def _parse_message(self, payload: dict) -> dict:
        msg_body = payload.get("message", {})
        text = msg_body.get("text", "")

        # Photos are forwarded as files; other attachments become placeholders
        attaches = msg_body.get("attaches", [])
        attach_descriptions = []
        photos = []
        for att in attaches:
            if not isinstance(att, dict):
                continue
            att_type = att.get("_type", "").upper()
            if att_type == "STICKER":
                tags = att.get("tags", [])
                emoji = " ".join(tags[:3]) if tags else ""
                attach_descriptions.append(f"[Стикер {emoji}]".strip())
            elif att_type in ("PHOTO", "IMAGE"):
                # MAX sends photos as "PHOTO" with a direct "baseUrl"; the old
                # "IMAGE"-only check fell through to a bare "[PHOTO]" stub.
                url = att.get("baseUrl") or att.get("url")
                if url:
                    photos.append(url)
                else:
                    attach_descriptions.append("[Изображение]")
            elif att_type == "VIDEO":
                attach_descriptions.append("[Видео]")
            elif att_type == "AUDIO":
                attach_descriptions.append("[Аудио]")
            elif att_type == "FILE":
                name = att.get("name", "файл")
                attach_descriptions.append(f"[Файл: {name}]")
            elif att_type == "VOICE":
                attach_descriptions.append("[Голосовое]")
            elif att_type == "CONTROL":
                event = att.get("event", "")
                note = (att.get("shortMessage") or att.get("message")
                        or CONTROL_EVENTS.get(event) or event)
                attach_descriptions.append(f"[{note}]" if note else "[Служебное]")
            elif att_type:
                attach_descriptions.append(f"[{att_type}]")

        if not text and attach_descriptions:
            text = "\n".join(attach_descriptions)
        elif text and attach_descriptions:
            text = text + "\n" + "\n".join(attach_descriptions)

        return {
            "chat_id": payload.get("chatId"),
            "sender_id": msg_body.get("sender"),
            "text": text,
            "photos": photos,
            "message_id": msg_body.get("id"),
            "timestamp": msg_body.get("time"),
            "raw": payload,
        }

    def _build_send_payload(self, chat_id: int, text: str) -> dict:
        cid = -int(time.time() * 1000)
        return {
            "chatId": chat_id,
            "message": {
                "text": text,
                "type": "USER",
                "cid": cid,
                "attaches": [],
            },
        }

    async def request(self, opcode: int, payload: dict) -> dict:
        """Send a request and wait for MAX's answer to it.

        Raises ConnectionError when MAX is not connected or the connection
        drops before the answer, MaxRequestError when MAX answers with an error.
        """
        if not self._ws or self._ws.closed or not self._authenticated:
            raise ConnectionError("MAX is not connected")
        frame = self._build_frame(opcode, payload)
        seq = frame["seq"]
        future = asyncio.get_running_loop().create_future()
        self._requests[seq] = (int(opcode), future)
        try:
            await self._ws.send_json(frame)
            cmd, answer = await asyncio.wait_for(future, self.REQUEST_TIMEOUT)
        finally:
            self._requests.pop(seq, None)
        if cmd != 1:
            raise MaxRequestError(answer)
        return answer

    def _fail_requests(self) -> None:
        for _, future in self._requests.values():
            if not future.done():
                future.set_exception(ConnectionError("MAX connection closed"))
        self._requests.clear()

    @staticmethod
    def contact_name(contact: dict) -> str:
        """Display name of a MAX contact: the name saved in the address book
        comes first in "names", the user's own MAX name after it."""
        names = contact.get("names", [])
        if isinstance(names, list) and names and isinstance(names[0], dict):
            first = names[0]
            name = f"{first.get('firstName', '')} {first.get('lastName', '')}".strip()
            if name:
                return name
            if first.get("name"):
                return first["name"]
        return contact.get("name") or contact.get("displayName", "")

    async def fetch_contact_names(self, user_ids: list[int]) -> dict[int, str]:
        """Look up names of users missing from the login snapshot."""
        answer = await self.request(OpCode.CONTACT_INFO, {"contactIds": list(user_ids)})
        names = {}
        for contact in answer.get("contacts") or []:
            if not isinstance(contact, dict):
                continue
            name = self.contact_name(contact)
            if contact.get("id") and name:
                names[contact["id"]] = name
        self.contacts.update(names)
        return names

    async def fetch_chats(self, chat_ids: list[int]) -> dict[int, dict]:
        """Look up type and title of chats missing from the login snapshot."""
        answer = await self.request(OpCode.CHAT_INFO, {"chatIds": list(chat_ids)})
        chats = {}
        for chat in answer.get("chats") or []:
            if not isinstance(chat, dict) or not chat.get("id"):
                continue
            chat_id = chat["id"]
            chats[chat_id] = {"type": chat.get("type", ""), "title": chat.get("title", "")}
            if chat.get("type"):
                self.chat_types[chat_id] = chat["type"]
            if chat.get("title"):
                self.chat_titles[chat_id] = chat["title"]
        return chats

    async def find_by_phone(self, phone: str) -> tuple[int, str] | None:
        """Find a MAX user by phone ("+79991234567"); None if there is none."""
        try:
            answer = await self.request(OpCode.CONTACT_INFO_BY_PHONE, {"phone": phone})
        except MaxRequestError:
            return None
        contact = answer.get("contact")
        if not isinstance(contact, dict) or not contact.get("id"):
            return None
        return contact["id"], self.contact_name(contact) or phone

    def dialog_chat_id(self, user_id: int) -> int:
        """MAX derives a dialog's chat id from its two members' ids."""
        return self.my_id ^ user_id

    async def send_text(self, chat_id: int, text: str) -> dict:
        """Send a message and wait until MAX accepts it (raises otherwise)."""
        return await self.request(OpCode.MESSAGE, self._build_send_payload(chat_id, text))

    async def send_message(self, chat_id: int, text: str) -> bool:
        """Send a text message to a MAX chat."""
        if not self._ws or self._ws.closed:
            log.error("Cannot send: WebSocket not connected")
            return False
        payload = self._build_send_payload(chat_id, text)
        frame = self._build_frame(OpCode.MESSAGE, payload)
        try:
            await self._ws.send_json(frame)
        except Exception as e:
            # A connection dropping mid-send must not escape into the
            # Telegram handler; report it as a failed send instead.
            log.error(f"Failed to send message to chat {chat_id}: {e}")
            return False
        log.debug(f"Sent message to chat {chat_id}: {text[:50]}")
        return True

    async def _handle_frame(self, data):
        """Process an incoming WebSocket frame."""
        if not isinstance(data, dict):
            log.debug(f"Non-dict frame skipped: {str(data)[:100]}")
            return
        op = data.get("opcode")
        cmd = data.get("cmd")
        payload = data.get("payload", {})

        # Pushes from MAX come with cmd=0 and their own seq, so only an
        # answer (cmd 1 = ok, 3 = error) can resolve a waiting request.
        waiting = self._requests.get(data.get("seq"))
        if cmd != 0 and waiting and waiting[0] == op and not waiting[1].done():
            waiting[1].set_result((cmd, payload if isinstance(payload, dict) else {}))
            return
        log.debug(f"Frame: op={op} cmd={cmd} payload_keys={list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__}")

        if op == OpCode.HANDSHAKE and cmd == 1:
            log.info("Handshake OK, sending auth...")
            auth_frame = self._build_frame(OpCode.AUTH_SNAPSHOT, {
                "chatsCount": 50,
                "interactive": True,
                "token": self.token,
            })
            await self._ws.send_json(auth_frame)

        elif op == OpCode.AUTH_SNAPSHOT and cmd != 1:
            log.error(f"Auth failed (cmd={cmd}): {json.dumps(payload, default=str)[:500]}")
            if isinstance(payload, dict) and payload.get("error") == "login.token":
                # The token is revoked for good: reconnecting with it only
                # spins forever while the user sees nothing. Stop and report.
                self._running = False
                if self._ws and not self._ws.closed:
                    await self._ws.close()
                if self.on_auth_failed:
                    self.on_auth_failed(payload)
            return

        elif op == OpCode.AUTH_SNAPSHOT and cmd == 1:
            self._authenticated = True
            profile = payload.get("profile") or {}
            # Current web clients get the account under profile.contact;
            # appVersion 25.x got it as profile.id.
            self.my_id = profile.get("id") or (profile.get("contact") or {}).get("id")
            # MAX rotates the web token and hands the current one back on
            # every login. Keeping only the token captured at pairing time
            # let it expire after a few months (the bridge died silently on
            # 2026-09-03), so adopt and persist every new one.
            new_token = payload.get("token")
            if isinstance(new_token, str) and new_token and new_token != self.token:
                self.token = new_token
                log.info("MAX token rotated by server")
                if self.on_token_refreshed:
                    self.on_token_refreshed(new_token)
            # Snapshot structure at DEBUG only: samples carry contacts' personal data
            chats = payload.get("chats", [])
            contacts_raw = payload.get("contacts", [])
            log.info(f"Auth snapshot: {len(chats)} chats, {len(contacts_raw)} raw contacts")
            if chats:
                sample = chats[0] if isinstance(chats[0], dict) else {}
                participants = sample.get("participants", {})
                log.debug(f"Chat type={type(participants).__name__}, sample_keys={list(sample.keys())[:5]}")
                # Dump first participant entry
                if isinstance(participants, dict):
                    for k, v in list(participants.items())[:1]:
                        log.debug(f"Participant key={k} value={json.dumps(v, default=str)[:300]}")
                elif isinstance(participants, list) and participants:
                    log.debug(f"Participant[0]={json.dumps(participants[0], default=str)[:300]}")
            if contacts_raw:
                sample_contact = contacts_raw[0] if isinstance(contacts_raw, list) else next(iter(contacts_raw.values())) if isinstance(contacts_raw, dict) else contacts_raw
                log.debug(f"Contact type={type(contacts_raw).__name__}, keys={list(sample_contact.keys()) if isinstance(sample_contact, dict) else 'N/A'}")
                log.debug(f"Contact sample full: {json.dumps(sample_contact, default=str)[:600]}")
            self.extract_snapshot_data(payload)
            log.info(f"Authenticated as user {self.my_id}, {len(self.contacts)} contacts loaded, {len(self.chat_types)} chat types extracted")
            if self.on_ready:
                self.on_ready({"my_id": self.my_id, "contacts": self.contacts})

        elif op == OpCode.MESSAGE and cmd == 1:
            # Server response to our sent message
            log.debug(f"MESSAGE response: {json.dumps(payload, default=str)[:500]}")

        elif op == OpCode.MESSAGE and cmd == 0:
            msg = self._parse_message(payload)
            # Skip own messages
            if self.my_id and msg["sender_id"] == self.my_id:
                return
            if self.on_message:
                self.on_message(msg)

        elif op == OpCode.DISPATCH:
            # Log full DISPATCH payload for debugging
            log.debug(f"DISPATCH full payload: {json.dumps(payload, default=str)[:1000]}")
            if "message" in payload and "chatId" in payload:
                msg = self._parse_message(payload)
                log.debug(f"DISPATCH parsed: chat_id={msg['chat_id']} sender={msg['sender_id']} my_id={self.my_id} text={msg['text'][:50] if msg['text'] else '<empty>'}")
                if self.my_id and msg["sender_id"] == self.my_id:
                    log.debug("Skipping own message")
                    return
                if self.on_message:
                    log.info(f"Calling on_message for chat {msg['chat_id']}")
                    self.on_message(msg)
                else:
                    log.warning("on_message callback not set!")

    def extract_contacts(self, snapshot_payload: dict) -> dict[int, str]:
        """Extract user_id -> name mapping from auth snapshot."""
        contacts = {}

        # Extract from top-level contacts array
        raw_contacts = snapshot_payload.get("contacts", [])
        if isinstance(raw_contacts, list):
            for c in raw_contacts:
                if not isinstance(c, dict):
                    continue
                uid = c.get("id") or c.get("userId")
                name = self.contact_name(c)
                if uid and name:
                    contacts[uid] = name

        # Extract from chat participants
        for chat in snapshot_payload.get("chats", []):
            if not isinstance(chat, dict):
                continue
            participants = chat.get("participants", {})
            # participants can be dict {userId: participantInfo} or list
            if isinstance(participants, dict):
                for uid_str, p in participants.items():
                    if isinstance(p, dict):
                        name = p.get("name") or p.get("displayName", "")
                        if name:
                            try:
                                contacts[int(uid_str)] = name
                            except (ValueError, TypeError):
                                pass
            elif isinstance(participants, list):
                for p in participants:
                    if not isinstance(p, dict):
                        continue
                    uid = p.get("userId") or p.get("id")
                    name = p.get("name") or p.get("displayName", "")
                    if uid and name:
                        contacts[uid] = name

            # For DM chats: map participant IDs from chat
            # participants is {userId_str: joinTimestamp}
            chat_id = chat.get("chatId") or chat.get("id")
            if isinstance(participants, dict) and chat_id:
                for uid_str in participants:
                    try:
                        uid = int(uid_str)
                        # If this participant is already in contacts, also map chatId
                        if uid in contacts and uid != self.my_id:
                            contacts.setdefault(chat_id, contacts[uid])
                    except (ValueError, TypeError):
                        pass

            # Chat title as fallback
            title = chat.get("title", "")
            if title and chat_id:
                contacts.setdefault(chat_id, title)

        return contacts

    def extract_snapshot_data(self, snapshot_payload: dict) -> None:
        """Extract contacts, chat types, and chat titles from auth snapshot."""
        # Extract contacts
        self.contacts = self.extract_contacts(snapshot_payload)

        # Extract chat types and titles
        for chat in snapshot_payload.get("chats", []):
            if not isinstance(chat, dict):
                continue
            chat_id = chat.get("chatId") or chat.get("id")
            if not chat_id:
                continue

            chat_type = chat.get("type", "")
            if chat_type:
                self.chat_types[chat_id] = chat_type

            chat_title = chat.get("title", "")
            if chat_title:
                self.chat_titles[chat_id] = chat_title

    async def _heartbeat(self):
        """Send heartbeat every 30 seconds; recycle the connection daily."""
        started = time.monotonic()
        while self._running:
            await asyncio.sleep(30)
            if self._ws and not self._ws.closed and time.monotonic() - started > self.SESSION_MAX_AGE:
                # A fresh login is what yields a rotated token, so a socket
                # that stays up for months would otherwise ride its token
                # into expiry.
                log.info("Recycling MAX connection to refresh the token")
                await self._ws.close()
                break
            if self._ws and not self._ws.closed:
                frame = self._build_frame(OpCode.HEARTBEAT, {"interactive": False})
                try:
                    await self._ws.send_json(frame)
                except Exception as e:
                    # The read loop handles the reconnect; just end quietly.
                    log.warning(f"Heartbeat send failed: {e}")
                    break
            else:
                break

    async def connect(self):
        """Main connection loop with auto-reconnect."""
        self._running = True
        delay = 5

        async with aiohttp.ClientSession() as session:
            while self._running:
                self._authenticated = False
                try:
                    log.info("Connecting to MAX WebSocket...")
                    # heartbeat= makes aiohttp send WebSocket pings and drop the
                    # connection when no pong arrives. Without it a half-open
                    # connection never yields a frame and never errors, so the
                    # read loop below would wait forever and the user's bridge
                    # would go silently dead until the process restarts.
                    async with session.ws_connect(
                        self.WS_URL, headers=WS_HEADERS, heartbeat=self.WS_PING_INTERVAL,
                    ) as ws:
                        self._ws = ws
                        self._seq = 0

                        # Send handshake
                        handshake = self._build_frame(
                            OpCode.HANDSHAKE, web_handshake_payload(self.device_id),
                        )
                        await ws.send_json(handshake)

                        # Start heartbeat
                        self._heartbeat_task = asyncio.create_task(self._heartbeat())

                        # Listen for frames
                        async for msg in ws:
                            log.debug(f"WS msg type={msg.type}")
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = msg.json()
                                except Exception:
                                    log.debug(f"Non-JSON frame: {msg.data[:100]}")
                                    continue
                                await self._handle_frame(data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                log.warning("WebSocket closed")
                                break
                        log.info(f"MAX connection ended: close_code={ws.close_code}")

                except Exception as e:
                    log.error(f"Connection error: {e}", exc_info=True)

                finally:
                    self._fail_requests()
                    if self._heartbeat_task:
                        self._heartbeat_task.cancel()
                        self._heartbeat_task = None

                if self._authenticated:
                    # Only a session that actually authenticated proves the
                    # token still works; resetting the backoff on a bare TCP
                    # connect made a rejected token reconnect every 5s forever.
                    delay = 5

                if self._running:
                    log.info(f"Reconnecting in {delay}s...")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 300)

    def stop(self):
        """Stop the client."""
        self._running = False
