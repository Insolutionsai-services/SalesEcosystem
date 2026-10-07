"""Pure helpers with no database or business knowledge. Reused across every layer."""
import csv
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TRUTHY = {"1", "y", "yes", "true", "granted"}


def now() -> datetime:
    """Naive UTC; the whole app stores naive UTC (SQLite has no timezone support)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(at: datetime, tz: str) -> datetime:
    return at.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))


def to_utc_naive(at: datetime) -> datetime:
    """Timezone-aware -> the app's naive-UTC convention."""
    return at.astimezone(timezone.utc).replace(tzinfo=None)


def hour_in_window(hour: int, start: int, end: int) -> bool:
    """True if hour is in [start, end), where the window may wrap midnight (e.g. 21 -> 9)."""
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def norm_phone(raw: str | None, country_code: str = "91") -> str | None:
    # ponytail: digits-only normalisation; swap for `phonenumbers` when tenants go multi-country
    digits = re.sub(r"\D", "", raw or "")
    return (country_code + digits if len(digits) == 10 else digits) or None


def norm_email(raw: str | None) -> str | None:
    return (raw or "").strip().lower() or None


def is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def parse_date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def parse_csv(text: str) -> list[dict]:
    """CSV -> rows with lower-cased, trimmed headers and trimmed values."""
    return [{k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            for row in csv.DictReader(io.StringIO(text))]


def first_name(name: str | None, fallback: str = "there") -> str:
    return (name or "").split()[0] if (name or "").strip() else fallback


def to_json(obj) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False, sort_keys=True)


def load_env_file(path: str | Path) -> None:
    """KEY=VALUE lines into os.environ; variables already set win. Enough for local dev, no dependency."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if value := value.strip().strip("\"'"):  # blank lines like `KEY=` must not mask other credentials
                os.environ.setdefault(key.strip(), value)
