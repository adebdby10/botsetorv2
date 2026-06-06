# utils/logger.py
# Log JSON: success.json, failed.json, pending.json, invalid_2fa.json

import json
from datetime import datetime
from pathlib import Path

SUCCESS_FILE = Path("success.json")
FAILED_FILE = Path("failed.json")
PENDING_FILE = Path("pending.json")
INVALID_2FA_FILE = Path("invalid_2fa.json")


def _init_file(p: Path):
    if not p.exists():
        with open(p, "w", encoding="utf-8") as f:
            json.dump([], f)


def init_logs():
    _init_file(SUCCESS_FILE)
    _init_file(FAILED_FILE)
    _init_file(PENDING_FILE)
    _init_file(INVALID_2FA_FILE)


def _append(p: Path, data: dict):
    try:
        arr = []
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                arr = json.load(f)
        arr.append(data)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(arr, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ Log error ({p.name}): {e}")


def _entry(phone: str, bot: str, status: str, extra: str = "") -> dict:
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "phone": phone,
        "bot": bot,
        "status": status,
        "extra": extra,
    }


def log_pending(phone: str, bot: str, extra: str = ""):
    print(f"📝 PENDING: {phone} @ {bot}")
    _append(PENDING_FILE, _entry(phone, bot, "pending", extra))


def log_success(phone: str, bot: str, extra: str = ""):
    print(f"✅ SUCCESS: {phone} @ {bot}")
    _append(SUCCESS_FILE, _entry(phone, bot, "success", extra))


def log_failed(phone: str, bot: str, extra: str = ""):
    print(f"❌ FAILED: {phone} @ {bot} | {extra}")
    _append(FAILED_FILE, _entry(phone, bot, "failed", extra))


def log_invalid_2fa(phone: str, bot: str, extra: str = ""):
    print(f"🔒 INVALID 2FA: {phone} @ {bot} | {extra}")
    _append(INVALID_2FA_FILE, _entry(phone, bot, "invalid_2fa", extra))
