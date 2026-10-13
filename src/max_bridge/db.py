"""Multi-user SQLite database."""
import aiosqlite


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()

    async def _create_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                max_token_encrypted TEXT NOT NULL,
                max_device_id TEXT NOT NULL,
                max_user_id INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                chat_id INTEGER NOT NULL,
                topic_dm INTEGER,
                topic_groups INTEGER,
                topic_channels INTEGER,
                topic_comments INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS message_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                tg_message_id INTEGER NOT NULL,
                tg_chat_id INTEGER NOT NULL,
                max_chat_id INTEGER NOT NULL,
                max_sender_id INTEGER NOT NULL,
                sender_name TEXT DEFAULT '',
                max_message_id TEXT DEFAULT '',
                chat_type TEXT DEFAULT 'dm',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS max_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                max_chat_id INTEGER NOT NULL,
                chat_type TEXT NOT NULL,
                title TEXT DEFAULT '',
                UNIQUE(user_id, max_chat_id)
            );

            CREATE TABLE IF NOT EXISTS pairing_codes (
                code TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_telegram_id INTEGER NOT NULL,
                admin_message_id INTEGER NOT NULL,
                user_message_text TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_mm_tg_msg
                ON message_mapping(tg_message_id, tg_chat_id);
            CREATE INDEX IF NOT EXISTS idx_mm_sender
                ON message_mapping(user_id, max_sender_id);
            CREATE INDEX IF NOT EXISTS idx_ug_chat
                ON user_groups(chat_id);
            CREATE INDEX IF NOT EXISTS idx_st_admin_msg
                ON support_tickets(admin_message_id);
        """)
        await self._db.commit()

    async def list_tables(self) -> list[str]:
        cursor = await self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

    # --- Users ---

    async def create_user(self, telegram_id: int, max_token_encrypted: str, max_device_id: str) -> int:
        cursor = await self._db.execute(
            "INSERT INTO users (telegram_id, max_token_encrypted, max_device_id) VALUES (?, ?, ?)",
            (telegram_id, max_token_encrypted, max_device_id),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_user(self, user_id: int) -> dict | None:
        cursor = await self._db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_user_by_telegram_id(self, telegram_id: int) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_user_status(self, user_id: int, status: str):
        await self._db.execute(
            "UPDATE users SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, user_id),
        )
        await self._db.commit()

    async def update_user_max_id(self, user_id: int, max_user_id: int):
        await self._db.execute(
            "UPDATE users SET max_user_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (max_user_id, user_id),
        )
        await self._db.commit()

    async def update_user_token(self, user_id: int, max_token_encrypted: str, max_device_id: str):
        await self._db.execute(
            "UPDATE users SET max_token_encrypted = ?, max_device_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (max_token_encrypted, max_device_id, user_id),
        )
        await self._db.commit()

    async def get_active_users(self) -> list[dict]:
        cursor = await self._db.execute("SELECT * FROM users WHERE status = 'active'")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- User Groups ---

    async def save_user_group(self, user_id: int, chat_id: int,
                               topic_dm: int, topic_groups: int,
                               topic_channels: int, topic_comments: int):
        await self._db.execute(
            """INSERT OR REPLACE INTO user_groups
               (user_id, chat_id, topic_dm, topic_groups, topic_channels, topic_comments)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, chat_id, topic_dm, topic_groups, topic_channels, topic_comments),
        )
        await self._db.commit()

    async def get_user_group(self, user_id: int) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM user_groups WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_user_by_tg_chat(self, chat_id: int) -> dict | None:
        cursor = await self._db.execute(
            """SELECT u.* FROM users u
               JOIN user_groups ug ON u.id = ug.user_id
               WHERE ug.chat_id = ?""",
            (chat_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # --- Pairing Codes ---

    async def create_pairing_code(self, code: str, telegram_id: int):
        await self._db.execute(
            "INSERT INTO pairing_codes (code, telegram_id) VALUES (?, ?)",
            (code, telegram_id),
        )
        await self._db.commit()

    async def get_pairing_code(self, code: str) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM pairing_codes WHERE code = ?", (code,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def delete_pairing_code(self, code: str):
        await self._db.execute("DELETE FROM pairing_codes WHERE code = ?", (code,))
        await self._db.commit()

    async def cleanup_expired_codes(self, ttl_seconds: int = 300):
        await self._db.execute(
            "DELETE FROM pairing_codes WHERE created_at < datetime('now', ?)",
            (f"-{ttl_seconds} seconds",),
        )
        await self._db.commit()

    # --- Message Mapping ---

    async def save_mapping(self, user_id: int, tg_message_id: int, tg_chat_id: int,
                           max_chat_id: int, max_sender_id: int,
                           sender_name: str = "", max_message_id: str = "",
                           chat_type: str = "dm"):
        await self._db.execute(
            """INSERT INTO message_mapping
               (user_id, tg_message_id, tg_chat_id, max_chat_id, max_sender_id,
                sender_name, max_message_id, chat_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, tg_message_id, tg_chat_id, max_chat_id, max_sender_id,
             sender_name, max_message_id, chat_type),
        )
        await self._db.commit()

    async def get_mapping_by_tg_message(self, tg_message_id: int, tg_chat_id: int) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM message_mapping WHERE tg_message_id = ? AND tg_chat_id = ?",
            (tg_message_id, tg_chat_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_sender_name(self, user_id: int, max_sender_id: int) -> str | None:
        cursor = await self._db.execute(
            """SELECT sender_name FROM message_mapping
               WHERE user_id = ? AND max_sender_id = ? AND sender_name != ''
                 AND sender_name NOT LIKE 'ID:%'
               ORDER BY id DESC LIMIT 1""",
            (user_id, max_sender_id),
        )
        row = await cursor.fetchone()
        return row["sender_name"] if row else None

    # --- MAX Chats ---

    async def save_max_chat(self, user_id: int, max_chat_id: int,
                            chat_type: str, title: str = ""):
        await self._db.execute(
            """INSERT INTO max_chats (user_id, max_chat_id, chat_type, title)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, max_chat_id)
               DO UPDATE SET chat_type = excluded.chat_type, title = excluded.title""",
            (user_id, max_chat_id, chat_type, title),
        )
        await self._db.commit()

    async def get_max_chats(self, user_id: int) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM max_chats WHERE user_id = ?", (user_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def get_max_chat(self, user_id: int, max_chat_id: int) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM max_chats WHERE user_id = ? AND max_chat_id = ?",
            (user_id, max_chat_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # --- Support Tickets ---

    async def save_support_ticket(self, user_telegram_id: int, admin_message_id: int,
                                   user_message_text: str = ""):
        await self._db.execute(
            """INSERT INTO support_tickets (user_telegram_id, admin_message_id, user_message_text)
               VALUES (?, ?, ?)""",
            (user_telegram_id, admin_message_id, user_message_text),
        )
        await self._db.commit()

    async def get_support_ticket(self, admin_message_id: int) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM support_tickets WHERE admin_message_id = ?",
            (admin_message_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def close(self):
        if self._db:
            await self._db.close()
