# engine/reg_world_v1.py
# Registrasi session via @WORLD_V1_FAST_BOT
# Logic diadaptasi dari world3.py (milik teman)

import asyncio
import random
import re
import shutil
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError, ServerError, TimedOutError,
    InvalidBufferError, AuthRestartError,
    MessageIdInvalidError, MessageNotModifiedError,
    AuthKeyDuplicatedError,
)
from telethon.tl.functions.auth import ResetAuthorizationsRequest

from engine.otp_listener import wait_for_otp_from_777000, get_otp_from_history
from config import (
    WORLD_V1_BOT,
    WORLD_V1_DIR,
    get_next_proxy,
    get_api,
)


# ─── Regex patterns ───────────────────────────────────────────────────────────
RE_CODE_REQUEST      = re.compile(r'Send the code you received', re.I)
RE_SUCCESS           = re.compile(r'Account Received Successfully', re.I)
RE_OTP               = re.compile(r'\b(\d{5})\b')
RE_FAILED_OTP        = re.compile(r'Failed to send OTP', re.I)
RE_ACTIVE_SESSION    = re.compile(r'active session.*cancel', re.I)
RE_WAITING_TIME      = re.compile(r'Waiting time', re.I)
RE_ERROR_SENDING_CODE = re.compile(r'An error occurred while sending the code', re.I)
RE_ERROR_CREATE_SESSION = re.compile(r'Error,\s*Failed to create client session', re.I)
RE_NUMBER_REGISTERED = re.compile(r'Number is already registered', re.I)
RE_PROCESS_FAILED    = re.compile(r'Error,\s*Process failed[\.\s]*Please try again', re.I)
RE_REQUEST_TIMEOUT   = re.compile(r'Error,\s*Request timeout[\.\s]*Please try again', re.I)
RE_OTP_INVALID       = re.compile(r'Error,\s*OTP is invalid or expired[\.\s]*Please try again', re.I)
RE_READONLY_DB       = re.compile(r'There is an error.*attempt to write a readonly database', re.I | re.S)
RE_NUMBER_EXISTS     = re.compile(r'This number is already exist', re.I)
RE_RESTRICTED        = re.compile(r'is restricted.*Contact Restriction.*silent.*Robots won\'t accept it', re.I | re.S)
RE_NUMBER_NOT_FOUND  = re.compile(r'Number not found', re.I)
RE_SPAM_REJECTED     = re.compile(r'Spam Contacts will be rejected', re.I)


# ─── DC fallback ──────────────────────────────────────────────────────────────
DEFAULT_DC_IPS = {
    1: '149.154.175.53',
    2: '149.154.167.51',
    3: '149.154.175.100',
    4: '149.154.167.91',
    5: '91.108.56.130',
}
STOR_DC_ID = 5


def _make_number_regex(nomor: str, prefix: str = '') -> re.Pattern:
    """Buat regex untuk mencocokkan nomor dalam teks bot."""
    nomor_clean = nomor.lstrip('+')
    return re.compile(rf'{prefix}\s*`?\+?{re.escape(nomor_clean)}`?', re.I)


def _parse_phone_from_path(session_path: Path) -> str:
    """Parse nomor telepon dari nama file session."""
    name = session_path.stem
    if not name.startswith('+'):
        name = f'+{name}'
    return name


def _move_session(session_path: Path, subfolder: str) -> bool:
    """Pindahkan session file (termasuk -journal, -wal, -shm) ke WORLD_V1_DIR/subfolder/."""
    dest_dir = WORLD_V1_DIR / subfolder
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not session_path or not session_path.exists():
        return False
    for ext in ['', '-journal', '-wal', '-shm']:
        src = session_path.parent / (session_path.name + ext)
        if src.exists():
            dest = dest_dir / (session_path.name + ext)
            try:
                if dest.exists():
                    dest.unlink()
                shutil.move(str(src), str(dest))
            except Exception as e:
                print(f"⚠️ Gagal pindah {src.name}: {e}")
    return True


