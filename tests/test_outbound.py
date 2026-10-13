"""Tests for writing first (/to), name lookups and group chat routing."""
import asyncio
import os
import tempfile

import pytest
from unittest.mock import AsyncMock, MagicMock

from max_bridge.bot import BotService
from max_bridge.db import Database
from max_bridge.max_client import MaxClient, MaxRequestError, OpCode
from max_bridge.outbound import Target, find_targets, parse_phone, split_command
from max_bridge.router import MessageRouter

ME = 200000001
PEOPLE = {300000002: "Мария Сидорова", 1001: "Мария Петрова", 1002: "Алёна Смирнова"}
CHATS = {-70000000000001: ("CHAT", "Секция по плаванию")}


class TestFindTargets:
    def test_dialog_chat_id_is_xor_of_member_ids(self):
        [t] = find_targets("Сидорова", PEOPLE, CHATS, ME)
        assert t.chat_id == 436887811  # dialog id derived from both member ids
        assert t.chat_type == "DIALOG" and t.user_id == 300000002

    def test_every_word_must_match_and_case_and_yo_are_ignored(self):
        assert [t.label for t in find_targets("алена", PEOPLE, CHATS, ME)] == ["Алёна Смирнова"]
        assert [t.label for t in find_targets("мария пет", PEOPLE, CHATS, ME)] == ["Мария Петрова"]

    def test_several_matches_sorted_by_name(self):
        labels = [t.label for t in find_targets("Мария", PEOPLE, CHATS, ME)]
        assert labels == ["Мария Петрова", "Мария Сидорова"]

    def test_exact_match_first(self):
        people = {1: "Аня Иванова", 2: "Аня"}
        assert find_targets("аня", people, {}, ME)[0].label == "Аня"

    def test_group_chats_are_found_by_title(self):
        [t] = find_targets("плаванию", PEOPLE, CHATS, ME)
        assert t == Target(-70000000000001, "Секция по плаванию", "CHAT")


class TestSplitCommand:
    def lookup(self, q):
        return find_targets(q, PEOPLE, CHATS, ME)

    def test_longest_matching_prefix_is_the_recipient(self):
        query, matches, text = split_command("Мария Сидорова привет, как дела?", self.lookup)
        assert query == "Мария Сидорова"
        assert [m.label for m in matches] == ["Мария Сидорова"]
        assert text == "привет, как дела?"

    def test_first_line_is_recipient_when_multiline(self):
        query, matches, text = split_command("Алёна\nстрока 1\nстрока 2", self.lookup)
        assert query == "Алёна" and len(matches) == 1
        assert text == "строка 1\nстрока 2"

    def test_no_text(self):
        _, matches, text = split_command("Сидорова", self.lookup)
        assert len(matches) == 1 and text == ""

    def test_nobody_found(self):
        query, matches, _ = split_command("Вася привет", self.lookup)
        assert matches == [] and query == "Вася"


class TestParsePhone:
    @pytest.mark.parametrize("raw", ["+79991234567", "89991234567", "9991234567", "+7(999)123-45-67"])
    def test_normalized(self, raw):
        assert parse_phone(raw) == "+79991234567"

    @pytest.mark.parametrize("raw", ["Мария", "12345", "+19991234567"])
    def test_not_a_phone(self, raw):
        assert parse_phone(raw) is None


def connected_client():
    client = MaxClient(token="tok", device_id="dev")
    client.my_id = ME
    client._authenticated = True
    client._ws = MagicMock(closed=False)
    client._ws.send_json = AsyncMock()
    return client


async def answer(client, opcode, payload, cmd=1):
    """Wait for the client to send a request, then answer it like MAX does."""
    for _ in range(100):
        if client._ws.send_json.call_args:
            break
        await asyncio.sleep(0)
    frame = client._ws.send_json.call_args[0][0]
    assert frame["opcode"] == opcode
    await client._handle_frame({"ver": 11, "cmd": cmd, "seq": frame["seq"],
                                "opcode": opcode, "payload": payload})
    return frame


