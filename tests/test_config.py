"""Tests for configuration."""
import os
import pytest

from max_bridge.config import Config


class TestConfig:
    def test_defaults(self):
        cfg = Config()
        assert cfg.DB_PATH == "data/bridge.db"
        assert cfg.LOG_LEVEL == "INFO"
        assert cfg.API_HOST == "0.0.0.0"
        assert cfg.API_PORT == 8080

    def test_tg_bot_token_from_env(self, monkeypatch):
        monkeypatch.setenv("TG_BOT_TOKEN", "test_token")
        cfg = Config()
        assert cfg.TG_BOT_TOKEN == "test_token"

    def test_encryption_key_from_env(self, monkeypatch):
        monkeypatch.setenv("ENCRYPTION_KEY", "test_key")
        cfg = Config()
        assert cfg.ENCRYPTION_KEY == "test_key"
