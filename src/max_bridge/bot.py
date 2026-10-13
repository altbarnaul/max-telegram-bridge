"""aiogram 3.x Telegram bot for MAX Bridge."""
import asyncio
import html
import io
import logging
import uuid

import segno
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, ChatMemberUpdated, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile,
)
from aiogram.filters import Command, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER

from .db import Database
from .crypto import TokenCrypto
from .outbound import MAX_CHOICES, Target, find_targets, parse_phone, split_command
from .qr_login import QrExpiredError, qr_login
from .router import CHAT_TYPE_TO_TOPIC
from . import analytics

log = logging.getLogger("max_bridge.bot")

LOGIN_BUTTON = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="📱 Подключить MAX", callback_data="qr_login"),
]])


def qr_png(link: str) -> bytes:
    buf = io.BytesIO()
    segno.make(link, error="m").save(buf, kind="png", scale=10, border=2)
    return buf.getvalue()


class BotService:
    def __init__(self, bot_token: str, db: Database, crypto: TokenCrypto,
                 session_manager=None, admin_tg_id: int = 0):
        self.bot = Bot(token=bot_token)
        self.dp = Dispatcher()
        self.db = db
        self.crypto = crypto
        self.session_manager = session_manager
        self.admin_tg_id = admin_tg_id
        # Guards topic creation so /setup and the "bot added" event cannot
        # both create a set of topics for the same user.
        self._setup_lock = asyncio.Lock()
        # Telegram users with a QR login in flight, so a second /login does
        # not open a competing one.
        self._qr_logins: set[int] = set()
        # /to commands waiting for the user to pick one of several matches:
        # key -> (bridge user id, matches, text)
        self._to_choices: dict[str, tuple[int, list[Target], str]] = {}
        self._register_handlers()

    def _register_handlers(self):
        r = Router()
        r.message.register(self.handle_start, Command("start"), F.chat.type == "private")
        r.message.register(self.handle_login, Command("login"), F.chat.type == "private")
        r.message.register(self.handle_status, Command("status"), F.chat.type == "private")
        r.message.register(self.handle_stop, Command("stop"), F.chat.type == "private")
        r.message.register(self.handle_help, Command("help"), F.chat.type == "private")
        r.message.register(self.handle_support, Command("support"), F.chat.type == "private")
        r.message.register(self.handle_admin_reply, F.chat.type == "private", F.reply_to_message)
        r.message.register(self.handle_setup, Command("setup"), F.chat.type.in_({"group", "supergroup"}))
        r.message.register(self.handle_to, Command("to"), F.chat.type.in_({"group", "supergroup"}))
        r.callback_query.register(self.handle_callback)
        r.my_chat_member.register(
            self.handle_bot_added,
            ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER),
        )
        r.message.register(self.handle_group_reply, F.chat.type.in_({"group", "supergroup"}))
        self.dp.include_router(r)

    # ─── Onboarding flow ────────────────────────────────

    async def handle_start(self, message: Message):
        tg_id = message.from_user.id
        user = await self.db.get_user_by_telegram_id(tg_id)

        if user and user["status"] in ("active", "pending"):
            group = await self.db.get_user_group(user["id"])
            if group and user["status"] == "active":
                await message.answer(
                    "✅ MAX Bridge работает.\n\n"
                    "/status — проверить статус\n"
                    "/stop — приостановить"
                )
                return
            if not group:
                # Logged in to MAX, group not set up yet
                await self._send_step3(message.chat.id)
                return

        if user:
            # Known user that is not running (paused, or MAX revoked the
            # login) → a fresh QR login is all that is needed
            await self._run_qr_login(message.chat.id, tg_id)
            return

        # New user — start onboarding from step 1
        analytics.track(tg_id, "onboarding_step", {"step": 1})
        analytics.identify(tg_id, username=message.from_user.username or "", full_name=message.from_user.full_name or "")
        await self._send_step1(message.chat.id)

    async def handle_login(self, message: Message):
        await self._run_qr_login(message.chat.id, message.from_user.id)

    async def _send_step1(self, chat_id: int):
        """Step 1: Send full guide + log in to MAX."""
        await self.bot.send_message(
            chat_id,
            "<b>MAX Bridge — пересылка сообщений из MAX в Telegram</b>\n\n"
            "Бот пересылает все сообщения из MAX в вашу Telegram-группу "
            "с разделением по топикам: личные, группы, каналы, комментарии. "
            "Можно отвечать прямо из Telegram.\n\n"
            "<b>Как подключить (3 шага):</b>\n\n"
            "<b>Шаг 1.</b> Нажмите кнопку ниже — бот пришлёт QR-код. "
            "Отсканируйте его камерой телефона, на котором установлен MAX, "
            "и подтвердите вход в приложении MAX\n\n"
            "<b>Шаг 2.</b> Создайте группу в Telegram, добавьте бота, "
            "включите «Темы» и дайте боту права админа с «Управлением темами»\n\n"
            "<b>Шаг 3.</b> Напишите <code>/setup</code> в группе — "
            "бот создаст топики и начнёт пересылку",
            parse_mode="HTML",
            reply_markup=LOGIN_BUTTON,
        )

    async def _run_qr_login(self, chat_id: int, tg_id: int):
        """Log the bridge in to MAX as its own device, approved by QR.

        A token copied from the user's browser gets the bridge and web.max.ru
        logged out together as soon as the browser reconnects, so the bridge
        always gets a session of its own.
        """
        if tg_id in self._qr_logins:
            await self.bot.send_message(chat_id, "QR-код уже отправлен выше — отсканируйте его.")
            return
        self._qr_logins.add(tg_id)
        analytics.track(tg_id, "onboarding_step", {"step": 1, "method": "qr"})
        shown: list[int] = []

        async def show_qr(link: str):
            await self._delete_messages(chat_id, shown)
            sent = await self.bot.send_photo(
                chat_id,
                BufferedInputFile(qr_png(link), filename="max-login.png"),
                caption=(
                    "<b>Вход в MAX</b>\n\n"
                    "Отсканируйте QR камерой телефона, на котором установлен MAX, "
                    f"или откройте ссылку на этом телефоне: {html.escape(link)}\n\n"
                    "Подтвердите вход в приложении MAX. "
                    "Код действует 2 минуты, потом бот пришлёт новый."
                ),
                parse_mode="HTML",
            )
            shown.append(sent.message_id)

        device_id = str(uuid.uuid4())
        try:
            token = await qr_login(device_id, show_qr)
        except QrExpiredError:
            await self.bot.send_message(
                chat_id, "⌛ QR-код истёк, вход не подтверждён. Чтобы попробовать снова — /login",
            )
            return
        except Exception as e:
            log.warning(f"QR login for TG user {tg_id} failed: {e}")
            await self.bot.send_message(
                chat_id,
                f"❌ Вход в MAX не выполнен: {html.escape(str(e))}\n\nЧтобы попробовать снова — /login",
            )
            return
        finally:
            self._qr_logins.discard(tg_id)
            await self._delete_messages(chat_id, shown)

        await self._attach_max_login(chat_id, tg_id, token, device_id)

    async def _delete_messages(self, chat_id: int, message_ids: list[int]):
        for message_id in message_ids:
            try:
                await self.bot.delete_message(chat_id, message_id)
            except Exception as e:
                log.debug(f"Could not delete message {message_id}: {e}")
        message_ids.clear()

    async def _attach_max_login(self, chat_id: int, tg_id: int, token: str, device_id: str):
        encrypted = self.crypto.encrypt(token)
        user = await self.db.get_user_by_telegram_id(tg_id)
        if user:
            user_id = user["id"]
            await self.db.update_user_token(user_id, encrypted, device_id)
        else:
            user_id = await self.db.create_user(
                telegram_id=tg_id, max_token_encrypted=encrypted, max_device_id=device_id,
            )
        log.info(f"MAX QR login completed for user {user_id} (TG:{tg_id})")

        if await self.db.get_user_group(user_id):
            # The group and topics are still there: resume forwarding into them
            await self.db.update_user_status(user_id, "active")
            if self.session_manager:
                await self.session_manager.stop_session(user_id)
                await self.session_manager.start_session(user_id)
            await self.bot.send_message(chat_id, "✅ MAX подключён, пересылка возобновлена.")
            return

        await self.db.update_user_status(user_id, "pending")
        await self.bot.send_message(chat_id, "✅ MAX подключён.")
        await self._send_step3(chat_id)

    async def _send_step3(self, chat_id: int):
        """Step 3: Create group."""
        analytics.track(chat_id, "onboarding_step", {"step": 3})
        await self.bot.send_message(
            chat_id,
            "<b>Шаг 2 из 3 — Создайте группу</b>\n\n"
            "1. Создайте <b>новую группу</b> в Telegram\n"
            "   (☰ → Создать группу → назовите как хотите)\n\n"
            "2. Добавьте <b>этого бота</b> в группу\n\n"
            "3. Откройте настройки группы и включите <b>«Темы»</b> (Topics)\n\n"
            "4. Сделайте бота <b>администратором</b>:\n"
            "   Настройки → Администраторы → этот бот\n"
            "   Включите право <b>«Управление темами»</b>\n\n"
            "Когда бот — админ с правом на темы, нажмите кнопку ниже.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Бот добавлен в группу", callback_data="bot_added"),
            ]]),
        )

    async def _send_step4(self, chat_id: int):
        """Step 4: Run /setup in group."""
        analytics.track(chat_id, "onboarding_step", {"step": 4})
        await self.bot.send_message(
            chat_id,
            "<b>Шаг 3 из 3 — Запустите бота</b>\n\n"
            "Перейдите в созданную группу и напишите:\n\n"
            "<code>/setup</code>\n\n"
            "Бот автоматически создаст 4 топика и начнёт пересылку сообщений из MAX.\n\n"
            "После этого всё готово! 🎉",
            parse_mode="HTML",
        )

    # ─── Callback handler ───────────────────────────────

    async def handle_callback(self, callback: CallbackQuery):
        data = callback.data
        tg_id = callback.from_user.id

        if data in ("qr_login", "ext_installed", "new_code"):
            # ext_installed/new_code are buttons left from the extension
            # onboarding in older messages
            await callback.answer()
            await self._run_qr_login(callback.message.chat.id, tg_id)

        elif data == "bot_added":
            await callback.answer()
            await self._send_step4(callback.message.chat.id)

        elif data and data.startswith("to:"):
            await self._handle_to_choice(callback)

    # ─── DM commands ────────────────────────────────────

    async def handle_status(self, message: Message):
        user = await self.db.get_user_by_telegram_id(message.from_user.id)
        if not user:
            await message.answer("Вы не подключены. Используйте /start для начала.")
            return

        group = await self.db.get_user_group(user["id"])
        status_text = {
            "pending": "⏳ MAX подключён, ждёт настройки группы",
            "active": "✅ Активен",
            "paused": "⏸ Приостановлен",
            "error": "❌ Ошибка",
            "expired": "❌ MAX отозвал вход — подключите заново через /login",
        }.get(user["status"], user["status"])

        text = f"Статус: {status_text}\n"
        if group:
            text += "Группа: настроена\n"
        else:
            text += "Группа: не настроена (добавьте бота в форум-группу и напишите /setup)\n"

        await message.answer(text)

    async def handle_stop(self, message: Message):
        user = await self.db.get_user_by_telegram_id(message.from_user.id)
        if not user:
            await message.answer("Вы не подключены.")
            return

        await self.db.update_user_status(user["id"], "paused")
        if self.session_manager:
            await self.session_manager.stop_session(user["id"])
        await message.answer("⏸ MAX Bridge приостановлен. Используйте /start для повторного подключения.")

    async def notify_auth_failed(self, user_id: int):
        """Tell the user the bridge stopped because MAX revoked the token."""
        user = await self.db.get_user(user_id)
        if not user:
            return
        try:
            await self.bot.send_message(
                user["telegram_id"],
                "⚠️ MAX отозвал вход моста — пересылка остановлена.\n\n"
                "Нажмите /login — бот пришлёт QR-код, отсканируйте его "
                "телефоном с MAX. Группа и топики останутся прежними.",
            )
        except Exception as e:
            log.error(f"Failed to notify user {user_id} about revoked token: {e}")

    async def handle_help(self, message: Message):
        await message.answer(
            "<b>MAX Bridge</b> — пересылка сообщений из MAX в Telegram.\n\n"
            "/start — подключить MAX аккаунт\n"
            "/login — заново войти в MAX по QR-коду\n"
            "/status — проверить статус\n"
            "/stop — приостановить\n"
            "/setup — создать топики (в группе)\n"
            "/to — написать в MAX первым (в группе): <code>/to Имя текст</code>\n"
            "/support — написать в поддержку\n"
            "/help — эта справка",
            parse_mode="HTML",
        )

    # ─── Support ───────────────────────────────────────

    async def handle_support(self, message: Message):
        """User sends /support <message> — forward to admin."""
        if not self.admin_tg_id:
            await message.answer("Поддержка на этом сервере не настроена.")
            return
        text = (message.text or "").partition(" ")[2].strip()
        if not text:
            await message.answer(
                "Напишите сообщение после команды:\n"
                "<code>/support ваш вопрос</code>",
                parse_mode="HTML",
            )
            return

        tg_id = message.from_user.id
        username = message.from_user.username or ""
        full_name = message.from_user.full_name or str(tg_id)

        # User-supplied values are escaped: an unescaped "<" makes Telegram
        # reject the whole message with "can't parse entities".
        header = f"🆘 <b>Обращение в поддержку</b>\n\nОт: {html.escape(full_name)}"
        if username:
            header += f" (@{html.escape(username)})"
        header += f"\nID: <code>{tg_id}</code>\n\n{html.escape(text)}"

        try:
            sent = await self.bot.send_message(
                self.admin_tg_id,
                header,
                parse_mode="HTML",
            )
            await self.db.save_support_ticket(tg_id, sent.message_id, text)
            analytics.track(tg_id, "support_sent", {})
            await message.answer("✅ Сообщение отправлено в поддержку. Ответ придёт сюда.")
            log.info(f"Support ticket from TG:{tg_id} forwarded to admin")
        except Exception as e:
            log.error(f"Failed to forward support message: {e}")
            await message.answer("❌ Не удалось отправить сообщение. Попробуйте позже.")

    async def handle_admin_reply(self, message: Message):
        """Admin replies to a forwarded support message — send reply back to user."""
        if message.from_user.id != self.admin_tg_id:
            return
        if not message.reply_to_message:
            return

        replied_msg_id = message.reply_to_message.message_id
        ticket = await self.db.get_support_ticket(replied_msg_id)
        if not ticket:
            return

        reply_text = message.text or ""
        if not reply_text:
            return

        try:
            await self.bot.send_message(
                ticket["user_telegram_id"],
                f"💬 <b>Ответ от поддержки:</b>\n\n{html.escape(reply_text)}",
                parse_mode="HTML",
            )
            log.info(f"Admin reply sent to TG:{ticket['user_telegram_id']}")
        except Exception as e:
            log.error(f"Failed to send admin reply to user {ticket['user_telegram_id']}: {e}")
            await message.answer(f"❌ Не удалось доставить ответ пользователю: {e}")

    # ─── Group: topic creation ──────────────────────────

    async def _create_topics_for_user(self, chat_id: int, user: dict) -> bool:
        """Create 4 forum topics and save group config. Returns True on success."""
        topic_dm = await self.bot.create_forum_topic(chat_id, "Личные")
        topic_groups = await self.bot.create_forum_topic(chat_id, "Группы")
        topic_channels = await self.bot.create_forum_topic(chat_id, "Каналы")
        topic_comments = await self.bot.create_forum_topic(chat_id, "Комментарии")

        await self.db.save_user_group(
            user_id=user["id"],
            chat_id=chat_id,
            topic_dm=topic_dm.message_thread_id,
            topic_groups=topic_groups.message_thread_id,
            topic_channels=topic_channels.message_thread_id,
            topic_comments=topic_comments.message_thread_id,
        )

        await self.db.update_user_status(user["id"], "active")
        if self.session_manager:
            await self.session_manager.start_session(user["id"])

        await self.bot.send_message(
            chat_id,
            "✅ MAX Bridge настроен!\n\n"
            "Сообщения из MAX будут поступать в топики:\n"
            "• <b>Личные</b> — DM\n"
            "• <b>Группы</b> — групповые чаты\n"
            "• <b>Каналы</b> — посты каналов\n"
            "• <b>Комментарии</b> — комментарии к постам\n\n"
            "Чтобы ответить — сделайте Reply на сообщение в любом топике.\n"
            "Чтобы написать первым — <code>/to Имя текст</code> или <code>/to +79991234567 текст</code>.",
            parse_mode="HTML",
            message_thread_id=topic_dm.message_thread_id,
        )
        log.info(f"Created 4 topics in group {chat_id} for user {user['id']}")
        return True

    async def _setup_group(self, chat_id: int, user: dict) -> str:
        """Create topics for `user` in `chat_id`, serialized by a lock.

        The lock keeps /setup and the "bot added to group" event from both
        passing the "no group yet" check and creating two sets of topics.

        Returns "created", "exists" (user already has a group) or "taken"
        (the chat is already bound to a different user).
        """
        async with self._setup_lock:
            existing_group = await self.db.get_user_group(user["id"])
            if existing_group:
                return "exists"

            chat_owner = await self.db.get_user_by_tg_chat(chat_id)
            if chat_owner and chat_owner["id"] != user["id"]:
                return "taken"

            await self._create_topics_for_user(chat_id, user)
            return "created"

    async def handle_setup(self, message: Message):
        """Manual /setup command in a group — create topics."""
        chat_id = message.chat.id
        user_tg_id = message.from_user.id

        user = await self.db.get_user_by_telegram_id(user_tg_id)
        if not user:
            await message.reply(
                "Сначала привяжите MAX аккаунт: напишите /start этому боту в личных сообщениях."
            )
            return

        try:
            result = await self._setup_group(chat_id, user)
        except Exception as e:
            log.error(f"Failed to create topics via /setup in {chat_id}: {e}")
            await message.reply(
                "❌ Не удалось создать топики. Проверьте:\n\n"
                "1. В группе включены <b>Темы</b> (Topics)\n"
                "2. Бот — администратор с правом <b>«Управление темами»</b>",
                parse_mode="HTML",
            )
            return

        if result == "exists":
            await message.reply("✅ Топики уже созданы. Сообщения из MAX поступают.")
        elif result == "taken":
            await message.reply(
                "❌ Эта группа уже подключена к другому аккаунту MAX Bridge. "
                "Создайте отдельную группу для своего аккаунта."
            )
        else:
            analytics.track(user_tg_id, "setup_completed", {"chat_id": chat_id})

    async def handle_bot_added(self, event: ChatMemberUpdated):
        """Bot added to a group — auto-create 4 forum topics."""
        chat_id = event.chat.id
        user_tg_id = event.from_user.id

        user = await self.db.get_user_by_telegram_id(user_tg_id)
        if not user:
            log.warning(f"Bot added to group by unknown user TG:{user_tg_id}")
            return

        log.info(f"Bot added to group {chat_id} by user {user_tg_id}")

        try:
            result = await self._setup_group(chat_id, user)
            if result == "exists":
                log.info(f"User {user['id']} already has a group, skipping topic creation")
                return
            if result == "taken":
                log.warning(f"Chat {chat_id} already bound to another user, skipping")
                return
        except Exception as e:
            log.error(f"Failed to create topics in {chat_id}: {e}")
            try:
                await self.bot.send_message(
                    chat_id,
                    "Не удалось создать топики автоматически.\n"
                    "Убедитесь, что у бота есть право «Управление темами», "
                    "затем напишите /setup в этой группе.",
                )
            except Exception:
                log.error(f"Could not send error message to group {chat_id}")

    # ─── Group: reply routing ───────────────────────────

    async def handle_group_reply(self, message: Message):
        """Handle reply messages in forum groups — route back to MAX."""
        if not message.reply_to_message:
            log.debug(f"Group msg ignored: not a reply (chat={message.chat.id})")
            return
        if not message.message_thread_id:
            log.debug(f"Group msg ignored: no thread_id (chat={message.chat.id})")
            return

        chat_id = message.chat.id
        reply_to_id = message.reply_to_message.message_id
        log.info(f"Group reply: chat={chat_id} reply_to={reply_to_id} thread={message.message_thread_id}")

        user = await self.db.get_user_by_tg_chat(chat_id)
        if not user:
            log.warning(f"Group reply ignored: no user for chat {chat_id}")
            return

        mapping = await self.db.get_mapping_by_tg_message(reply_to_id, chat_id)
        if not mapping:
            log.warning(f"Group reply ignored: no mapping for tg_msg={reply_to_id} chat={chat_id}")
            return

        text = message.text or ""
        if not text:
            log.debug(f"Group reply ignored: empty text (reply_to={reply_to_id})")
            return

        if self.session_manager:
            ok = await self.session_manager.send_message(
                user["id"], mapping["max_chat_id"], text,
            )
            log.info(f"Reply routed: TG:{reply_to_id} → MAX:{mapping['max_chat_id']} ok={ok}")
            if not ok:
                # Without this the reply just disappears with no sign to the user.
                try:
                    await message.reply(
                        "❌ Не удалось отправить ответ в MAX — соединение с MAX потеряно. "
                        "Попробуйте ещё раз через минуту."
                    )
                except Exception as e:
                    log.error(f"Could not notify about failed reply in {chat_id}: {e}")

    # ─── Group: writing first ───────────────────────────

    async def handle_to(self, message: Message):
        """/to <who> [text] — start or continue a MAX conversation from Telegram."""
        user = await self.db.get_user_by_tg_chat(message.chat.id)
        if not user:
            return
        parts = (message.text or "").split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        if not args.strip():
            await message.reply(
                "Кому написать? Примеры:\n"
                "/to Мария привет\n"
                "/to +79991234567 привет\n"
                "Без текста бот пришлёт сообщение-адресата — ответьте на него Reply."
            )
            return

        client = self.session_manager.get_client(user["id"]) if self.session_manager else None
        if not client or not client.my_id:
            await message.reply("❌ MAX сейчас не подключён — нажмите /status в личке с ботом.")
            return

        first, _, rest = args.strip().partition(" ")
        phone = parse_phone(first)
        if phone:
            try:
                found = await client.find_by_phone(phone)
            except Exception as e:
                log.error(f"Phone lookup failed: {e}")
                await message.reply("❌ MAX не ответил на поиск по номеру, попробуйте ещё раз.")
                return
            if not found:
                await message.reply(f"Номер {phone} в MAX не найден.")
                return
            user_id, name = found
            target = Target(client.dialog_chat_id(user_id), name, "DIALOG", user_id)
            await self._open_dialog(user["id"], client, target, rest.strip(), message)
            return

        people = {uid: name for uid, name in client.contacts.items()
                  if uid > 0 and uid != client.my_id and uid not in client.chat_types}
        chats = {c["max_chat_id"]: (c["chat_type"], c["title"])
                 for c in await self.db.get_max_chats(user["id"])
                 if c["chat_type"] != "DIALOG"}
        for chat_id, title in client.chat_titles.items():
            chat_type = client.chat_types.get(chat_id, "CHAT")
            if chat_type != "DIALOG":
                chats[chat_id] = (chat_type, title)

        query, matches, text = split_command(
            args, lambda q: find_targets(q, people, chats, client.my_id),
        )
        if not matches:
            await message.reply(f"Не нашёл «{query}» среди контактов и чатов MAX.")
            return
        if len(matches) == 1:
            await self._open_dialog(user["id"], client, matches[0], text, message)
            return

        matches = matches[:MAX_CHOICES]
        key = uuid.uuid4().hex[:10]
        self._to_choices[key] = (user["id"], matches, text)
        while len(self._to_choices) > 50:
            self._to_choices.pop(next(iter(self._to_choices)))
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=("👥 " if t.chat_type != "DIALOG" else "") + t.label[:60],
                callback_data=f"to:{key}:{i}",
            )]
            for i, t in enumerate(matches)
        ])
        await message.reply(f"Кому именно? Нашлось несколько «{query}»:", reply_markup=keyboard)

    async def _handle_to_choice(self, callback: CallbackQuery):
        _, key, index = callback.data.split(":")
        pending = self._to_choices.pop(key, None)
        if not pending:
            await callback.answer("Выбор устарел — повторите /to", show_alert=True)
            return
        user_id, matches, text = pending
        target = matches[int(index)]
        await callback.answer()
        try:
            await callback.message.edit_text(f"Выбрано: {target.label}")
        except Exception as e:
            log.debug(f"Could not edit /to choice message: {e}")
        client = self.session_manager.get_client(user_id) if self.session_manager else None
        if not client:
            await callback.message.answer("❌ MAX сейчас не подключён.")
            return
        await self._open_dialog(user_id, client, target, text, callback.message)

    async def _open_dialog(self, user_id: int, client, target: Target, text: str,
                           origin: Message):
        """Send `text` to `target` (if any) and post the message to reply to.

        The posted message is mapped to the MAX chat like a forwarded one, so
        replying to it continues the conversation.
        """
        group = await self.db.get_user_group(user_id)
        if not group:
            await origin.reply("❌ Группа моста не настроена — /setup.")
            return
        if text:
            try:
                await client.send_text(target.chat_id, text)
            except Exception as e:
                log.error(f"/to send to MAX:{target.chat_id} failed: {e}")
                await origin.reply(f"❌ Не отправилось в MAX ({target.label}): {e}")
                return
            note = f"→ {target.label}:\n{text}"
        else:
            note = f"→ {target.label}\nОтветьте на это сообщение (Reply) — текст уйдёт в MAX."

        topic_id = group.get(CHAT_TYPE_TO_TOPIC.get(target.chat_type, "topic_dm")) or group["topic_dm"]
        sent = await self.bot.send_message(
            group["chat_id"], note[:4096], message_thread_id=topic_id,
        )
        await self.db.save_mapping(
            user_id=user_id,
            tg_message_id=sent.message_id,
            tg_chat_id=group["chat_id"],
            max_chat_id=target.chat_id,
            max_sender_id=target.user_id,
            sender_name=target.label if target.user_id else "",
            chat_type=target.chat_type.lower(),
        )
        log.info(f"/to: MAX:{target.chat_id}({target.chat_type}) sent={bool(text)}")

    # ─── Lifecycle ──────────────────────────────────────

    async def start(self):
        """Start polling."""
        await self.dp.start_polling(self.bot)

    async def stop(self):
        """Stop polling."""
        await self.dp.stop_polling()
        await self.bot.session.close()