class TestClientRequests:
    @pytest.mark.asyncio
    async def test_contact_names_prefer_address_book_name(self):
        client = connected_client()
        task = asyncio.create_task(client.fetch_contact_names([400000003]))
        frame = await answer(client, OpCode.CONTACT_INFO, {"contacts": [{
            "id": 400000003,
            "names": [{"firstName": "Сергей", "lastName": "Соколов", "type": "CUSTOM"},
                      {"firstName": "Sergey", "lastName": "S.", "type": "ONEME"}],
        }]})
        assert frame["payload"] == {"contactIds": [400000003]}
        assert await task == {400000003: "Сергей Соколов"}
        assert client.contacts[400000003] == "Сергей Соколов"

    @pytest.mark.asyncio
    async def test_fetch_chats_records_type_and_title(self):
        client = connected_client()
        task = asyncio.create_task(client.fetch_chats([-70000000000002]))
        await answer(client, OpCode.CHAT_INFO, {"chats": [
            {"id": -70000000000002, "type": "CHAT", "title": "Мультичат"},
        ]})
        assert await task == {-70000000000002: {"type": "CHAT", "title": "Мультичат"}}
        assert client.chat_types[-70000000000002] == "CHAT"

    @pytest.mark.asyncio
    async def test_error_answer_raises(self):
        client = connected_client()
        task = asyncio.create_task(client.send_text(1, "hi"))
        await answer(client, OpCode.MESSAGE, {"error": "x", "localizedMessage": "Нельзя"}, cmd=3)
        with pytest.raises(MaxRequestError, match="Нельзя"):
            await task

    @pytest.mark.asyncio
    async def test_push_with_same_seq_does_not_resolve_request(self):
        client = connected_client()
        client.on_message = MagicMock()
        task = asyncio.create_task(client.fetch_chats([5]))
        for _ in range(100):
            if client._ws.send_json.call_args:
                break
            await asyncio.sleep(0)
        seq = client._ws.send_json.call_args[0][0]["seq"]
        await client._handle_frame({"ver": 11, "cmd": 0, "seq": seq, "opcode": OpCode.CHAT_INFO,
                                    "payload": {}})
        assert not task.done()
        task.cancel()

    @pytest.mark.asyncio
    async def test_request_needs_connection(self):
        client = MaxClient(token="tok", device_id="dev")
        with pytest.raises(ConnectionError):
            await client.request(OpCode.CONTACT_INFO, {})

    @pytest.mark.asyncio
    async def test_dropped_connection_fails_waiting_requests(self):
        client = connected_client()
        task = asyncio.create_task(client.fetch_chats([5]))
        await asyncio.sleep(0)
        client._fail_requests()
        with pytest.raises(ConnectionError):
            await task

    @pytest.mark.asyncio
    async def test_unknown_phone_returns_none(self):
        client = connected_client()
        task = asyncio.create_task(client.find_by_phone("+79990000000"))
        await answer(client, OpCode.CONTACT_INFO_BY_PHONE, {"error": "not.found"}, cmd=3)
        assert await task is None

    def test_control_attach_shows_its_message(self):
        client = MaxClient(token="tok", device_id="dev")
        msg = client._parse_message({"chatId": 1, "message": {"sender": 2, "text": "", "attaches": [
            {"_type": "CONTROL", "shortMessage": "Теперь в MAX!", "event": "system"},
        ]}})
        assert msg["text"] == "[Теперь в MAX!]"

    def test_control_event_without_message_is_translated(self):
        client = MaxClient(token="tok", device_id="dev")
        msg = client._parse_message({"chatId": 1, "message": {"sender": 2, "text": "", "attaches": [
            {"_type": "CONTROL", "event": "leave"},
        ]}})
        assert msg["text"] == "[участник вышел]"


GROUP = {"chat_id": -100, "topic_dm": 2, "topic_groups": 3, "topic_channels": 4, "topic_comments": 5}


