from __future__ import annotations

import datetime as dt


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
