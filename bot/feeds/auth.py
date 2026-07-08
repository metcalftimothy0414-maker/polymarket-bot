from __future__ import annotations

from polymarket_us.auth import create_auth_headers


class PolymarketAuth:
    """Thin wrapper around the official polymarket-us SDK's request signer
    (github.com/Polymarket/polymarket-us-python) instead of hand-rolled Ed25519 signing."""

    def __init__(self, key_id: str, private_key_b64: str) -> None:
        self.key_id = key_id
        self._private_key_b64 = private_key_b64

    def headers(self, method: str, path: str) -> dict[str, str]:
        return create_auth_headers(self.key_id, self._private_key_b64, method.upper(), path)
