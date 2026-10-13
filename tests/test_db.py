"""Tests for multi-user database."""
import os
import tempfile
import pytest

from max_bridge.db import Database


@pytest.fixture
async def db():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        path = f.name
    d = Database(path)
    await d.init()
    yield d
    await d.close()
    os.unlink(path)


class TestDatabaseInit:
    @pytest.mark.asyncio
    async def test_creates_all_tables(self, db):
        tables = await db.list_tables()
        assert "users" in tables
        assert "user_groups" in tables
        assert "message_mapping" in tables
        assert "max_chats" in tables
        assert "pairing_codes" in tables


class TestUsers:
    @pytest.mark.asyncio
    async def test_create_user(self, db):
        user_id = await db.create_user(
            telegram_id=111111111,
            max_token_encrypted="enc_token",
            max_device_id="dev123",
        )
        assert user_id > 0

    @pytest.mark.asyncio
    async def test_get_user_by_telegram_id(self, db):
        await db.create_user(
            telegram_id=111111111,
            max_token_encrypted="enc_token",
            max_device_id="dev123",
        )
        user = await db.get_user_by_telegram_id(111111111)
        assert user is not None
        assert user["telegram_id"] == 111111111
        assert user["status"] == "pending"

    @pytest.mark.asyncio
    async def test_get_user_nonexistent(self, db):
        user = await db.get_user_by_telegram_id(999)
        assert user is None

    @pytest.mark.asyncio
    async def test_update_user_status(self, db):
        uid = await db.create_user(
            telegram_id=100, max_token_encrypted="t", max_device_id="d"
        )
        await db.update_user_status(uid, "active")
        user = await db.get_user(uid)
        assert user["status"] == "active"

    @pytest.mark.asyncio
    async def test_update_user_max_user_id(self, db):
        uid = await db.create_user(
            telegram_id=100, max_token_encrypted="t", max_device_id="d"
        )
        await db.update_user_max_id(uid, 200000001)
        user = await db.get_user(uid)
        assert user["max_user_id"] == 200000001

    @pytest.mark.asyncio
    async def test_get_active_users(self, db):
        uid1 = await db.create_user(telegram_id=1, max_token_encrypted="t1", max_device_id="d1")
        uid2 = await db.create_user(telegram_id=2, max_token_encrypted="t2", max_device_id="d2")
        await db.update_user_status(uid1, "active")
        # uid2 stays "pending"
        users = await db.get_active_users()
        assert len(users) == 1
        assert users[0]["telegram_id"] == 1

    @pytest.mark.asyncio
    async def test_duplicate_telegram_id_raises(self, db):
        await db.create_user(telegram_id=100, max_token_encrypted="t", max_device_id="d")
        with pytest.raises(Exception):
            await db.create_user(telegram_id=100, max_token_encrypted="t2", max_device_id="d2")


class TestUserGroups:
    @pytest.mark.asyncio
    async def test_save_and_get_user_group(self, db):
        uid = await db.create_user(telegram_id=100, max_token_encrypted="t", max_device_id="d")
        await db.save_user_group(
            user_id=uid, chat_id=-100123456,
            topic_dm=2, topic_groups=3, topic_channels=4, topic_comments=5,
        )
        group = await db.get_user_group(uid)
        assert group["chat_id"] == -100123456
        assert group["topic_dm"] == 2
        assert group["topic_groups"] == 3

    @pytest.mark.asyncio
    async def test_get_user_by_tg_chat(self, db):
        uid = await db.create_user(telegram_id=100, max_token_encrypted="t", max_device_id="d")
        await db.save_user_group(user_id=uid, chat_id=-100123456, topic_dm=2, topic_groups=3, topic_channels=4, topic_comments=5)
        user = await db.get_user_by_tg_chat(-100123456)
        assert user is not None
        assert user["telegram_id"] == 100


class TestPairingCodes:
    @pytest.mark.asyncio
    async def test_create_and_get_code(self, db):
        await db.create_pairing_code("123456", telegram_id=111111111)
        result = await db.get_pairing_code("123456")
        assert result is not None
        assert result["telegram_id"] == 111111111

    @pytest.mark.asyncio
    async def test_delete_code(self, db):
        await db.create_pairing_code("123456", telegram_id=111111111)
        await db.delete_pairing_code("123456")
        result = await db.get_pairing_code("123456")
        assert result is None


class TestMessageMapping:
    @pytest.mark.asyncio
    async def test_save_and_get_mapping(self, db):
        uid = await db.create_user(telegram_id=100, max_token_encrypted="t", max_device_id="d")
        await db.save_mapping(
            user_id=uid, tg_message_id=500, tg_chat_id=-100123456,
            max_chat_id=200, max_sender_id=300, sender_name="Ivan",
            chat_type="dm",
        )
        mapping = await db.get_mapping_by_tg_message(500, -100123456)
        assert mapping is not None
        assert mapping["max_chat_id"] == 200
        assert mapping["user_id"] == uid

    @pytest.mark.asyncio
    async def test_get_sender_name(self, db):
        uid = await db.create_user(telegram_id=100, max_token_encrypted="t", max_device_id="d")
        await db.save_mapping(
            user_id=uid, tg_message_id=1, tg_chat_id=-100, max_chat_id=10,
            max_sender_id=42, sender_name="Anna", chat_type="dm",
        )
        name = await db.get_sender_name(uid, 42)
        assert name == "Anna"


class TestMaxChats:
    @pytest.mark.asyncio
    async def test_save_and_get_chat_type(self, db):
        uid = await db.create_user(telegram_id=100, max_token_encrypted="t", max_device_id="d")
        await db.save_max_chat(uid, max_chat_id=200, chat_type="DIALOG", title="Ivan")
        chat = await db.get_max_chat(uid, 200)
        assert chat["chat_type"] == "DIALOG"
        assert chat["title"] == "Ivan"

    @pytest.mark.asyncio
    async def test_upsert_chat(self, db):
        uid = await db.create_user(telegram_id=100, max_token_encrypted="t", max_device_id="d")
        await db.save_max_chat(uid, 200, "DIALOG", "Ivan")
        await db.save_max_chat(uid, 200, "DIALOG", "Ivan Petrov")
        chat = await db.get_max_chat(uid, 200)
        assert chat["title"] == "Ivan Petrov"