def _make_string_session(session_path: Path) -> str:
    """Load .session file → return StringSession string (in-memory, no file writes).
    Always set port=443 for proxy compatibility."""
    import sqlite3
    from telethon.sessions import StringSession as _SS
    from telethon.crypto import AuthKey
    db = sqlite3.connect(str(session_path))
    try:
        row = db.execute("SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1").fetchone()
        if not row or not row[3]:
            raise ValueError("No valid auth data")
        dc_id, server_address, port, auth_key = row
        ss = _SS()
        ss._dc_id = dc_id
        ss._server_address = server_address
        ss._port = 443
        ss._auth_key = AuthKey(auth_key[:256])
        return ss.save()
    finally:
        db.close()


def _get_proxy_tuple():
    """Ambil random proxy dari config."""
    proxy = get_next_proxy()
    if proxy:
        # get_next_proxy returns ('socks5', host, port, True, user, pass)
        return proxy
    return None


# ─── Retry helper ─────────────────────────────────────────────────────────────

class StoppedError(Exception):
    pass


def _check_stop(stop_event: asyncio.Event | None):
    if stop_event and stop_event.is_set():
        raise StoppedError()


async def _wait_stop(stop_event: asyncio.Event | None, timeout: float):
    if stop_event is None:
        await asyncio.sleep(timeout)
        return False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def retry_with_timeout(operation, operation_name: str, max_retries=5,
                             initial_delay=2, timeout=10, client=None):
    """Retry wrapper dengan FloodWaitError handling — adaptasi dari world3."""
    retry_delay = initial_delay

    for attempt in range(max_retries):
        try:
            if asyncio.iscoroutinefunction(operation):
                result = await asyncio.wait_for(operation(), timeout=timeout)
            elif callable(operation):
                op_result = operation()
                if asyncio.iscoroutine(op_result):
                    result = await asyncio.wait_for(op_result, timeout=timeout)
                else:
                    loop = asyncio.get_event_loop()
                    result = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: op_result),
                        timeout=timeout
                    )
            else:
                result = await asyncio.wait_for(operation, timeout=timeout)
            return result

        except FloodWaitError as e:
            if e.seconds > 60:
                print(f"⏳ Flood wait {e.seconds}s — skip {operation_name}")
                raise Exception(f"Flood wait {e.seconds}s — operation skipped")
            else:
                print(f"⏳ Flood wait {e.seconds}s — waiting...")
                await asyncio.sleep(e.seconds)
                if attempt < max_retries - 1:
                    continue
                else:
                    raise

        except (ConnectionError, OSError, asyncio.exceptions.IncompleteReadError,
                ConnectionResetError, ServerError, TimedOutError, InvalidBufferError,
                BrokenPipeError, EOFError, AuthRestartError) as e:
            if attempt < max_retries - 1:
                print(f"🔁 Connection error attempt {attempt+1}/{max_retries}: {e}")
                if client and hasattr(client, 'disconnect') and hasattr(client, 'connect'):
                    try:
                        await client.disconnect()
                        await client.connect()
                    except Exception:
                        pass
                await asyncio.sleep(retry_delay)
                retry_delay *= 1.5
                continue
            else:
                raise

        except Exception as e:
            error_msg = str(e)
            if "Server sent a very new message" in error_msg or \
               "Server closed the connection" in error_msg or \
               "0 bytes read" in error_msg:
                if attempt < max_retries - 1:
                    if client and hasattr(client, 'disconnect') and hasattr(client, 'connect'):
                        try:
                            await client.disconnect()
                            await client.connect()
                        except Exception:
                            pass
                    await asyncio.sleep(2)
                    continue
            raise


# ─── Connect session with DC fallback ────────────────────────────────────────

