from __future__ import annotations

# Polymarket US fee schedule (docs.polymarket.us/fees.md), effective 2026-07-01:
# fee_per_contract = theta * price * (1 - price). Verified against the documented
# example: taker theta=0.06 -> $0.015/contract at p=$0.50 -> "$1.50/100 contracts".
TAKER_THETA = 0.06
MAKER_THETA = -0.0125  # negative = rebate


def taker_fee(price: float) -> float:
    return TAKER_THETA * price * (1 - price)


def maker_fee(price: float) -> float:
    return MAKER_THETA * price * (1 - price)
