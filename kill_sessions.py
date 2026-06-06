# kill_sessions.py

import asyncio
import sys
import json
import shutil
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    SessionPasswordNeededError,
    PhoneNumberBannedError,
    UserDeactivatedError,
    UserDeactivatedBanError,
    RPCError,
)
from telethon.tl.functions.auth import ResetAuthorizationsRequest

# -----------------------------
# CONFIG
# -----------------------------

SOURCE_DIR = Path("kill")
DONE_DIR = Path("done")
FAILED_DIR = Path("failed")
FROZEN_DIR = Path("frozen")
CONFIG_FILE = Path("angga.json")

MAX_CONCURRENCY = 10  # batas concurrency

# Timeout (detik) per operasi
TIMEOUT_CONNECT    = 30
TIMEOUT_AUTH_CHECK = 15
TIMEOUT_GET_ME     = 15
TIMEOUT_RESET_AUTH = 20
TIMEOUT_DISCONNECT = 5
TIMEOUT_SESSION    = 90  # hard cap per session keseluruhan


def load_config():
    """Memuat API_ID dan API_HASH dari file config JSON."""
    if not CONFIG_FILE.exists():
        print(f"❌ Config JSON tidak ditemukan: {CONFIG_FILE.resolve()}")
        sys.exit(1)

    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"❌ Gagal membaca config JSON: {e}", file=sys.stderr)
        sys.exit(1)

    api_id = data.get("API_ID")
    api_hash = data.get("API_HASH")

    if not api_id or not api_hash:
        print("❌ Config JSON harus punya key 'API_ID' dan 'API_HASH'", file=sys.stderr)
        sys.exit(1)

    try:
        api_id = int(api_id)
    except ValueError:
        print("❌ API_ID di config harus integer", file=sys.stderr)
        sys.exit(1)

    print(f"✅ Config dimuat dari {CONFIG_FILE.name} (API_ID={api_id})")
    return api_id, api_hash


def safe_move(src: Path, dst_dir: Path):
    """Memindahkan file dengan aman, membuat folder jika belum ada."""
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst_dir / src.name))
    except Exception as e:
        print(f"CRITICAL: Gagal memindahkan file {src.name} ke {dst_dir}: {e}", file=sys.stderr)


def is_frozen_message(msg: str) -> bool:
    """Cek apakah pesan RPC menandakan akun frozen."""
    msg = msg.upper()
    return any(k in msg for k in ("FROZEN_METHOD_INVALID", "ACCOUNT_FROZEN", "USER_DEACTIVATED"))


async def safe_disconnect(client: TelegramClient):
    """Disconnect aman dengan timeout — tidak bisa hang selamanya."""
    try:
        await client.disconnect()
        try:
            await asyncio.wait_for(asyncio.shield(client.disconnected), timeout=TIMEOUT_DISCONNECT)
        except (asyncio.TimeoutError, Exception):
            pass
    except Exception:
        pass


