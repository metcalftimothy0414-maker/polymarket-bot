from __future__ import annotations

import base64
import unittest

from nacl.signing import SigningKey, VerifyKey

from bot.feeds.auth import PolymarketAuth


class TestPolymarketAuth(unittest.TestCase):
    def test_signature_verifies_against_the_matching_public_key(self):
        signing_key = SigningKey.generate()
        seed_b64 = base64.b64encode(bytes(signing_key)).decode()

        auth = PolymarketAuth("key-id-123", seed_b64)
        headers = auth.headers("get", "/v1/orders")

        message = f"{headers['X-PM-Timestamp']}GET/v1/orders".encode()
        VerifyKey(signing_key.verify_key.encode()).verify(
            message, base64.b64decode(headers["X-PM-Signature"])
        )  # raises nacl.exceptions.BadSignatureError on mismatch
        self.assertEqual(headers["X-PM-Access-Key"], "key-id-123")


if __name__ == "__main__":
    unittest.main()
