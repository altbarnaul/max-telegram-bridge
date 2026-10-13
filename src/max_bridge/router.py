"""Route MAX messages to correct Telegram forum topics."""
import logging

import aiohttp
from aiogram.types import BufferedInputFile, InputMediaPhoto

from .db import Database

log = logging.getLogger("max_bridge.router")

CAPTION_LIMIT = 1024
MEDIA_GROUP_LIMIT = 10

# MAX chat type → topic field in user_groups
CHAT_TYPE_TO_TOPIC = {
    "DIALOG": "topic_dm",
    "CHAT": "topic_groups",
    "GROUP": "topic_groups",
    "MULTIDIALOG": "topic_groups",
    "CHANNEL": "topic_channels",
}


class MessageRouter:
    def __init__(self, db: Database, bot, session_manager):
        self.db = db
        self.bot = bot
        self.session_manager = session_manager

    @staticmethod
    def format_message(sender_name: str, text: str, chat_title: str | None = None) -> str:
        if chat_title:
            msg = f"[{chat_title}]\n{sender_name}:\n{text}"
        else:
            msg = f"{sender_name}:\n{text}"
        msg = msg.rstrip("\n")
        if len(msg) > 4096:
            msg = msg[:4093] + "..."
        return msg

    @staticmethod
    async def download(url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"Referer": "https://web.max.ru/"}) as resp:
                resp.raise_for_status()
                return await resp.read()

    async def _send_photos(self, chat_id: int, topic_id: int, urls: list[str],
                           caption: str | None):
        """Send photos as an album; returns the first Telegram message."""
        files = []
        for i, url in enumerate(urls, 1):
            try:
                files.append(BufferedInputFile(await self.download(url), f"photo_{i}.jpg"))
            except Exception as e:
                log.error(f"Failed to download MAX photo: {e}")
        if not files:
            return None

        first = None
        for start in range(0, len(files), MEDIA_GROUP_LIMIT):
            chunk = files[start:start + MEDIA_GROUP_LIMIT]
            chunk_caption = caption if start == 0 else None
            if len(chunk) == 1:
                sent = [await self.bot.send_photo(
                    chat_id=chat_id, photo=chunk[0], caption=chunk_caption,
                    message_thread_id=topic_id,
                )]
            else:
                # Captions go in the constructor: aiogram 3.31+ media types are frozen
                media = [InputMediaPhoto(media=f, caption=chunk_caption if i == 0 else None)
                         for i, f in enumerate(chunk)]
                sent = await self.bot.send_media_group(
                    chat_id=chat_id, media=media, message_thread_id=topic_id,
                )
            first = first or sent[0]
        if len(files) < len(urls):
            await self.bot.send_message(
                chat_id=chat_id, message_thread_id=topic_id,
                text=f"[Не загрузилось фото: {len(urls) - len(files)} из {len(urls)}]",
            )
        return first

    async def _chat_info(self, user_id: int, chat_id: int, client) -> tuple[str, str]:
        """Type and title of a MAX chat: from the database, else asked from MAX.

        The login snapshot only covers the latest 50 chats, so older ones are
        looked up the first time they get a message.
        """
        info = await self.db.get_max_chat(user_id, chat_id)
        if info:
            return info["chat_type"], info["title"]
        if client:
            try:
                found = (await client.fetch_chats([chat_id])).get(chat_id)
            except Exception as e:
                log.warning(f"Chat info lookup failed for {chat_id}: {e}")
                found = None
            if found and found["type"]:
                await self.db.save_max_chat(user_id, chat_id, found["type"], found["title"])
                return found["type"], found["title"]
        # MAX gives group chats and channels negative ids, dialogs positive ones
        return ("CHAT" if isinstance(chat_id, int) and chat_id < 0 else "DIALOG"), ""

    async def _sender_name(self, user_id: int, sender_id: int, client) -> str:
        """Sender's name: contacts, then earlier messages, then asked from MAX."""
        name = client.contacts.get(sender_id) if client else None
        if not name:
            name = await self.db.get_sender_name(user_id, sender_id)
        if not name and client and sender_id:
            try:
                name = (await client.fetch_contact_names([sender_id])).get(sender_id)
            except Exception as e:
                log.warning(f"Name lookup failed for MAX user {sender_id}: {e}")
        return name or f"ID:{sender_id}"

    async def route_message(self, user_id: int, msg: dict):
        text = msg.get("text", "")
        photos = msg.get("photos") or []
        if not text and not photos:
            return

        sender_id = msg.get("sender_id")
        chat_id = msg.get("chat_id")

        # Get user's TG group config
        group = await self.db.get_user_group(user_id)
        if not group:
            log.debug(f"No group configured for user {user_id}")
            return

        client = self.session_manager.get_client(user_id)
        chat_type, chat_title = await self._chat_info(user_id, chat_id, client)

        # Determine target topic
        topic_field = CHAT_TYPE_TO_TOPIC.get(chat_type, "topic_dm")
        topic_id = group.get(topic_field, group["topic_dm"])

        sender_name = await self._sender_name(user_id, sender_id, client)

        # Format based on chat type
        show_title = chat_title if chat_type != "DIALOG" else None
        formatted = self.format_message(sender_name, text, show_title)

        # Send to TG topic
        try:
            # Sent as plain text on purpose: the forwarded text carries no
            # markup of ours, and with parse_mode="HTML" any MAX message
            # containing "<" would be rejected by Telegram ("can't parse
            # entities") and lost.
            tg_msg = None
            if photos:
                # A caption longer than Telegram allows goes as its own message
                fits = len(formatted) <= CAPTION_LIMIT
                if not fits:
                    tg_msg = await self.bot.send_message(
                        chat_id=group["chat_id"], text=formatted,
                        message_thread_id=topic_id,
                    )
                album = await self._send_photos(
                    group["chat_id"], topic_id, photos, formatted if fits else None,
                )
                tg_msg = tg_msg or album
            if tg_msg is None:
                if photos:
                    formatted += "\n" + "\n".join(["[Изображение]"] * len(photos))
                tg_msg = await self.bot.send_message(
                    chat_id=group["chat_id"],
                    text=formatted,
                    message_thread_id=topic_id,
                )

            # Save mapping for reverse routing
            await self.db.save_mapping(
                user_id=user_id,
                tg_message_id=tg_msg.message_id,
                tg_chat_id=group["chat_id"],
                max_chat_id=chat_id,
                max_sender_id=sender_id,
                sender_name=sender_name,
                max_message_id=msg.get("message_id", ""),
                chat_type=chat_type.lower() if chat_type else "dm",
            )
            log.info(f"Routed: user={user_id} MAX:{chat_id}({chat_type}) → TG topic {topic_id}")

        except Exception as e:
            log.error(f"Failed to route message for user {user_id}: {e}")
