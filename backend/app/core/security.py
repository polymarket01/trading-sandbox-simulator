from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets


def hash_session_token(token: str) -> str:
    """Store only a one-way digest for Paper HTTP sessions."""

    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 260_000


def make_ws_signature(api_key: str, api_secret: str, timestamp: int) -> str:
    payload = f"{api_key}:{timestamp}".encode("utf-8")
    return hmac.new(api_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_ws_signature(api_key: str, api_secret: str, timestamp: int, signature: str) -> bool:
    expected = make_ws_signature(api_key, api_secret, timestamp)
    return hmac.compare_digest(expected, signature)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "$".join(
        [
            PASSWORD_SCHEME,
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        scheme, iterations_text, salt_text, digest_text = password_hash.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_text))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def generate_api_key(prefix: str = "user") -> str:
    return f"{prefix}-{secrets.token_urlsafe(18)}"


def generate_api_secret() -> str:
    return secrets.token_urlsafe(32)