async def _connect_session_with_fallback(client: TelegramClient, phone: str) -> bool:
    """Coba konek dengan DC 5 dulu, fallback ke DC lain jika gagal.
    Returns True jika berhasil."""
    try:
        client.session.set_dc(STOR_DC_ID, DEFAULT_DC_IPS[STOR_DC_ID], 443)
    except Exception:
        pass

    async def connect_and_check():
        await client.connect()
        await client.get_me()
        return True

    try:
        await retry_with_timeout(
            connect_and_check,
            f"koneksi {phone}",
            max_retries=2, initial_delay=2, timeout=30, client=client
        )
        return True
    except AuthKeyDuplicatedError:
        print(f"🔑 AuthKeyDuplicated untuk {phone}")
        raise
    except Exception as e:
        print(f"⚠️ DC {STOR_DC_ID} gagal untuk {phone}: {e}")
        await client.disconnect()
        # Fallback ke DC lain
        for dc_id, ip in DEFAULT_DC_IPS.items():
            if dc_id == STOR_DC_ID:
                continue
            try:
                print(f"🔁 Coba DC {dc_id} ({ip})...")
                client.session.set_dc(dc_id, ip, 443)
                await client.connect()
                await client.get_me()
                print(f"✅ Terhubung ke DC {dc_id}")
                return True
            except Exception as dc_e:
                print(f"⚠️ DC {dc_id} gagal: {dc_e}")
                try:
                    await client.disconnect()
                except Exception:
                    pass
        return False


# ─── STEP 2: Wait for code request from bot ───────────────────────────────────

