"""Configuration from environment variables."""
import os


class Config:
    def __init__(self):
        # Telegram bot
        self.TG_BOT_TOKEN: str = os.environ.get("TG_BOT_TOKEN", "")

        # Encryption
        self.ENCRYPTION_KEY: str = os.environ.get("ENCRYPTION_KEY", "")

        # HTTP API
        self.API_HOST: str = os.environ.get("API_HOST", "0.0.0.0")
        self.API_PORT: int = int(os.environ.get("API_PORT", "8080"))

        # Database
        self.DB_PATH: str = os.environ.get("DB_PATH", "data/bridge.db")
        self.LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

        # Admin for /support (0 = support disabled)
        self.ADMIN_TG_ID: int = int(os.environ.get("ADMIN_TG_ID", "0"))

        # Analytics
        self.POSTHOG_API_KEY: str = os.environ.get("POSTHOG_API_KEY", "")

        # WebSocket (constants, not per-user)
        self.WS_URL: str = "wss://ws-api.oneme.ru/websocket"
        self.WS_ORIGIN: str = "https://web.max.ru"
        self.HEARTBEAT_INTERVAL: int = 30
        self.RECONNECT_BASE_DELAY: int = 5
        self.RECONNECT_MAX_DELAY: int = 300
