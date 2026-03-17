from __future__ import annotations

import hashlib
import hmac


def make_ws_signature(api_key: str, api_secret: str, timestamp: int) -> str:
    payload = f"{api_key}:{timestamp}".encode("utf-8")
    return hmac.new(api_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_ws_signature(api_key: str, api_secret: str, timestamp: int, signature: str) -> bool:
    expected = make_ws_signature(api_key, api_secret, timestamp)
    return hmac.compare_digest(expected, signature)