async def _wait_for_code_request(admin_client: TelegramClient, phone: str,
                                  prefix: str, stop_event: asyncio.Event | None) -> str:
    """
    Pantau chat dengan @WORLD_V1_FAST_BOT setelah kirim nomor.
    Return:
      "code_request" → siap lanjut ambil OTP
      "retry"        → kirim ulang nomor (process failed / timeout pulih)
      "skip_xxx"     → skip dengan alasan tertentu
    """
    nomor_display = phone
    RE_NUM_CODE = _make_number_regex(nomor_display)

    cancel_sent = False
    bot_error_count = 0

    for _ in range(240):  # max ~4 menit
        _check_stop(stop_event)
        try:
            async for msg in admin_client.iter_messages(WORLD_V1_BOT, limit=10):
                if not msg or not msg.text:
                    continue
                text = msg.text

                # Readonly DB
                if RE_READONLY_DB.search(text):
                    print(f"{prefix} ❌ Readonly DB — pindah ke error")
                    return "error_readonly"

                # Number already exist
                if RE_NUMBER_EXISTS.search(text):
                    print(f"{prefix} ❌ Nomor already exist — pindah ke regist")
                    try:
                        await admin_client.delete_messages(WORLD_V1_BOT, msg.id)
                    except Exception:
                        pass
                    await admin_client.send_message(WORLD_V1_BOT, '/cancel')
                    await asyncio.sleep(2)
                    await admin_client.send_message(WORLD_V1_BOT, '/cancel')
                    return "number_exists"

                # Number not found
                if RE_NUMBER_NOT_FOUND.search(text) and RE_NUM_CODE.search(text):
                    print(f"{prefix} ❌ Number not found — skip")
                    try:
                        await admin_client.delete_messages(WORLD_V1_BOT, msg.id)
                    except Exception:
                        pass
                    await admin_client.send_message(WORLD_V1_BOT, '/cancel')
                    await asyncio.sleep(2)
                    return "number_not_found"

                # Process failed → retry
                if RE_PROCESS_FAILED.search(text):
                    print(f"{prefix} 🔄 'Process failed' — hapus pesan, kirim ulang")
                    try:
                        await admin_client.delete_messages(WORLD_V1_BOT, msg.id)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    await admin_client.send_message(WORLD_V1_BOT, '/cancel')
                    await asyncio.sleep(1)
                    await admin_client.send_message(WORLD_V1_BOT, nomor_display)
                    continue

                # Request timeout → retry
                if RE_REQUEST_TIMEOUT.search(text):
                    print(f"{prefix} 🔄 'Request timeout' — hapus pesan, kirim ulang")
                    try:
                        await admin_client.delete_messages(WORLD_V1_BOT, msg.id)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    await admin_client.send_message(WORLD_V1_BOT, '/cancel')
                    await asyncio.sleep(1)
                    await admin_client.send_message(WORLD_V1_BOT, nomor_display)
                    continue

                # Active session → /cancel + retry
                if RE_ACTIVE_SESSION.search(text) and not cancel_sent:
                    print(f"{prefix} 🔄 Session aktif — /cancel + kirim ulang")
                    await admin_client.send_message(WORLD_V1_BOT, '/cancel')
                    await asyncio.sleep(3)
                    await admin_client.send_message(WORLD_V1_BOT, '/start')
                    await asyncio.sleep(3)
                    await admin_client.send_message(WORLD_V1_BOT, nomor_display)
                    cancel_sent = True
                    continue

                # Failed OTP (bot side)
                if RE_FAILED_OTP.search(text) and nomor_display in text:
                    print(f"{prefix} ❌ Gagal kirim OTP dari bot — skip")
                    try:
                        await admin_client.delete_messages(WORLD_V1_BOT, msg.id)
                    except Exception:
                        pass
                    return "failed_otp"

                # Error sending code
                if RE_ERROR_SENDING_CODE.search(text):
                    bot_error_count += 1
                    print(f"{prefix} ⚠️ Bot error kirim kode ({bot_error_count}/3)")
                    try:
                        await admin_client.delete_messages(WORLD_V1_BOT, msg.id)
                    except Exception:
                        pass
                    if bot_error_count >= 3:
                        print(f"{prefix} ❌ Bot error 3x — skip")
                        return "bot_error"
                    await asyncio.sleep(10)
                    await admin_client.send_message(WORLD_V1_BOT, '/cancel')
                    await asyncio.sleep(3)
                    await admin_client.send_message(WORLD_V1_BOT, '/start')
                    await asyncio.sleep(3)
                    await admin_client.send_message(WORLD_V1_BOT, nomor_display)
                    continue

                # Error create session
                if RE_ERROR_CREATE_SESSION.search(text):
                    bot_error_count += 1
                    print(f"{prefix} ⚠️ Bot error buat session ({bot_error_count}/3)")
                    try:
                        await admin_client.delete_messages(WORLD_V1_BOT, msg.id)
                    except Exception:
                        pass
                    if bot_error_count >= 3:
                        print(f"{prefix} ❌ Bot error 3x — skip")
                        return "bot_error"
                    await asyncio.sleep(10)
                    await admin_client.send_message(WORLD_V1_BOT, '/cancel')
                    await asyncio.sleep(3)
                    await admin_client.send_message(WORLD_V1_BOT, '/start')
                    await asyncio.sleep(3)
                    await admin_client.send_message(WORLD_V1_BOT, nomor_display)
                    continue

                # Number already registered
                if RE_NUMBER_REGISTERED.search(text):
                    print(f"{prefix} ❌ Nomor sudah terdaftar — pindah ke regist")
                    try:
                        await admin_client.delete_messages(WORLD_V1_BOT, msg.id)
                    except Exception:
                        pass
                    return "number_registered"

                # Spam rejected
                if RE_SPAM_REJECTED.search(text):
                    print(f"{prefix} 🚫 SPAM REJECTED — pindah ke rejected")
                    try:
                        await admin_client.delete_messages(WORLD_V1_BOT, msg.id)
                    except Exception:
                        pass
                    return "spam_rejected"

                # Code request → success!
                if RE_CODE_REQUEST.search(text) and RE_NUM_CODE.search(text):
                    print(f"{prefix} ✅ Bot meminta kode — siap ambil OTP")
                    return "code_request"

        except StoppedError:
            raise
        except Exception as e:
            print(f"{prefix} ⚠️ Error baca pesan bot: {e}")
            await asyncio.sleep(2)
            continue

        await asyncio.sleep(1)

    # Timeout — kirim /cancel 2x lalu retry sekali
    print(f"{prefix} ⏰ Timeout, /cancel 2x lalu kirim ulang...")
    await admin_client.send_message(WORLD_V1_BOT, '/cancel')
    await asyncio.sleep(2)
    await admin_client.send_message(WORLD_V1_BOT, '/cancel')
    await asyncio.sleep(2)
    await admin_client.send_message(WORLD_V1_BOT, nomor_display)

    # Retry: pantau lagi
    for _ in range(240):
        _check_stop(stop_event)
        try:
            async for msg in admin_client.iter_messages(WORLD_V1_BOT, limit=10):
                if not msg or not msg.text:
                    continue
                text = msg.text
                if RE_CODE_REQUEST.search(text) and RE_NUM_CODE.search(text):
                    print(f"{prefix} ✅ Bot meminta kode (retry)")
                    return "code_request"
        except StoppedError:
            raise
        except Exception as e:
            print(f"{prefix} ⚠️ Error retry: {e}")
            await asyncio.sleep(2)
            continue
        await asyncio.sleep(1)

    print(f"{prefix} ❌ Bot tidak meminta kode setelah 2x percobaan — skip")
    return "skip_no_code"