class TestRouterLookups:
    @pytest.fixture
    def setup(self):
        db = AsyncMock()
        db.get_user_group = AsyncMock(return_value=GROUP)
        db.get_max_chat = AsyncMock(return_value=None)
        db.get_sender_name = AsyncMock(return_value=None)
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
        client = MagicMock(contacts={})
        client.fetch_chats = AsyncMock(return_value={})
        client.fetch_contact_names = AsyncMock(return_value={})
        sm = MagicMock()
        sm.get_client = MagicMock(return_value=client)
        return MessageRouter(db=db, bot=bot, session_manager=sm), db, bot, client

    @pytest.mark.asyncio
    async def test_chat_type_goes_to_groups_topic(self, setup):
        router, db, bot, client = setup
        db.get_max_chat = AsyncMock(return_value={"chat_type": "CHAT", "title": "Секция по плаванию"})
        client.contacts = {60000005: "Папа Пети"}
        await router.route_message(1, {"chat_id": -70000000000001, "sender_id": 60000005,
                                       "text": "Клубнику съели", "message_id": "m"})
        kwargs = bot.send_message.call_args[1]
        assert kwargs["message_thread_id"] == 3
        assert kwargs["text"] == "[Секция по плаванию]\nПапа Пети:\nКлубнику съели"

    @pytest.mark.asyncio
    async def test_unknown_chat_is_asked_from_max_and_saved(self, setup):
        router, db, bot, client = setup
        client.contacts = {5: "Елена"}
        client.fetch_chats = AsyncMock(return_value={-77: {"type": "CHAT", "title": "Мультичат"}})
        await router.route_message(1, {"chat_id": -77, "sender_id": 5, "text": "hi", "message_id": "m"})
        db.save_max_chat.assert_awaited_once_with(1, -77, "CHAT", "Мультичат")
        assert bot.send_message.call_args[1]["message_thread_id"] == 3

    @pytest.mark.asyncio
    async def test_unknown_negative_chat_falls_back_to_groups(self, setup):
        router, db, bot, client = setup
        client.contacts = {5: "Елена"}
        client.fetch_chats = AsyncMock(side_effect=ConnectionError("down"))
        await router.route_message(1, {"chat_id": -77, "sender_id": 5, "text": "hi", "message_id": "m"})
        assert bot.send_message.call_args[1]["message_thread_id"] == 3

    @pytest.mark.asyncio
    async def test_unknown_sender_name_is_asked_from_max(self, setup):
        router, db, bot, client = setup
        db.get_max_chat = AsyncMock(return_value={"chat_type": "DIALOG", "title": ""})
        client.fetch_contact_names = AsyncMock(return_value={50000004: "Ольга Кузнецова"})
        await router.route_message(1, {"chat_id": 9, "sender_id": 50000004, "text": "hi", "message_id": "m"})
        assert bot.send_message.call_args[1]["text"] == "Ольга Кузнецова:\nhi"
        assert db.save_mapping.call_args[1]["sender_name"] == "Ольга Кузнецова"

    @pytest.mark.asyncio
    async def test_failed_lookup_falls_back_to_id(self, setup):
        router, db, bot, client = setup
        db.get_max_chat = AsyncMock(return_value={"chat_type": "DIALOG", "title": ""})
        client.fetch_contact_names = AsyncMock(side_effect=ConnectionError("down"))
        await router.route_message(1, {"chat_id": 9, "sender_id": 42, "text": "hi", "message_id": "m"})
        assert bot.send_message.call_args[1]["text"] == "ID:42:\nhi"


@pytest.fixture
async def real_db():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        path = f.name
    d = Database(path)
    await d.init()
    yield d
    await d.close()
    os.unlink(path)


class TestDbNames:
    @pytest.mark.asyncio
    async def test_id_placeholders_are_not_reused_as_names(self, real_db):
        uid = await real_db.create_user(1, "enc", "dev")
        await real_db.save_mapping(uid, 10, -100, 9, 42, sender_name="ID:42")
        assert await real_db.get_sender_name(uid, 42) is None
        await real_db.save_mapping(uid, 11, -100, 9, 42, sender_name="Ольга")
        await real_db.save_mapping(uid, 12, -100, 9, 42, sender_name="ID:42")
        assert await real_db.get_sender_name(uid, 42) == "Ольга"


