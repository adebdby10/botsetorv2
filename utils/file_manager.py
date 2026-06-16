import shutil
import re
import time
from pathlib import Path
from telethon import TelegramClient
from config import SESSIONS_DIR, TWO_FA_ON_DIR, OTHER_DEVICE_DIR, UNAUTH_DIR, REJECTED_DIR, RECOVERED_DIR, ALREADY_SOLD_DIR, CANCELLED_DIR

PHONE_RE = re.compile(r"(\+?\d{6,20})")


def list_sessions(dir_path: Path) -> list[Path]:
    return sorted([p for p in dir_path.rglob("*.session") if p.is_file()])


def parse_phone_from_filename(name: str) -> str | None:
    m = PHONE_RE.search(name)
    if not m:
        return None
    num = m.group(1)
    if not num.startswith("+"):
        num = f"+{num}"
    return num


def _move_preserving_user(session_path: Path, dest_base: Path) -> Path:
    try:
        relative = session_path.relative_to(SESSIONS_DIR)
        dest = dest_base / relative
    except ValueError:
        dest = dest_base / session_path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(session_path), str(dest))
    return dest


def move_to_2fa_on(session_path: Path) -> Path:
    dest = _move_preserving_user(session_path, TWO_FA_ON_DIR)
    print(f"🔒 Dipindah ke 2FA_ON: {session_path.name}")
    return dest


def move_to_other_device(session_path: Path) -> Path:
    dest = _move_preserving_user(session_path, OTHER_DEVICE_DIR)
    print(f"📱 Dipindah ke OTHER_DEVICE: {session_path.name}")
    return dest


def move_to_invalid_2fa(session_path: Path, retries: int = 3, delay: float = 1.0):
    for i in range(retries):
        try:
            move_to_2fa_on(session_path)
            return
        except Exception as e:
            if i < retries - 1:
                time.sleep(delay)
            else:
                print(f"⚠️ Gagal move ke 2FA_ON: {e}")


def move_to_rejected(session_path: Path, retries: int = 3, delay: float = 1.0) -> Path | None:
    for i in range(retries):
        try:
            dest = _move_preserving_user(session_path, REJECTED_DIR)
            print(f"🚫 Dipindah ke REJECTED: {session_path.name}")
            return dest
        except Exception as e:
            if i < retries - 1:
                time.sleep(delay)
            else:
                print(f"⚠️ Gagal move ke REJECTED: {e}")
    return None


def move_to_recovered(session_path: Path, retries: int = 3, delay: float = 1.0) -> Path | None:
    for i in range(retries):
        try:
            dest = _move_preserving_user(session_path, RECOVERED_DIR)
            print(f"🔄 Dipindah ke RECOVERED: {session_path.name}")
            return dest
        except Exception as e:
            if i < retries - 1:
                time.sleep(delay)
            else:
                print(f"⚠️ Gagal move ke RECOVERED: {e}")
    return None


def move_to_already_sold(session_path: Path, retries: int = 3, delay: float = 1.0) -> Path | None:
    for i in range(retries):
        try:
            dest = _move_preserving_user(session_path, ALREADY_SOLD_DIR)
            print(f"🔁 Dipindah ke ALREADY_SOLD: {session_path.name}")
            return dest
        except Exception as e:
            if i < retries - 1:
                time.sleep(delay)
            else:
                print(f"⚠️ Gagal move ke ALREADY_SOLD: {e}")
    return None


def move_to_cancelled(session_path: Path, retries: int = 3, delay: float = 1.0) -> Path | None:
    for i in range(retries):
        try:
            dest = _move_preserving_user(session_path, CANCELLED_DIR)
            print(f"🚫 Dipindah ke CANCELLED: {session_path.name}")
            return dest
        except Exception as e:
            if i < retries - 1:
                time.sleep(delay)
            else:
                print(f"⚠️ Gagal move ke CANCELLED: {e}")
    return None


def move_to_unauth(session_path: Path, retries: int = 3, delay: float = 1.0):
    for i in range(retries):
        try:
            _move_preserving_user(session_path, UNAUTH_DIR)
            print(f"📦 Dipindah ke UNAUTH: {session_path.name}")
            return
        except Exception as e:
            if i < retries - 1:
                time.sleep(delay)
            else:
                print(f"⚠️ Gagal move ke UNAUTH: {e}")


async def logout_and_close(client: TelegramClient):
    try:
        ok = await client.log_out()
        print(f"🧹 Logout session: {ok}")
    except Exception as e:
        print(f"⚠️ Logout error: {e}")
    try:
        await client.disconnect()
    except Exception:
        pass
