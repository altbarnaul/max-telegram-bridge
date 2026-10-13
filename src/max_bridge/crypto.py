"""Fernet encryption for MAX tokens."""
from cryptography.fernet import Fernet


class TokenCrypto:
    def __init__(self, key: str):
        self.key = key
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    @classmethod
    def generate(cls) -> "TokenCrypto":
        key = Fernet.generate_key().decode()
        return cls(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()