async def terminate_other_sessions(session_path: Path, api_id: int, api_hash: str) -> str:
    """
    Menghubungkan ke satu sesi dan me-reset otorisasi lain.
    Return status: "success", "failed", atau "frozen"
    """
    # FIX: TelegramClient dikonfigurasi dengan timeout & connection_retries
    client = TelegramClient(
        str(session_path), api_id, api_hash,
        connection_retries=1,
        retry_delay=2,
        timeout=30,
    )
    phone = "N/A"

    try:
        # FIX: connect() dengan timeout
        try:
            await asyncio.wait_for(client.connect(), timeout=TIMEOUT_CONNECT)
        except asyncio.TimeoutError:
            print(f"⏰ [{session_path.name}] connect() timeout (>{TIMEOUT_CONNECT}s)", file=sys.stderr)
            return "failed"

        # FIX: is_user_authorized() dengan timeout
        try:
            authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=TIMEOUT_AUTH_CHECK)
        except asyncio.TimeoutError:
            print(f"⏰ [{session_path.name}] auth check timeout (>{TIMEOUT_AUTH_CHECK}s)", file=sys.stderr)
            return "failed"

        if not authorized:
            print(f"⚠️ [{session_path.name}] Sesi tidak terotorisasi.", file=sys.stderr)
            return "failed"

        # FIX: get_me() dengan timeout
        try:
            me = await asyncio.wait_for(client.get_me(), timeout=TIMEOUT_GET_ME)
            if me and me.phone:
                phone = f"+{me.phone}"
        except asyncio.TimeoutError:
            print(f"⏰ [{session_path.name}] get_me() timeout (>{TIMEOUT_GET_ME}s)", file=sys.stderr)
            return "failed"

        # FIX: ResetAuthorizationsRequest() dengan timeout
        try:
            await asyncio.wait_for(client(ResetAuthorizationsRequest()), timeout=TIMEOUT_RESET_AUTH)
        except asyncio.TimeoutError:
            print(f"⏰ [{session_path.name}] ResetAuthorizations timeout (>{TIMEOUT_RESET_AUTH}s)", file=sys.stderr)
            return "failed"

        print(f"✅ [{phone}] Berhasil: Semua sesi lain telah di-terminate.")
        return "success"

    except RPCError as e:
        msg = str(e)
        if is_frozen_message(msg):
            print(f"🧊 [{session_path.name}] Akun FROZEN (RPCError): {msg}", file=sys.stderr)
            return "frozen"

        print(f"💥 [{session_path.name}] RPCError: {e.__class__.__name__}: {e}", file=sys.stderr)
        return "failed"

    except SessionPasswordNeededError:
        print(f"🔐 [{session_path.name}] Dilindungi 2FA, tidak bisa login.", file=sys.stderr)
        return "failed"

    except (
        AuthKeyUnregisteredError,
        UserDeactivatedError,
        UserDeactivatedBanError,
        PhoneNumberBannedError,
    ):
        print(f"🚫 [{session_path.name}] Sesi tidak valid atau akun diblokir.", file=sys.stderr)
        return "failed"

    except Exception as e:
        print(f"💥 [{session_path.name}] Error tak terduga: {e.__class__.__name__}: {e}", file=sys.stderr)
        return "failed"

    finally:
        # FIX: ganti client.disconnect() biasa dengan safe_disconnect() berTimeout
        await safe_disconnect(client)


async def main():
    api_id, api_hash = load_config()

    if not SOURCE_DIR.exists():
        print(f"❌ Folder sumber '{SOURCE_DIR}' tidak ditemukan.")
        sys.exit(1)

    DONE_DIR.mkdir(exist_ok=True)
    FAILED_DIR.mkdir(exist_ok=True)
    FROZEN_DIR.mkdir(exist_ok=True)

    session_files = [p for p in SOURCE_DIR.rglob("*.session") if p.is_file()]
    total = len(session_files)

    if total == 0:
        print("📦 Tidak ada file .session yang ditemukan di folder 'sessions'.")
        return

    print(f"\n📦 Ditemukan {total} file sesi.")
    start = input(f"Anda akan me-reset otorisasi (terminate other devices) untuk {total} akun. Lanjutkan? [y/n]: ").strip().lower()
    if start != "y":
        print("⏹️  Proses dibatalkan.")
        return

    print("\n🚀 Memulai proses terminasi dan pemindahan sesi...\n")

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    done_count = 0
    success_count = 0
    failed_count = 0
    frozen_count = 0

    async def worker(session_path):
        nonlocal done_count, success_count, failed_count, frozen_count
        async with sem:
            # FIX: per-session hard timeout agar slot semaphore tidak terkunci selamanya
            try:
                status = await asyncio.wait_for(
                    terminate_other_sessions(session_path, api_id, api_hash),
                    timeout=TIMEOUT_SESSION,
                )
            except asyncio.TimeoutError:
                print(f"\n⏰ [{session_path.name}] Session timeout (>{TIMEOUT_SESSION}s)", file=sys.stderr)
                status = "failed"

            if status == "success":
                safe_move(session_path, DONE_DIR)
                success_count += 1
            elif status == "frozen":
                safe_move(session_path, FROZEN_DIR)
                frozen_count += 1
            else:
                safe_move(session_path, FAILED_DIR)
                failed_count += 1

            done_count += 1
            print(f"\r⏳ Progress: {done_count}/{total}", end="", flush=True)

    tasks = [worker(p) for p in session_files]
    # FIX: return_exceptions=True agar satu task crash tidak membatalkan semua task lain
    await asyncio.gather(*tasks, return_exceptions=True)

    # Summary akhir
    print("\n\n🎉 Proses selesai!")
    print(f"  - Berhasil : {success_count} sesi -> '{DONE_DIR}'")
    print(f"  - Frozen  : {frozen_count} sesi -> '{FROZEN_DIR}'")
    print(f"  - Gagal   : {failed_count} sesi -> '{FAILED_DIR}'")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⏹️  Proses dihentikan oleh pengguna.")
