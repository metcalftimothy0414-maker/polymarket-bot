from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class PolymarketAuth:
    """Ed25519 request signer per docs.polymarket.us/api-reference/authentication.

    Signed message is f"{timestamp_ms}{method}{path}" (no query string, no body).
    """

    def __init__(self, key_id: str, private_key_b64: str) -> None:
        self.key_id = key_id
        seed = base64.b64decode(private_key_b64)[:32]
        self._key = Ed25519PrivateKey.from_private_bytes(seed)

    def headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path}".encode()
        signature = base64.b64encode(self._key.sign(message)).decode()
        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": signature,
        }
