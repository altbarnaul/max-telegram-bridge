"""Tests for Fernet token encryption."""
import pytest

from max_bridge.crypto import TokenCrypto


@pytest.fixture
def crypto():
    return TokenCrypto.generate()


class TestTokenCrypto:
    def test_encrypt_decrypt_roundtrip(self, crypto):
        token = "some_secret_max_token_12345"
        encrypted = crypto.encrypt(token)
        assert encrypted != token
        decrypted = crypto.decrypt(encrypted)
        assert decrypted == token

    def test_encrypted_is_different_each_time(self, crypto):
        token = "test_token"
        enc1 = crypto.encrypt(token)
        enc2 = crypto.encrypt(token)
        # Fernet uses random IV, so encryptions differ
        assert enc1 != enc2

    def test_from_key(self):
        c1 = TokenCrypto.generate()
        key = c1.key
        c2 = TokenCrypto(key)
        token = "test"
        encrypted = c1.encrypt(token)
        assert c2.decrypt(encrypted) == token

    def test_invalid_key_raises(self):
        with pytest.raises(Exception):
            TokenCrypto("not-a-valid-key")

    def test_decrypt_wrong_key_raises(self):
        c1 = TokenCrypto.generate()
        c2 = TokenCrypto.generate()
        encrypted = c1.encrypt("secret")
        with pytest.raises(Exception):
            c2.decrypt(encrypted)