# ─── STEP 6: Wait for confirmation after OTP sent ────────────────────────────

async def _wait_for_confirmation(admin_client: TelegramClient, phone: str,
                                  prefix: str, stop_event: asyncio.Event | None) -> str:
    """
    Tunggu konfirmasi dari bot setelah OTP dikirim.
    Return: "success" | "otp_invalid" | "spam_rejected" | "restricted" | "timeout"
    """
    nomor_display = phone
    RE_NUM_CODE = _make_number_regex(nomor_display)

    for _ in range(300):  # max ~5 menit
        _check_stop(stop_event)
        try:
            async for msg in admin_client.iter_messages(WORLD_V1_BOT, limit=10):
                if not msg or not msg.text:
                    continue
                text = msg.text

                # OTP invalid
                if RE_OTP_INVALID.search(text):
                    print(f"{prefix} ❌ OTP invalid/expired — skip")
                    try:
                        await admin_client.delete_messages(WORLD_V1_BOT, msg.id)
                    except Exception:
                        pass
                    await admin_client.send_message(WORLD_V1_BOT, '/cancel')
                    await asyncio.sleep(1)
                    return "otp_invalid"

                # Spam rejected (post-OTP)
                if RE_SPAM_REJECTED.search(text):
                    print(f"{prefix} 🚫 SPAM REJECTED (post-OTP)")
                    try:
                        await admin_client.delete_messages(WORLD_V1_BOT, msg.id)
                    except Exception:
                        pass
                    return "spam_rejected"

                # Restricted
                if RE_RESTRICTED.search(text) and RE_NUM_CODE.search(text):
                    print(f"{prefix} 🚫 RESTRICTED!")
                    return "restricted"

                # Success!
                if RE_SUCCESS.search(text) and RE_NUM_CODE.search(text):
                    print(f"{prefix} ✅ Sukses dikonfirmasi bot!")
                    return "success"

        except StoppedError:
            raise
        except Exception as e:
            print(f"{prefix} ⚠️ Error pantau konfirmasi: {e}")
            await asyncio.sleep(2)
            continue

        await asyncio.sleep(1)

    return "timeout"


# ─── STEP 7: Infinite monitoring for delayed success ─────────────────────────

async def _infinite_monitor(admin_client: TelegramClient, phone: str,
                             prefix: str, stop_event: asyncio.Event | None) -> bool:
    """
    Monitoring infinite setelah timeout STEP 6 — terus pantau sampe sukses.
    Return True jika akhirnya sukses, False jika tidak.
    """
    nomor_display = phone
    RE_NUM_CODE = _make_number_regex(nomor_display)
    print(f"{prefix} 🔍 Mulai monitoring infinite...")

    while True:
        _check_stop(stop_event)
        try:
            async for msg in admin_client.iter_messages(WORLD_V1_BOT, limit=5):
                if not msg or not msg.text:
                    continue
                text = msg.text
                if RE_SUCCESS.search(text) and RE_NUM_CODE.search(text):
                    print(f"{prefix} ✅ Akhirnya sukses!")
                    return True
        except StoppedError:
            raise
        except Exception as e:
            print(f"{prefix} ⚠️ Error monitor: {e}")
        await asyncio.sleep(2)


# ─── Process one session ──────────────────────────────────────────────────────

