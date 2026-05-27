from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet


class TokenCipher:
    def __init__(self, secret: str) -> None:
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("utf-8")).decode("utf-8")


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 6)}{value[-3:]}"


def build_auth_token(password: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), password.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_auth_token(token: str | None, password: str, secret: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token, build_auth_token(password, secret))
