from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol


def hash_params(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12]


@dataclass
class Opportunity:
    strategy_id: str
    params_hash: str
    detected_at: str
    market_ref: str
    direction: str
    signal_value: float
    entry_price: float
    top_levels_json: str
    extra_json: str = "{}"


class Strategy(Protocol):
    strategy_id: str

    def params_hash(self) -> str: ...

    async def scan(self) -> list[Opportunity]: ...