async def process_one(
    admin_client: TelegramClient,
    api_id: int,
    api_hash: str,
    session_path: Path,
    idx: int,
    total: int,
    stop_event: asyncio.Event | None = None,
    event_cb=None,
) -> str:
    """
    Proses registrasi SATU session via @WORLD_V1_FAST_BOT.
    Mengembalikan string status:
      "success"            → ✅ sukses, session dihapus
      "error_readonly"     → WORLD_V1/error/
      "number_exists"      → WORLD_V1/regist/
      "number_not_found"   → skip (session tetap, tidak dipindah)
      "failed_otp"         → skip
      "bot_error"          → skip
      "number_registered"  → WORLD_V1/regist/
      "spam_rejected"      → WORLD_V1/rejected/ + reset auth
      "restricted"         → WORLD_V1/error/ + reset auth
      "otp_invalid"        → skip
      "skip_no_code"       → skip
      "session_corrupt"    → WORLD_V1/corrupt/
      "auth_key_dup"       → WORLD_V1/error/
    """
    phone = _parse_phone_from_path(session_path)
    prefix = f'[{idx}/{total}]'
    nomor_display = phone

    if event_cb:
        await event_cb("phone_sent", phone, "")

    # ================================================================= #
    # STEP 1: Kirim nomor ke bot                                        #
    # ================================================================= #
    print(f"{prefix} 📤 Kirim nomor: {nomor_display}")
    await admin_client.send_message(WORLD_V1_BOT, nomor_display)

    # ================================================================= #
    # STEP 2: Tunggu bot minta kode / deteksi error                     #
    # ================================================================= #
    step2_result = await _wait_for_code_request(admin_client, phone, prefix, stop_event)

    if step2_result != "code_request":
        # Handle error cases that need session file movement
        if step2_result == "error_readonly":
            _move_session(session_path, "error")
        elif step2_result == "number_exists":
            _move_session(session_path, "regist")
        elif step2_result == "number_registered":
            _move_session(session_path, "regist")
        elif step2_result == "spam_rejected":
            await _reset_auth_and_move(session_path, phone, prefix)
            return "spam_rejected"
        return step2_result  # "number_not_found", "failed_otp", "bot_error", "skip_no_code"

    # ================================================================= #
    # STEP 3: Login session + DC fallback                              #
    # ================================================================= #
    proxy = _get_proxy_tuple()
    print(f"{prefix} 🔌 Login session: {nomor_display}")

    str_session = None
    try:
        from telethon.sessions import StringSession
        session_string = _make_string_session(session_path)
        str_session = StringSession(session_string)
        client = TelegramClient(
            str_session,
            api_id, api_hash,
            proxy=proxy,
            timeout=30,
            connection_retries=3,
            request_retries=3,
            retry_delay=2,
        )
    except ValueError:
        print(f"{prefix} ❌ Session file tidak punya auth data (corrupt)")
        return "session_corrupt"
    except Exception as e:
        print(f"{prefix} ❌ Gagal buat client: {e}")
        return "session_corrupt"

    # DC fallback connect
    dc_ok = False
    try:
        dc_ok = await _connect_session_with_fallback(client, phone)
    except AuthKeyDuplicatedError:
        print(f"{prefix} 🔑 AuthKeyDuplicated — pindah ke error")
        try:
            await client.disconnect()
        except Exception:
            pass
        _move_session(session_path, "error")
        await admin_client.send_message(WORLD_V1_BOT, '/cancel')
        return "auth_key_dup"
    except StoppedError:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise

    if not dc_ok:
        print(f"{prefix} ❌ Gagal connect session")
        try:
            await client.disconnect()
        except Exception:
            pass
        return "session_corrupt"

    # Check authorization
    _check_stop(stop_event)
    try:
        await asyncio.wait_for(client.get_me(), timeout=10)
    except Exception as e:
        print(f"{prefix} ⚠️ Koneksi tidak stabil: {e}")
        try:
            await client.disconnect()
            await asyncio.sleep(2)
            await client.connect()
            await asyncio.wait_for(client.get_me(), timeout=10)
        except Exception as re_e:
            print(f"{prefix} ❌ Gagal reconnect: {re_e}")
            return "session_corrupt"

    if not await client.is_user_authorized():
        print(f"{prefix} ❌ Session tidak terautentikasi!")
        await admin_client.send_message(WORLD_V1_BOT, '/cancel')
        await asyncio.sleep(3)
        try:
            await client.disconnect()
        except Exception:
            pass
        _move_session(session_path, "corrupt")
        return "session_corrupt"

    await asyncio.sleep(3)

    # ================================================================= #
    # STEP 4: Ambil OTP dari 777000                                     #
    # ================================================================= #
    _check_stop(stop_event)

    async def get_otp_with_fallback():
        # Coba realtime listener dulu
        code = await wait_for_otp_from_777000(client, timeout=70)
        if code:
            return code
        # Fallback: history
        code = await get_otp_from_history(client)
        return code

    otp_code = None
    try:
        otp_code = await retry_with_timeout(
            get_otp_with_fallback,
            f"OTP {nomor_display}",
            max_retries=3, initial_delay=2, timeout=90, client=client
        )
    except Exception as e:
        print(f"{prefix} ❌ Gagal dapat OTP: {e}")

    if not otp_code:
        print(f"{prefix} ❌ OTP tidak ditemukan!")
        try:
            await client.disconnect()
        except Exception:
            pass
        return "skip_no_code"

    print(f"{prefix} 🔑 OTP: {otp_code}")
    if event_cb:
        await event_cb("otp_sent", phone, otp_code)

    # ================================================================= #
    # STEP 5: Kirim OTP ke bot                                          #
    # ================================================================= #
    await admin_client.send_message(WORLD_V1_BOT, otp_code)
    print(f"{prefix} 📨 OTP dikirim, menunggu konfirmasi...")

    # ================================================================= #
    # STEP 6: Tunggu konfirmasi                                         #
    # ================================================================= #
    confirm_result = await _wait_for_confirmation(admin_client, phone, prefix, stop_event)

    if confirm_result == "success":
        pass  # lanjut ke STEP 7 logout
    elif confirm_result == "otp_invalid":
        try:
            await client.disconnect()
        except Exception:
            pass
        return "otp_invalid"
    elif confirm_result == "spam_rejected":
        try:
            await client(ResetAuthorizationsRequest())
            print(f"{prefix} 🔑 Auth direset")
        except Exception as e:
            print(f"{prefix} ⚠️ Gagal reset auth: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        _move_session(session_path, "rejected")
        return "spam_rejected"
    elif confirm_result == "restricted":
        try:
            await client(ResetAuthorizationsRequest())
            print(f"{prefix} 🔑 Auth direset")
        except Exception as e:
            print(f"{prefix} ⚠️ Gagal reset auth: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        _move_session(session_path, "error")
        return "restricted"
    elif confirm_result == "timeout":
        # Masih ada harapan — monitoring infinite (tanpa timeout)
        print(f"{prefix} ⏰ Timeout konfirmasi, lanjut monitoring infinite...")
    else:
        try:
            await client.disconnect()
        except Exception:
            pass
        return confirm_result

    # ================================================================= #
    # STEP 6b: Infinite monitoring setelah timeout                       #
    # (FIXED: pake flag, bukan break otomatis)                          #
    # ================================================================= #
    if confirm_result == "timeout":
        found = await _infinite_monitor(admin_client, phone, prefix, stop_event)
        if not found:
            # Udah ditunggu tapi tetep gak sukses — lanjut logout aja
            print(f"{prefix} ⚠️ Tidak terdeteksi sukses, lanjut cleanup...")

    # ================================================================= #
    # Logout client                                                     #
    # ================================================================= #
    try:
        if client.is_connected():
            try:
                await retry_with_timeout(
                    client.log_out,
                    f"logout {nomor_display}",
                    max_retries=2, initial_delay=1, timeout=10, client=client
                )
                print(f"{prefix} 🔓 Logout {nomor_display}")
            except Exception as e:
                print(f"{prefix} ⚠️ Gagal logout: {e}")
        else:
            print(f"{prefix} ℹ️ Koneksi sudah terputus")
    except Exception as e:
        print(f"{prefix} ⚠️ Gagal logout: {e}")

    # Disconnect
    try:
        if client.is_connected():
            await retry_with_timeout(
                client.disconnect,
                f"disconnect {nomor_display}",
                max_retries=1, initial_delay=1, timeout=5, client=client
            )
    except Exception as e:
        print(f"{prefix} ⚠️ Gagal disconnect: {e}")

    # ================================================================= #
    # STEP 7: Hapus session file (sukses)                               #
    # ================================================================= #
    if session_path.exists():
        try:
            # Hapus semua file terkait session
            for ext in ['', '-journal', '-wal', '-shm']:
                f = session_path.parent / (session_path.name + ext)
                if f.exists():
                    f.unlink()
            print(f"{prefix} 🗑️ Session {nomor_display} dihapus!")
        except Exception as e:
            print(f"{prefix} ⚠️ Gagal hapus session: {e}")

    return "success"


async def _reset_auth_and_move(session_path: Path, phone: str, prefix: str):
    """Login session, reset authorization, lalu pindahkan ke rejected."""
    api_id, api_hash = get_api()
    client = None
    try:
        from telethon.sessions import StringSession
        session_string = _make_string_session(session_path)
        str_session = StringSession(session_string)
        client = TelegramClient(str_session, api_id, api_hash, timeout=15,
                                connection_retries=2, request_retries=2)
        await client.connect()
        if await client.is_user_authorized():
            await client(ResetAuthorizationsRequest())
            print(f"{prefix} 🔑 Auth direset untuk {phone}")
    except Exception as e:
        print(f"{prefix} ⚠️ Gagal reset auth: {e}")
    finally:
        try:
            if client and client.is_connected():
                await client.disconnect()
        except Exception:
            pass
    _move_session(session_path, "rejected")


# ─── Run batch ────────────────────────────────────────────────────────────────

async def run_batch(
    admin_client: TelegramClient,
    api_id: int,
    api_hash: str,
    session_files: list[Path],
    progress_cb=None,
    event_cb=None,
    stop_event: asyncio.Event | None = None,
) -> dict:
    """
    Proses batch registrasi semua session via @WORLD_V1_FAST_BOT.
    Sama pattern kayak sell_sessions_with_bot di seller.py.

    Returns dict:
      {"success": N, "error": N, "skipped": N, "rejected": N, 
       "total": total_sessions, "detail": {...}}
    """
    total = len(session_files)
    results = {
        "success": 0,
        "error": 0,
        "skipped": 0,
        "rejected": 0,
        "total": total,
        "detail": {},
    }

    for idx, session_path in enumerate(session_files, 1):
        _check_stop(stop_event)

        try:
            status = await process_one(
                admin_client=admin_client,
                api_id=api_id,
                api_hash=api_hash,
                session_path=session_path,
                idx=idx,
                total=total,
                stop_event=stop_event,
                event_cb=event_cb,
            )
        except StoppedError:
            print("🛑 Batch dihentikan oleh user.")
            break
        except Exception as e:
            print(f"[{idx}/{total}] ❌ Error fatal: {e}")
            import traceback
            traceback.print_exc()
            status = "error_fatal"

        phone = _parse_phone_from_path(session_path)

        if status == "success":
            results["success"] += 1
            if event_cb:
                await event_cb("success", phone, "")
        elif status in ("error_readonly", "bot_error", "restricted",
                        "auth_key_dup", "session_corrupt", "error_fatal"):
            results["error"] += 1
            if event_cb:
                await event_cb("fail", phone, status)
        elif status in ("number_exists", "number_registered", "number_not_found",
                        "failed_otp", "skip_no_code", "skip_no_code", "otp_invalid",
                        "skip"):
            results["skipped"] += 1
            if event_cb:
                await event_cb("fail", phone, status)
        elif status == "spam_rejected":
            results["rejected"] += 1
            if event_cb:
                await event_cb("fail", phone, status)

        results["detail"][phone] = status

        # Update progress
        if progress_cb:
            await progress_cb(results["success"], total - results["success"] - results["error"] - results["skipped"] - results["rejected"],
                              total, session_path.name)
        await asyncio.sleep(2)

    return results