class TestToCommand:
    @pytest.fixture
    def svc(self):
        db = AsyncMock()
        db.get_user_by_tg_chat = AsyncMock(return_value={"id": 1})
        db.get_user_group = AsyncMock(return_value=GROUP)
        db.get_max_chats = AsyncMock(return_value=[
            {"max_chat_id": -70000000000001, "chat_type": "CHAT", "title": "Секция по плаванию"},
            {"max_chat_id": 436887811, "chat_type": "DIALOG", "title": ""},
        ])
        client = MagicMock()
        client.my_id = ME
        client.contacts = dict(PEOPLE)
        client.chat_types = {436887811: "DIALOG"}
        client.chat_titles = {}
        client.dialog_chat_id = lambda uid: ME ^ uid
        client.send_text = AsyncMock(return_value={})
        client.find_by_phone = AsyncMock(return_value=(400000003, "Сергей Соколов"))
        sm = MagicMock()
        sm.get_client = MagicMock(return_value=client)
        s = BotService(bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz-1234567890",
                       db=db, crypto=MagicMock(), session_manager=sm)
        s.bot = AsyncMock()
        s.bot.send_message = AsyncMock(return_value=MagicMock(message_id=77))
        return s, db, client

    def message(self, text):
        m = AsyncMock()
        m.text = text
        m.chat.id = -100
        return m

    @pytest.mark.asyncio
    async def test_single_match_sends_and_posts_reply_anchor(self, svc):
        s, db, client = svc
        await s.handle_to(self.message("/to Сидорова привет"))
        client.send_text.assert_awaited_once_with(436887811, "привет")
        args, kwargs = s.bot.send_message.call_args
        assert args == (-100, "→ Мария Сидорова:\nпривет")
        assert kwargs["message_thread_id"] == 2
        mapping = db.save_mapping.call_args[1]
        assert mapping["tg_message_id"] == 77 and mapping["max_chat_id"] == 436887811
        assert mapping["max_sender_id"] == 300000002

    @pytest.mark.asyncio
    async def test_no_text_only_posts_anchor(self, svc):
        s, db, client = svc
        await s.handle_to(self.message("/to Сидорова"))
        client.send_text.assert_not_awaited()
        assert "Ответьте на это сообщение" in s.bot.send_message.call_args[0][1]
        db.save_mapping.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_group_chat_goes_to_groups_topic(self, svc):
        s, db, client = svc
        await s.handle_to(self.message("/to плаванию всем привет"))
        client.send_text.assert_awaited_once_with(-70000000000001, "всем привет")
        assert s.bot.send_message.call_args[1]["message_thread_id"] == 3

    @pytest.mark.asyncio
    async def test_several_matches_ask_and_choice_sends(self, svc):
        s, db, client = svc
        msg = self.message("/to Мария привет")
        await s.handle_to(msg)
        client.send_text.assert_not_awaited()
        keyboard = msg.reply.call_args[1]["reply_markup"].inline_keyboard
        assert [row[0].text for row in keyboard] == ["Мария Петрова", "Мария Сидорова"]

        callback = AsyncMock()
        callback.data = keyboard[1][0].callback_data
        callback.message = self.message("")
        await s.handle_callback(callback)
        client.send_text.assert_awaited_once_with(436887811, "привет")
        callback.message.edit_text.assert_awaited_once_with("Выбрано: Мария Сидорова")

    @pytest.mark.asyncio
    async def test_stale_choice_is_reported(self, svc):
        s, _, client = svc
        callback = AsyncMock()
        callback.data = "to:deadbeef00:0"
        await s.handle_callback(callback)
        assert callback.answer.call_args[1]["show_alert"] is True
        client.send_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_phone_lookup(self, svc):
        s, _, client = svc
        await s.handle_to(self.message("/to 8 999 привет"))  # not a phone token → name search
        client.find_by_phone.assert_not_awaited()
        await s.handle_to(self.message("/to 89990001122 добрый день"))
        client.find_by_phone.assert_awaited_once_with("+79990001122")
        client.send_text.assert_awaited_once_with(ME ^ 400000003, "добрый день")

    @pytest.mark.asyncio
    async def test_nobody_found(self, svc):
        s, _, client = svc
        msg = self.message("/to Вася привет")
        await s.handle_to(msg)
        assert "Не нашёл «Вася»" in msg.reply.call_args[0][0]
        client.send_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_failure_is_reported_and_not_posted(self, svc):
        s, db, client = svc
        client.send_text = AsyncMock(side_effect=MaxRequestError({"localizedMessage": "Нельзя писать"}))
        msg = self.message("/to Сидорова привет")
        await s.handle_to(msg)
        assert "Нельзя писать" in msg.reply.call_args[0][0]
        s.bot.send_message.assert_not_awaited()
        db.save_mapping.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disconnected_max(self, svc):
        s, _, client = svc
        client.my_id = None
        msg = self.message("/to Сидорова привет")
        await s.handle_to(msg)
        assert "не подключён" in msg.reply.call_args[0][0]
