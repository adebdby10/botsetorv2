# engine/seller.py
# Flow jual akun dengan bot buyer menggunakan admin_client

import asyncio
import random  # buat delay tambahan floodwait & kirim pesan
import re  # buat parse detik
import zipfile
import shutil
from datetime import datetime
from pathlib import Path

# ─── Stop-aware wait helper ──────────────────────────────────────────────────
# Raises CancelledError if stop_event is set while waiting, so callers can
# abort promptly when user hits Stop.

async def _wait_stop(stop_event: asyncio.Event | None, timeout: float):
    """Sleep for `timeout` seconds, but return early if stop_event is set."""
    if stop_event is None:
        await asyncio.sleep(timeout)
        return False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        return True  # stop was requested
    except asyncio.TimeoutError:
        return False  # normal expiry


class StoppedError(Exception):
    """Raised when stop_event is set during a long-running operation."""
    pass


def _check_stop(stop_event: asyncio.Event | None):
    """Raise StoppedError if stop_event is set."""
    if stop_event and stop_event.is_set():
        raise StoppedError()
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import RPCError, FreshResetAuthorisationForbiddenError
from telethon.tl.functions.account import (
    GetPasswordRequest,
    GetAuthorizationsRequest,
    ResetAuthorizationRequest,
)
from telethon.tl.functions.auth import LogOutRequest
from telethon.tl.functions.contacts import GetContactsRequest, ImportContactsRequest
from telethon.tl.types import InputPhoneContact

from engine.otp_listener import wait_for_otp_from_777000, get_otp_from_history
from utils.file_manager import (
    move_to_invalid_2fa,
    move_to_2fa_on,
    move_to_other_device,
    move_to_unauth,
    move_to_rejected,
    move_to_recovered,
    move_to_already_sold,
    move_to_cancelled,
    parse_phone_from_filename,
)
from utils.logger import log_pending, log_success, log_failed, log_invalid_2fa
from config import (
    SESSIONS_DIR,
    ROOT,
    WARMUP_DELAY_MIN,
    WARMUP_DELAY_MAX,
    TASK_STAGGER_MIN,
    TASK_STAGGER_MAX,
    GRACE_PERIOD_SECONDS,
    RECOVERED_DIR,
    ALREADY_SOLD_DIR,
    CANCELLED_DIR,
    get_next_proxy,
)

OLD_PASS = "wsl2026wsl"

SUCCESS_HINTS = [
    "successfully registered",
    "berhasil",
    "registered",
    "success",
    "confirmation",
]
ERROR_HINTS = [
    "error",
    "invalid",
    "try again",
    "too many",
    "sudah terdaftar",
    "already registered",
]

# Regex helper (dipakai juga di reply-mode buyer)
OTP_TO_RE = re.compile(r"otp to\s*(\+?\d{6,20})", re.I)
NUMBER_IN_MSG_RE = re.compile(r"number:\s*(\+?\d{6,20})", re.I)


class CapacityFullError(Exception):
    """
    Kapasitas buyer penuh → stop semua proses batch.
    Dipakai untuk break di sell_sessions_with_bot.
    """
    pass


def _get_admin_label(admin_client: TelegramClient) -> str:
    """
    Ambil nama session admin (file .session) kalau ada.
    Dipakai buat log: 'Session Admin: xxx.session' sebelum Mulai Jual.
    """
    try:
        sess = getattr(admin_client, "session", None)
        if sess is None:
            return "unknown_admin"
        filename = getattr(sess, "filename", None) or getattr(sess, "file", None)
        if filename:
            return Path(str(filename)).name
    except Exception as e:
        print(f"⚠️ Gagal baca admin session label: {e}")
    return "unknown_admin"


async def _send_with_delay(
    target,
    text: str,
    min_delay: float = 1.0,
    max_delay: float = 5.0,
):
    """
    Helper: kirim pesan dengan delay random 1-5 detik
    supaya ga terlalu spammy ke buyer bot.
    target: bisa Conversation (conv) atau entity langsung.
    """
    delay = random.uniform(min_delay, max_delay)
    try:
        print(f"⏳ Delay {delay:.1f}s sebelum kirim: {text}")
    except Exception:
        print(f"⏳ Delay {delay:.1f}s sebelum kirim pesan ke buyer.")
    await asyncio.sleep(delay)
    await target.send_message(text)


async def _conv_send_and_wait(conv, text: str, timeout: int = 10):
    # kirim pesan ke bot buyer dan tunggu balasan (pakai delay random)
    await _send_with_delay(conv, text)
    return await conv.get_response(timeout=timeout)


def _looks_success(msg_text: str) -> bool:
    # deteksi pesan sukses dari bot buyer
    if not msg_text:
        return False
    low = msg_text.lower()
    res = any(h in low for h in SUCCESS_HINTS)
    print(f"⏩ _looks_success result: {res} | Msg: {msg_text}")
    return res


def _looks_error(msg_text: str) -> bool:
    # deteksi pesan error dari bot buyer
    if not msg_text:
        return False
    low = msg_text.lower()
    return any(err in low for err in ERROR_HINTS)


def _looks_balance_notif(msg_text: str) -> bool:
    """
    Notif pembayaran / processed account (akun lama) → harus diabaikan,
    termasuk info post-check kayak multiple active sessions dari buyer.
    """
    if not msg_text:
        return False
    low = msg_text.lower()
    if "we have successfully processed your account" in low:
        return True
    if "price:" in low and "status:" in low:
        return True
    if "congratulations" in low and "balance" in low:
        return True
    if "multiple active sessions detected for the number" in low:
        return True
    return False


def _looks_frozen(msg_text: str) -> bool:
    # akun frozen: jangan logout, pindahin ke folder frozen
    if not msg_text:
        return False
    low = msg_text.lower()
    return "account is frozen" in low or "this account is frozen" in low


def _looks_rejected(raw: str) -> bool:
    txt = raw.lower()
    return (
        "account rejected" in txt
        or "contact list restriction" in txt
        or "cannot proceed to confirmation" in txt
        or ("has been returned" in txt and "number" in txt)
    )


async def terminate_other_devices(client: TelegramClient, phone: str) -> str:
    """
    Pastikan sebelum jual:
    - cuma ada 1 device (session Telethon ini)
    - reset semua authorization lain
    Returns: "ok" | "fresh" | "error"
    - "fresh": session terlalu baru (<24j), FreshResetAuthorisationForbiddenError
    - "error": multi-device masih ada setelah reset, atau exception lain
    """
    try:
        auths = await client(GetAuthorizationsRequest())
        auth_list = auths.authorizations or []
        print(f"📊 [AUTH PRE] {phone}: {len(auth_list)} authorizations")
        removed = 0

        for idx, auth in enumerate(auth_list, start=1):
            dev_info = (
                f"device={getattr(auth, 'device_model', '?')} | "
                f"app={getattr(auth, 'app_name', '?')} | "
                f"platform={getattr(auth, 'platform', '?')}"
            )
            print(f"   [PRE] #{idx} current={auth.current} | {dev_info}")
            if not auth.current:
                try:
                    await client(ResetAuthorizationRequest(hash=auth.hash))
                    removed += 1
                    print(f"   🔻 Reset auth #{idx} untuk {phone}")
                except FreshResetAuthorisationForbiddenError:
                    print(
                        f"⚠️ Session {phone} terlalu baru (<24j), "
                        f"tidak bisa kill device #{idx} → pindah ke OTHER_DEVICE."
                    )
                    return "fresh"
                except RPCError as e:
                    print(f"⚠️ Gagal kill device #{idx} {phone}: {e}")

        auths_after = await client(GetAuthorizationsRequest())
        auth_list_after = auths_after.authorizations or []
        print(f"📊 [AUTH POST] {phone}: {len(auth_list_after)} authorizations")

        for idx, auth in enumerate(auth_list_after, start=1):
            dev_info = (
                f"device={getattr(auth, 'device_model', '?')} | "
                f"app={getattr(auth, 'app_name', '?')} | "
                f"platform={getattr(auth, 'platform', '?')}"
            )
            print(f"   [POST] #{idx} current={auth.current} | {dev_info}")

        if len(auth_list_after) > 1:
            print(
                f"❌ Masih ada {len(auth_list_after)} devices aktif untuk {phone} "
                f"setelah reset → session ini TIDAK boleh dijual (multi_devices_precheck)."
            )
            return "error"

        print(f"🗑️ Devices lain di-terminate untuk {phone} (removed={removed})")
        return "ok"

    except Exception as e:
        print(f"⚠️ Gagal terminate devices {phone}: {e}")
        return "error"


async def check_and_disable_2fa(
    client: TelegramClient,
    phone: str,
    session_path: Path,
) -> bool:
    """
    Cek 2FA dulu:
    - 2FA ON  → disconnect + pindah ke 2FA_ON/ (user bisa ambil kembali)
    - 2FA OFF → terminate_other_devices()
      - "ok"    → return True (lanjut jual)
      - "fresh" → disconnect + pindah ke OTHER_DEVICE/ (user bisa ambil kembali)
      - "error" → return False (skip)
    """
    try:
        pw = await client(GetPasswordRequest())
        if pw and pw.has_password:
            hint = pw.hint or ""
            print(f"🔒 2FA AKTIF untuk {phone}, hint: {hint} → pindah ke 2FA_ON.")
            log_invalid_2fa(phone, "-", f"2fa_on_hint:{hint}")
            try:
                await client.disconnect()
            except Exception:
                pass
            move_to_2fa_on(session_path)
            return False

        print(f"🟢 2FA OFF untuk {phone}, lanjut cek devices (single device policy).")
        result = await terminate_other_devices(client, phone)
        if result == "ok":
            return True
        elif result == "fresh":
            print(f"⚠️ Session {phone} terlalu baru → pindah ke OTHER_DEVICE.")
            try:
                await client.disconnect()
            except Exception:
                pass
            move_to_other_device(session_path)
            return False
        else:
            print(f"❌ Single-device precheck GAGAL untuk {phone}, jual dibatalkan.")
            return False

    except Exception as e:
        print(f"❌ Error cek 2FA {phone}: {e}")
        return False


def _load_seeder_numbers() -> list[str]:
    path = ROOT / "number.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return [l.strip() for l in lines if l.strip()]
    except Exception as e:
        print(f"⚠️ Gagal baca number.txt: {e}")
        return []


async def auto_solve_contact_restriction(
    client: TelegramClient,
    phone: str,
) -> bool:
    """
    Cek kontak akun sebelum jual.
    Jika 0 kontak: otomatis import nomor dari number.txt (max 3 percobaan).
    Return False hanya jika semua percobaan gagal.
    """
    try:
        res = await client(GetContactsRequest(hash=0))
        count = len(res.users)
        print(f"📇 Kontak {phone}: {count} contact(s).")
        if count >= 1:
            return True
    except Exception as e:
        print(f"⚠️ Gagal cek kontak {phone}: {e} → treat as allowed.")
        return True

    print(f"📵 {phone} punya 0 kontak → coba auto-import dari number.txt...")
    seeders = _load_seeder_numbers()
    random.shuffle(seeders)
    for seeder in seeders[:3]:
        try:
            await client(ImportContactsRequest([
                InputPhoneContact(client_id=0, phone=seeder, first_name="Contact", last_name="")
            ]))
            re_check = await client(GetContactsRequest(hash=0))
            if len(re_check.users) >= 1:
                print(f"✅ Kontak berhasil di-import untuk {phone} (seeder: {seeder})")
                return True
        except Exception as e:
            print(f"⚠️ Gagal import seeder {seeder} untuk {phone}: {e}")

    print(f"❌ Semua percobaan import kontak gagal untuk {phone}")
    log_failed(phone, "-", "contact_zero_unsolvable")
    return False


async def sell_one_session(
    api_id: int,
    api_hash: str,
    admin_client: TelegramClient,
    bot_username: str,
    session_path: Path,
    otp_timeout: int = 90,
    event_cb=None,
    grace_tasks: list | None = None,
    stop_event: asyncio.Event | None = None,
) -> bool:

    _proxy = get_next_proxy()
    selling_client = TelegramClient(StringSession(_make_string_session(session_path)), api_id, api_hash, proxy=_proxy, device_model="")
    user_id = None
    try:
        try:
            await asyncio.wait_for(selling_client.connect(), timeout=30)
        except asyncio.TimeoutError:
            print(f"❌ Connection timeout untuk {session_path.name}")
            log_failed(session_path.name, bot_username, "connect_timeout")
            try:
                await selling_client.disconnect()
            except Exception:
                pass
            move_to_unauth(session_path)
            return False
        if not await selling_client.is_user_authorized():
            print("❌ Session unauth, tidak bisa ambil nomor.")
            log_failed("-", bot_username, "unauthorized")
            try:
                await selling_client.disconnect()
            except Exception:
                pass
            move_to_unauth(session_path)
            return False

        # Anti-freeze warmup: jeda setelah connect sebelum API call pertama.
        # Session bisa saja baru dibuat di IP/device lain — langsung eksekusi
        # cepat dari server ini memicu deteksi Telegram dan mem-freeze akun.
        _warmup = random.uniform(WARMUP_DELAY_MIN, WARMUP_DELAY_MAX)
        print(f"⏳ Warmup [{session_path.name}]: jeda {_warmup:.1f}s sebelum mulai...")
        if await _wait_stop(stop_event, _warmup):
            print(f"🛑 Stop requested during warmup for {session_path.name}")
            raise StoppedError()

        me = await selling_client.get_me()
        user_id = getattr(me, "id", None)
        phone = f"+{me.phone}" if me and me.phone else None
        if not phone:
            print(f"❌ Gagal ambil nomor dari session: {session_path.name}")
            log_failed(session_path.name, bot_username, "no_phone")
            await selling_client.disconnect()
            return False

    except Exception as e:
        print(f"❌ Gagal open session jual: {e}")
        log_failed("-", bot_username, f"open_error: {e}")
        return False

    log_pending(phone, bot_username, "started")
    _check_stop(stop_event)

    admin_label = _get_admin_label(admin_client)
    print(f"✅ Session Admin dipakai: {admin_label}")
    print(f"\n🟡 Mulai jual: {phone} via {bot_username} (ID={user_id})")

    # 🔎 Contact restriction check + auto-solve (sebelum cek 2FA / devices)
    contacts_ok = await auto_solve_contact_restriction(selling_client, phone)
    if not contacts_ok:
        log_failed(phone, bot_username, "contact_restriction_zero")
        try:
            await selling_client.disconnect()
        except Exception:
            pass
        return False

    ok_2fa_single = await check_and_disable_2fa(selling_client, phone, session_path)
    if not ok_2fa_single:
        log_failed(phone, bot_username, "precheck_failed")
        return False

    try:
        async with admin_client.conversation(bot_username, timeout=180) as conv:
            await _conv_send_and_wait(conv, phone)
            print(f"📩 Nomor dikirim ke bot: {phone}")
            if event_cb:
                await event_cb("phone_sent", phone)
            print("⏳ Bot masih processing, tunggu update...")

            otp_future = asyncio.create_task(
                wait_for_otp_from_777000(selling_client, timeout=otp_timeout)
            )

            loop = asyncio.get_event_loop()
            otp_trigger: asyncio.Future = loop.create_future()

            start_sent = False
            cancel_sent = False

            @admin_client.on(events.NewMessage(chats=bot_username))
            @admin_client.on(events.MessageEdited(chats=bot_username))
            async def buyer_handler(event):
                nonlocal start_sent, cancel_sent
                raw = event.raw_text or ""
                txt = raw.lower()
                print(f"👀 [BUYER_HANDLER] {raw}")

                if (
                    "still processing your request" in txt
                    and "please try again later" in txt
                    and not otp_trigger.done()
                ):
                    print(
                        "⚠️ Buyer bilang still processing & suruh coba nanti (phase awal) → kirim /cancel & skip session ini."
                    )
                    if not cancel_sent:
                        cancel_sent = True
                        try:
                            await _send_with_delay(conv, "/cancel")
                            print(
                                "↩️ /cancel dikirim (still processing / try again later, phase awal)."
                            )
                        except Exception as e:
                            print(
                                f"⚠️ Gagal kirim /cancel (processing_later, phase awal): {e}"
                            )
                    otp_trigger.set_result("processing_later")
                    return

                if (
                    "capacity of id has been completed" in txt
                    and not otp_trigger.done()
                ):
                    print(
                        "⛔ Buyer balas capacity full untuk ID → stop seluruh batch, jangan lanjut kirim nomor lagi."
                    )
                    log_failed(phone, bot_username, "capacity_full")
                    otp_trigger.set_result("capacity_full")
                    return

                if "/start" in txt and not start_sent:
                    start_sent = True
                    print(
                        "↩️ Buyer minta /start (phase awal) → kirim /start dan tandai session ini gagal (lanjut nomor berikutnya)."
                    )
                    try:
                        await _send_with_delay(conv, "/start")
                    except Exception as e:
                        print(f"⚠️ Gagal kirim /start ke buyer (phase awal): {e}")
                    if not otp_trigger.done():
                        otp_trigger.set_result("restart")
                    return

                ask_code_en = (
                    "enter the code" in txt
                    or "enter the otp you received" in txt
                    or "enter the otp" in txt
                )
                ask_code_id = (
                    "silakan masukkan kode" in txt
                    or "silahkan masukkan kode" in txt
                    or "kode berhasil dikirim" in txt
                )

                if (ask_code_en or ask_code_id) and not otp_trigger.done():
                    otp_trigger.set_result("enter_code")
                    print(f"✏️ Pesan bot ter-update (minta OTP): {event.raw_text}")
                elif "already been sold" in txt and not otp_trigger.done():
                    otp_trigger.set_result("already_sold")
                    print(f"⏭ Nomor sudah pernah terjual: {event.raw_text}")
                elif _looks_error(raw) and not otp_trigger.done():
                    if "already registered" in txt:
                        otp_trigger.set_result("already_registered")
                        print(
                            f"♻️ Number already registered, skip & logout session: {event.raw_text}"
                        )
                    else:
                        otp_trigger.set_result("error")
                        print(f"⚠️ Bot buyer balas error (awal): {event.raw_text}")

            try:
                _check_stop(stop_event)
                trigger = await asyncio.wait_for(otp_trigger, timeout=120)
            except asyncio.TimeoutError:
                print("⚠️ Tidak ada update dari bot setelah Processing..")
                log_failed(phone, bot_username, "no_enter_code")
                await selling_client.disconnect()
                return False
            finally:
                admin_client.remove_event_handler(buyer_handler, events.NewMessage)
                admin_client.remove_event_handler(buyer_handler, events.MessageEdited)

            if trigger == "capacity_full":
                print(
                    f"⛔ Capacity buyer full terdeteksi saat proses {phone} → hentikan batch sekarang."
                )
                try:
                    await selling_client.disconnect()
                except Exception:
                    pass
                raise CapacityFullError("Buyer capacity for this country is full.")

            if trigger == "processing_later":
                print(
                    f"⚠️ Buyer still processing / try again later (phase awal) untuk {phone} → session di-skip."
                )
                log_failed(phone, bot_username, "processing_try_later_phase1")
                try:
                    await selling_client.disconnect()
                except Exception:
                    pass
                return False

            if trigger == "restart":
                print(
                    f"♻️ Buyer minta restart (/start) di phase awal. Session {phone} di-skip, lanjut ke nomor berikutnya."
                )
                log_failed(phone, bot_username, "restart_requested")
                try:
                    await selling_client.disconnect()
                except Exception:
                    pass
                return False

            if trigger == "already_registered":
                print(
                    f"♻️ Nomor {phone} sudah terdaftar di buyer, lanjut ke berikutnya."
                )
                log_success(phone, bot_username, "already_registered")
                # Auto logout device bot
                try:
                    await selling_client(LogOutRequest())
                    print(f"🔫 Auto logout (already_registered): {phone}")
                except Exception as e:
                    print(f"⚠️ Auto logout gagal {phone}: {e}")
                try:
                    await selling_client.disconnect()
                except Exception:
                    pass
                return True

            if trigger == "already_sold":
                print(f"⏭ Nomor {phone} sudah pernah terjual, simpan ke ALREADY_SOLD.")
                log_failed(phone, bot_username, "already_sold")
                move_to_already_sold(session_path)
                if event_cb:
                    await event_cb("already_sold", phone)
                try:
                    await selling_client.disconnect()
                except Exception:
                    pass
                return "already_sold"

            if trigger == "error":
                log_failed(phone, bot_username, "buyer_error")
                await selling_client.disconnect()
                return False

            print("👂 Bot minta OTP, ambil dari listener...")
            code = None
            if otp_future.done():
                code = otp_future.result()
                if code:
                    print(
                        f"⚡ OTP sudah ada sebelum buyer minta — langsung dipakai: {code}"
                    )
            if not code:
                _check_stop(stop_event)
                try:
                    code = await asyncio.wait_for(otp_future, timeout=30)
                except asyncio.TimeoutError:
                    pass
            if not code:
                code = await get_otp_from_history(selling_client)

            if not code:
                print("❌ OTP tidak ditemukan ➜ SKIP tanpa logout.")
                log_failed(phone, bot_username, "otp_not_found")
                await selling_client.disconnect()
                return False

            print(f"✅ OTP siap: {code} — kirim ke bot buyer.")

            result_future: asyncio.Future = loop.create_future()

            @admin_client.on(events.NewMessage(chats=bot_username))
            @admin_client.on(events.MessageEdited(chats=bot_username))
            async def result_handler(event):
                nonlocal start_sent, cancel_sent
                raw = event.raw_text or ""
                txt = raw.lower()
                print(f"👀 [RESULT_HANDLER] {raw}")

                if _looks_balance_notif(raw):
                    print(
                        "💰 / ℹ️ Notif balance / processed / multiple-active-info terdeteksi → di-skip (bukan flow akun ini)."
                    )
                    return

                if "/start" in txt and not start_sent:
                    start_sent = True
                    print(
                        "↩️ Buyer minta /start (result phase) → kirim /start & tandai session ini gagal (lanjut nomor berikutnya)."
                    )
                    try:
                        await _send_with_delay(conv, "/start")
                    except Exception as e:
                        print(f"⚠️ Gagal kirim /start ke buyer (result phase): {e}")
                    if not result_future.done():
                        result_future.set_result("restart")
                    return

                if "/cancel" in txt and not cancel_sent:
                    cancel_sent = True
                    print("↩️ Buyer minta /cancel → kirim /cancel & tandai session gagal.")
                    try:
                        await _send_with_delay(conv, "/cancel")
                    except Exception as e:
                        print(f"⚠️ Gagal kirim /cancel ke buyer: {e}")
                    if not result_future.done():
                        result_future.set_result("cancelled")
                    return

                if (
                    "multiple numbers waiting for otp" in txt
                    and not result_future.done()
                ):
                    print(
                        "⚠️ Detected multiple numbers waiting for OTP → kirim /cancel dan skip."
                    )
                    try:
                        await _send_with_delay(conv, "/cancel")
                        print("↩️ /cancel dikirim (multi-otp conflict).")
                    except Exception as e:
                        print(f"⚠️ Gagal kirim /cancel (multi-otp): {e}")
                    result_future.set_result("multi_otp")
                    return

                if (
                    "still processing your request" in txt
                    and "please try again later" in txt
                    and not result_future.done()
                ):
                    print(
                        "⚠️ Buyer bilang masih processing & suruh coba nanti → kirim /cancel & skip session ini."
                    )
                    try:
                        await _send_with_delay(conv, "/cancel")
                        print("↩️ /cancel dikirim (still processing / try again later).")
                    except Exception as e:
                        print(f"⚠️ Gagal kirim /cancel (processing_later): {e}")
                    result_future.set_result("processing_later")
                    return

                if _looks_frozen(raw) and not result_future.done():
                    print(
                        "🧊 Account frozen terdeteksi → tandai FROZEN & skip session ini."
                    )
                    result_future.set_result("frozen")
                    return

                if _looks_rejected(raw) and not result_future.done():
                    m_ph = NUMBER_IN_MSG_RE.search(raw)
                    if m_ph is None or _normalize_phone(m_ph.group(1)) == _normalize_phone(phone):
                        print("🚫 Account rejected terdeteksi (sebelum sukses).")
                        result_future.set_result("rejected")
                        return

                if result_future.done():
                    return
                if _looks_success(raw):
                    print("✅ result_handler detect SUCCESS")
                    result_future.set_result("success")
                elif _looks_error(raw):
                    print("❌ result_handler detect ERROR")
                    result_future.set_result("error")

            try:
                await _send_with_delay(conv, code)
                if event_cb:
                    await event_cb("otp_sent", phone, code)
                print("📩 OTP dikirim, tunggu respon dari bot (edit / new)...")

                try:
                    _check_stop(stop_event)
                    outcome = await asyncio.wait_for(result_future, timeout=180)
                except asyncio.TimeoutError:
                    print("⚠️ Tidak ada update dari bot setelah OTP → unknown_state")
                    log_failed(phone, bot_username, "unknown_state")
                    await selling_client.disconnect()
                    return False

                if outcome == "success":
                    print(f"🎉 Sukses jual (via handler): {phone}")
                    log_success(phone, bot_username, "sold")

                    # JANGAN logout dulu — grace period monitor di background
                    # selling_client tetap hidup selama 40s, lalu logout/recover
                    _gt = asyncio.create_task(_grace_period_and_finalize(
                        selling_client, admin_client, bot_username,
                        phone, session_path, event_cb,
                    ))
                    if grace_tasks is not None:
                        grace_tasks.append(_gt)
                    return True

                elif outcome == "rejected":
                    print(f"🚫 Account {phone} ditolak buyer (contact restriction), recovering...")
                    log_failed(phone, bot_username, "rejected_by_buyer")
                    if event_cb:
                        await event_cb("rejected", phone)
                    recovered = await _recover_rejected_session(selling_client, phone, session_path)
                    if not recovered:
                        if event_cb:
                            await event_cb("recover_failed", phone)
                        return False
                    return "recovered"

                elif outcome == "frozen":
                    print(
                        f"🧊 Session {phone} ditandai FROZEN, disconnect & pindah ke UNAUTH."
                    )
                    log_failed(phone, bot_username, "frozen")
                    try:
                        await selling_client.disconnect()
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)
                    move_to_unauth(session_path)
                    return False

                elif outcome == "multi_otp":
                    print(f"⚠️ Multi-OTP conflict untuk {phone}, session di-skip.")
                    log_failed(phone, bot_username, "multi_otp_conflict")
                    try:
                        await selling_client.disconnect()
                    except Exception:
                        pass
                    return False

                elif outcome == "cancelled":
                    print(
                        f"⚠️ Session {phone} dibatalkan sesuai instruksi buyer (/cancel)."
                    )
                    log_failed(phone, bot_username, "cancel_requested")
                    try:
                        await selling_client.disconnect()
                    except Exception:
                        pass
                    return False

                elif outcome == "processing_later":
                    print(
                        f"⚠️ Buyer bilang still processing / try again later untuk {phone} → sudah di-cancel, lanjut nomor berikutnya."
                    )
                    log_failed(phone, bot_username, "processing_try_later")
                    try:
                        await selling_client.disconnect()
                    except Exception:
                        pass
                    return False

                elif outcome == "restart":
                    print(
                        f"♻️ Buyer minta restart (/start) setelah OTP. Session {phone} di-skip, lanjut berikutnya."
                    )
                    log_failed(phone, bot_username, "restart_requested_after_otp")
                    try:
                        await selling_client.disconnect()
                    except Exception:
                        pass
                    return False

                else:
                    print("❌ Bot buyer error setelah OTP (via handler).")
                    log_failed(phone, bot_username, "buyer_error_after_otp")
                    await selling_client.disconnect()
                    return False

            except Exception as e:
                print(f"❌ Error setelah OTP: {e}")
                log_failed(phone, bot_username, f"otp_stage_error: {e}")
                await selling_client.disconnect()
                return False
            finally:
                try:
                    admin_client.remove_event_handler(result_handler, events.NewMessage)
                except Exception as e:
                    print(f"⚠️ Gagal remove result_handler NewMessage: {e}")
                try:
                    admin_client.remove_event_handler(
                        result_handler, events.MessageEdited
                    )
                except Exception as e:
                    print(f"⚠️ Gagal remove result_handler MessageEdited: {e}")

    except RPCError as e:
        msg = str(e)
        print(f"❌ RPCError conv bot: {msg}")

        m = re.search(r"a wait of (\d+) seconds is required", msg, re.I)
        if m:
            base_wait = int(m.group(1))
            extra = random.randint(20, 120)
            total = base_wait + extra
            print(
                f"⏳ FloodWait terdeteksi: {base_wait}s + extra {extra}s = {total}s. "
                f"Pause sebelum retry untuk {phone}"
            )
            try:
                await selling_client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(total)
            print(f"🔁 Retry jual: {phone} setelah floodwait selesai.")
            return await sell_one_session(
                api_id=api_id,
                api_hash=api_hash,
                admin_client=admin_client,
                bot_username=bot_username,
                session_path=session_path,
                otp_timeout=otp_timeout,
            )

        log_failed(phone, bot_username, f"rpc_error: {msg}")
        try:
            await selling_client.disconnect()
        except Exception:
            pass
        return False

    except CapacityFullError:
        # propagate ke batch
        raise

    except StoppedError:
        print(f"🛑 sell_one_session stopped for {session_path.name}")
        try:
            await selling_client.disconnect()
        except Exception:
            pass
        raise  # re-raise so caller knows it was stopped
    except Exception as e:
        print(f"❌ Error conv bot: {e}")
        log_failed(phone, bot_username, f"conv_error: {e}")
        try:
            await selling_client.disconnect()
        except Exception:
            pass
        return False


def _make_string_session(session_path: Path) -> str:
    """Load .session file → return StringSession string (in-memory, no file writes).
    Always set port=443 for proxy compatibility."""
    import sqlite3, os
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
        ss._port = 443  # Force 443 for proxy compatibility
        ss._auth_key = AuthKey(auth_key[:256])
        return ss.save()
    finally:
        db.close()


def _normalize_phone(s: str) -> str:
    """Ambil cuma digit, buang + dan karakter lain buat perbandingan."""
    return re.sub(r"\D", "", s or "")


async def _grace_period_and_finalize(
    selling_client: TelegramClient,
    admin_client: TelegramClient,
    bot_username: str,
    phone: str,
    session_path: Path,
    event_cb=None,
    grace_seconds: int = GRACE_PERIOD_SECONDS,
):
    """Grace period + auto-logout/recover, jalan di background.
    selling_client TETAP HIDUP selama grace period.
    - Kalau 40s tanpa reject → logout + disconnect
    - Kalau ada reject → kill device buyer + simpan ke RECOVERED + disconnect
    """
    norm_phone = _normalize_phone(phone)
    loop = asyncio.get_event_loop()
    reject_future: asyncio.Future = loop.create_future()

    @admin_client.on(events.NewMessage(chats=bot_username))
    @admin_client.on(events.MessageEdited(chats=bot_username))
    async def grace_watcher(event):
        raw = event.raw_text or ""
        if not _looks_rejected(raw):
            return
        m = NUMBER_IN_MSG_RE.search(raw)
        if not m:
            return
        if _normalize_phone(m.group(1)) == norm_phone:
            if not reject_future.done():
                print(f"🚫 Late REJECT terdeteksi saat grace period: {phone}")
                reject_future.set_result(True)

    print(f"⏳ Grace period {grace_seconds}s (monitor) untuk {phone}...")
    try:
        late_rejected = await asyncio.wait_for(reject_future, timeout=grace_seconds)
    except asyncio.TimeoutError:
        late_rejected = False
    finally:
        try:
            admin_client.remove_event_handler(grace_watcher, events.NewMessage)
        except Exception:
            pass
        try:
            admin_client.remove_event_handler(grace_watcher, events.MessageEdited)
        except Exception:
            pass

    if late_rejected:
        # Late reject → JANGAN logout, kill device buyer, simpan ke RECOVERED
        print(f"🔄 Late reject dikonfirmasi untuk {phone}, recovering...")
        recovered = await _recover_rejected_session(selling_client, phone, session_path)
        if recovered:
            log_failed(phone, bot_username, "late_rejected_recovered_bg")
            if event_cb:
                await event_cb("recovered", phone)
        else:
            log_failed(phone, bot_username, "late_rejected_recover_failed")
            if event_cb:
                await event_cb("recover_failed", phone)
    else:
        # Tidak ada reject → aman logout
        try:
            await selling_client(LogOutRequest())
            print(f"🔫 Auto logout (grace selesai): {phone}")
        except Exception as e:
            print(f"⚠️ Auto logout gagal {phone}: {e}")

    try:
        await selling_client.disconnect()
    except Exception:
        pass


async def _grace_period_watch(
    selling_client: TelegramClient,
    admin_client: TelegramClient,
    bot_username: str,
    phone: str,
    session_path: Path,
    grace_seconds: int = GRACE_PERIOD_SECONDS,
) -> bool:
    """
    Setelah buyer bilang 'Successfully', tunggu grace period sebelum logout.
    Monitor pesan buyer — kalau reject masuk dalam grace period, recover session.

    Returns:
        True  → late reject terdeteksi, session berhasil di-recover ke RECOVERED/
        False → grace period habis tanpa reject, aman untuk logout
    """
    norm_phone = _normalize_phone(phone)
    loop = asyncio.get_event_loop()
    reject_future: asyncio.Future = loop.create_future()

    @admin_client.on(events.NewMessage(chats=bot_username))
    @admin_client.on(events.MessageEdited(chats=bot_username))
    async def grace_reject_watcher(event):
        raw = event.raw_text or ""
        if not _looks_rejected(raw):
            return
        m = NUMBER_IN_MSG_RE.search(raw)
        if not m:
            return
        if _normalize_phone(m.group(1)) == norm_phone:
            if not reject_future.done():
                print(f"🚫 Late REJECT terdeteksi saat grace period: {phone}")
                reject_future.set_result(True)

    print(f"⏳ Grace period {grace_seconds}s untuk {phone}...")
    try:
        late_rejected = await asyncio.wait_for(reject_future, timeout=grace_seconds)
    except asyncio.TimeoutError:
        late_rejected = False
    finally:
        try:
            admin_client.remove_event_handler(grace_reject_watcher, events.NewMessage)
        except Exception:
            pass
        try:
            admin_client.remove_event_handler(grace_reject_watcher, events.MessageEdited)
        except Exception:
            pass

    if late_rejected:
        print(f"🔄 Late reject dikonfirmasi untuk {phone}, recovering...")
        recovered = await _recover_rejected_session(selling_client, phone, session_path)
        return recovered
    else:
        print(f"✅ Grace period selesai, tidak ada reject untuk {phone} → aman logout.")
        return False



async def _recover_rejected_session(
    selling_client: TelegramClient,
    phone: str,
    session_path: Path,
) -> bool:
    """
    Recover session yang di-reject buyer:
    1. Delay 10s (tunggu proses rejection di sisi buyer selesai)
    2. terminate_other_devices() → kill session buyer
    3. Verifikasi session kita masih valid (get_me())
    4. Disconnect selling_client
    5. Simpan session ke RECOVERED/ (valid) atau REJECTED/ (unauth)

    Returns:
        True  → berhasil recover (session masih valid)
        False → gagal recover (session unauth → REJECTED/)
    """
    print(f"⏳ [{phone}] Delay 10s sebelum kill device buyer (tunggu buyer selesai proses rejection)...")
    await asyncio.sleep(10)

    try:
        result = await terminate_other_devices(selling_client, phone)
        if result == "error":
            print(f"❌ [{phone}] Gagal kill device buyer, session tidak bisa di-recover.")
            try:
                await selling_client.disconnect()
            except Exception:
                pass
            move_to_rejected(session_path)
            return False

        print(f"✅ [{phone}] Device buyer berhasil di-kill. Verifikasi session...")

        # ── Verifikasi session masih valid setelah kill device buyer ──
        try:
            me = await asyncio.wait_for(selling_client.get_me(), timeout=15)
            if me is None:
                print(f"❌ [{phone}] get_me() return None → session unauth.")
                try:
                    await selling_client.disconnect()
                except Exception:
                    pass
                move_to_unauth(session_path)
                return False
            print(f"✅ [{phone}] Session VALID — user_id={me.id}, phone=+{me.phone}")
        except asyncio.TimeoutError:
            print(f"⚠️ [{phone}] get_me() timeout — session kemungkinan unauth.")
            try:
                await selling_client.disconnect()
            except Exception:
                pass
            move_to_unauth(session_path)
            return False
        except Exception as e:
            err = str(e).lower()
            if "unauthorized" in err or "auth" in err:
                print(f"❌ [{phone}] Session UNAUTH setelah kill device buyer: {e}")
                try:
                    await selling_client.disconnect()
                except Exception:
                    pass
                move_to_unauth(session_path)
                return False
            # Error lain (network etc) — tetap anggap recovered, biar bisa dicek manual
            print(f"⚠️ [{phone}] get_me() error (bukan unauth): {e} — tetap simpan ke RECOVERED.")

        try:
            await selling_client.disconnect()
        except Exception:
            pass
        move_to_recovered(session_path)
        return True

    except Exception as e:
        print(f"❌ [{phone}] Error saat recover: {e}")
        try:
            await selling_client.disconnect()
        except Exception:
            pass
        move_to_rejected(session_path)
        return False


async def sell_one_session_reply_mode(
    api_id: int,
    api_hash: str,
    admin_client: TelegramClient,
    bot_username: str,
    session_path: Path,
    otp_timeout: int = 90,
    event_cb=None,
    grace_tasks: list | None = None,
    stop_event: asyncio.Event | None = None,
) -> bool:
    """
    Mode buyer yg formatnya:
    1. Kirim nomor (bisa banyak)
    2. Buyer balas: '📨 OTP to +62xxxx ... Reply to this message'
    3. Kita BALAS (reply) ke pesan itu dengan OTP
    4. Buyer balas:
       '✅ Successfully\n📱 Number: +62...\n...'
    """

    _proxy = get_next_proxy()
    selling_client = TelegramClient(StringSession(_make_string_session(session_path)), api_id, api_hash, proxy=_proxy, device_model="")
    user_id = None
    try:
        # connect & auth
        try:
            await asyncio.wait_for(selling_client.connect(), timeout=30)
        except asyncio.TimeoutError:
            print(f"❌ [REPLY] Connection timeout untuk {session_path.name}")
            log_failed(session_path.name, bot_username, "connect_timeout_reply")
            try:
                await selling_client.disconnect()
            except Exception:
                pass
            move_to_unauth(session_path)
            return False
        if not await selling_client.is_user_authorized():
            print("❌ Session unauth, tidak bisa ambil nomor (reply-mode).")
            log_failed("-", bot_username, "unauthorized_reply_mode")
            try:
                await selling_client.disconnect()
            except Exception:
                pass
            move_to_unauth(session_path)
            return False

        # Anti-freeze warmup: sama seperti receiver mode — jeda setelah connect
        # sebelum API call pertama untuk menghindari freeze akibat rapid-action.
        _warmup = random.uniform(WARMUP_DELAY_MIN, WARMUP_DELAY_MAX)
        print(f"⏳ [REPLY] Warmup [{session_path.name}]: jeda {_warmup:.1f}s sebelum mulai...")
        if await _wait_stop(stop_event, _warmup):
            print(f"🛑 Stop requested during warmup for {session_path.name}")
            raise StoppedError()

        me = await selling_client.get_me()
        user_id = getattr(me, "id", None)
        phone = f"+{me.phone}" if me and me.phone else None
        if not phone:
            print(f"❌ Gagal ambil nomor dari session (reply-mode): {session_path.name}")
            log_failed(session_path.name, bot_username, "no_phone_reply_mode")
            await selling_client.disconnect()
            return False

    except Exception as e:
        print(f"❌ Gagal open session jual (reply-mode): {e}")
        log_failed("-", bot_username, f"open_error_reply: {e}")
        return False

    norm_phone = _normalize_phone(phone)
    log_pending(phone, bot_username, "started_reply_mode")
    _check_stop(stop_event)

    admin_label = _get_admin_label(admin_client)
    print(f"✅ Session Admin dipakai (reply-mode): {admin_label}")
    print(f"\n🟡 [REPLY] Mulai jual: {phone} via {bot_username} (ID={user_id})")

    # 🔎 Contact restriction check + auto-solve (sebelum cek 2FA / devices)
    contacts_ok = await auto_solve_contact_restriction(selling_client, phone)
    if not contacts_ok:
        log_failed(phone, bot_username, "contact_restriction_zero_reply")
        try:
            await selling_client.disconnect()
        except Exception:
            pass
        return False

    # precheck 2FA + single-device
    ok_2fa_single = await check_and_disable_2fa(selling_client, phone, session_path)
    if not ok_2fa_single:
        log_failed(phone, bot_username, "precheck_failed_reply")
        return False

    loop = asyncio.get_event_loop()
    otp_prompt_future: asyncio.Future = loop.create_future()
    result_future: asyncio.Future = loop.create_future()

    # handler buat OTP prompt + hasil
    @admin_client.on(events.NewMessage(chats=bot_username))
    @admin_client.on(events.MessageEdited(chats=bot_username))
    async def reply_mode_handler(event):
        raw = event.raw_text or ""
        txt = raw.lower()
        print(f"👀 [REPLY_HANDLER] {raw}")

        # 🔹 Step 1: detect OTP prompt untuk nomor ini
        if not otp_prompt_future.done():
            m = OTP_TO_RE.search(raw)
            if m:
                target_num = _normalize_phone(m.group(1))
                if target_num == norm_phone:
                    print(
                        f"📨 [REPLY_MODE] OTP prompt ketangkep untuk {phone} (msg_id={event.id})"
                    )
                    otp_prompt_future.set_result(event.message)
                    return

        # 🔹 Step 2: detect hasil sukses / error untuk nomor ini
        if result_future.done():
            return

        m2 = NUMBER_IN_MSG_RE.search(raw)
        if m2:
            target_num = _normalize_phone(m2.group(1))
            if target_num == norm_phone:
                # capacity full check
                if "capacity" in txt.lower() and "full" in txt.lower():
                    print(f"⛔ [REPLY_MODE] Capacity full detected")
                    if not result_future.done():
                        result_future.set_result("capacity_full")
                    return
                # already sold check
                if "already been sold" in txt:
                    print(f"⏭ [REPLY_MODE] Already sold detected untuk {phone}")
                    result_future.set_result("already_sold")
                    return
                # rejection check sebelum success
                if _looks_rejected(raw):
                    print(f"🚫 [REPLY_MODE] Rejection detected untuk {phone}")
                    result_future.set_result("rejected")
                    return
                # sukses
                if "success" in txt or "✅" in raw:
                    print(f"✅ [REPLY_MODE] Success detected untuk {phone}")
                    result_future.set_result("success")
                    return
                # error generic
                if _looks_error(raw):
                    print(f"❌ [REPLY_MODE] Error detected untuk {phone}")
                    result_future.set_result("error")
                    return

    try:
        # 1) kirim nomor ke buyer (no conversation)
        delay = random.uniform(0.5, 1.5)
        print(f"⏳ [REPLY] Delay {delay:.1f}s sebelum kirim nomor: {phone}")
        await asyncio.sleep(delay)
        await admin_client.send_message(bot_username, phone)
        print(f"📩 [REPLY] Nomor dikirim ke bot: {phone}")
        if event_cb:
            await event_cb("phone_sent", phone)

        # 2) start OTP listener
        otp_future = asyncio.create_task(
            wait_for_otp_from_777000(selling_client, timeout=otp_timeout)
        )

        # 3) tunggu OTP prompt '📨 OTP to +62... Reply to this message'
        try:
            _check_stop(stop_event)
            otp_prompt_msg = await asyncio.wait_for(otp_prompt_future, timeout=120)
        except asyncio.TimeoutError:
            print(
                f"⚠️ [REPLY] Tidak dapat OTP prompt untuk {phone} → skip session ini."
            )
            log_failed(phone, bot_username, "no_otp_prompt_reply")
            await selling_client.disconnect()
            return False

        # 4) ambil OTP dari listener / history
        print("👂 [REPLY] Bot sudah minta OTP, ambil dari listener...")
        code = None
        if otp_future.done():
            code = otp_future.result()
            if code:
                print(
                    f"⚡ [REPLY] OTP sudah ada sebelum diminta — langsung dipakai: {code}"
                )
        if not code:
            _check_stop(stop_event)
            try:
                code = await asyncio.wait_for(otp_future, timeout=30)
            except asyncio.TimeoutError:
                pass
        if not code:
            code = await get_otp_from_history(selling_client)

        if not code:
            print("❌ [REPLY] OTP tidak ditemukan ➜ SKIP tanpa logout.")
            log_failed(phone, bot_username, "otp_not_found_reply")
            await selling_client.disconnect()
            return False

        print(f"✅ [REPLY] OTP siap: {code} — akan di-reply ke msg_id={otp_prompt_msg.id}")

        # 5) reply OTP ke pesan prompt
        delay2 = random.uniform(1.0, 5.0)
        print(f"⏳ [REPLY] Delay {delay2:.1f}s sebelum reply OTP {code} ke {phone}")
        await asyncio.sleep(delay2)
        await admin_client.send_message(
            bot_username,
            code,
            reply_to=otp_prompt_msg.id,  # ini penting: reply ke prompt yg bener
        )
        if event_cb:
            await event_cb("otp_sent", phone, code)
        print("📩 [REPLY] OTP dikirim (reply), tunggu hasil...")

        # 6) tunggu hasil sukses / error
        try:
            _check_stop(stop_event)
            outcome = await asyncio.wait_for(result_future, timeout=180)
        except asyncio.TimeoutError:
            print("⚠️ [REPLY] Tidak ada update hasil setelah OTP → unknown_state")
            log_failed(phone, bot_username, "unknown_state_reply")
            await selling_client.disconnect()
            return False

        if outcome == "success":
            print(f"🎉 [REPLY] Sukses jual (reply-mode): {phone}")
            log_success(phone, bot_username, "sold_reply_mode")

            # JANGAN logout dulu — grace period monitor di background
            _gt = asyncio.create_task(_grace_period_and_finalize(
                selling_client, admin_client, bot_username,
                phone, session_path, event_cb,
            ))
            if grace_tasks is not None:
                grace_tasks.append(_gt)
            return True

        elif outcome == "rejected":
            print(f"🚫 [REPLY] Account {phone} ditolak buyer, recovering...")
            log_failed(phone, bot_username, "rejected_by_buyer_reply")
            if event_cb:
                await event_cb("rejected", phone)
            recovered = await _recover_rejected_session(selling_client, phone, session_path)
            if not recovered:
                if event_cb:
                    await event_cb("recover_failed", phone)
                return False
            return "recovered"

        elif outcome == "capacity_full":
            print(f"⛔ [REPLY] Buyer capacity FULL untuk {bot_username}.")
            log_failed(phone, bot_username, "capacity_full_reply")
            try:
                await selling_client.disconnect()
            except Exception:
                pass
            raise CapacityFullError("Buyer capacity for this country is full.")

        elif outcome == "already_sold":
            print(f"⏭ [REPLY] Nomor {phone} sudah pernah terjual, simpan ke ALREADY_SOLD.")
            log_failed(phone, bot_username, "already_sold_reply")
            move_to_already_sold(session_path)
            if event_cb:
                await event_cb("already_sold", phone)
            await selling_client.disconnect()
            return "already_sold"

        else:
            print(f"❌ [REPLY] Buyer error setelah OTP (reply-mode) untuk {phone}")
            log_failed(phone, bot_username, "buyer_error_after_otp_reply")
            await selling_client.disconnect()
            return False

    finally:
        # bersihin handler reply-mode biar ga numpuk
        try:
            admin_client.remove_event_handler(reply_mode_handler, events.NewMessage)
        except Exception as e:
            print(f"⚠️ Gagal remove reply_mode_handler NewMessage: {e}")
        try:
            admin_client.remove_event_handler(
                reply_mode_handler,
                events.MessageEdited,
            )
        except Exception as e:
            print(f"⚠️ Gagal remove reply_mode_handler MessageEdited: {e}")


def _zip_recovered_sessions() -> Path | None:
    """Zip semua session di RECOVERED/ dan return path zip-nya. Hapus file setelah zip."""
    recovered_files = list(RECOVERED_DIR.rglob("*.session"))
    if not recovered_files:
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = RECOVERED_DIR.parent / f"RECOVERED_{timestamp}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in recovered_files:
            zf.write(f, f.name)
    print(f"📦 Zipped {len(recovered_files)} session ke {zip_path.name}")
    # Bersihkan file session setelah zip
    for f in recovered_files:
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
    # Bersihin subfolder kosong
    for sub in sorted(RECOVERED_DIR.rglob("*"), reverse=True):
        if sub.is_dir():
            try:
                sub.rmdir()  # hanya hapus kalau kosong
            except Exception:
                pass
    return zip_path


def _zip_already_sold_sessions() -> Path | None:
    """Zip semua session di ALREADY_SOLD/ dan return path zip-nya. Hapus file setelah zip."""
    already_sold_files = list(ALREADY_SOLD_DIR.rglob("*.session"))
    if not already_sold_files:
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = ALREADY_SOLD_DIR.parent / f"ALREADY_SOLD_{timestamp}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in already_sold_files:
            zf.write(f, f.name)
    print(f"📦 Zipped {len(already_sold_files)} session ke {zip_path.name}")
    # Bersihkan file session setelah zip
    for f in already_sold_files:
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
    # Bersihin subfolder kosong
    for sub in sorted(ALREADY_SOLD_DIR.rglob("*"), reverse=True):
        if sub.is_dir():
            try:
                sub.rmdir()  # hanya hapus kalau kosong
            except Exception:
                pass
    return zip_path


def _zip_cancelled_sessions() -> Path | None:
    """Zip semua session di CANCELLED/ dan return path zip-nya. Hapus file setelah zip."""
    cancelled_files = list(CANCELLED_DIR.rglob("*.session"))
    if not cancelled_files:
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = CANCELLED_DIR.parent / f"CANCELLED_{timestamp}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in cancelled_files:
            zf.write(f, f.name)
    print(f"📦 Zipped {len(cancelled_files)} session ke {zip_path.name}")
    for f in cancelled_files:
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
    for sub in sorted(CANCELLED_DIR.rglob("*"), reverse=True):
        if sub.is_dir():
            try:
                sub.rmdir()
            except Exception:
                pass
    return zip_path


async def sell_sessions_with_bot(
    api_id: int,
    api_hash: str,
    admin_client: TelegramClient,
    bot_username: str,
    session_files: list[Path],
    progress_cb=None,
    event_cb=None,
    stop_event: asyncio.Event | None = None,
):
    ok, fail = 0, 0
    total = len(session_files)
    recovered_count = 0
    already_sold_count = 0
    cancelled_count = 0
    grace_tasks: list[asyncio.Task] = []
    success_paths: list[Path] = []  # Jangan hapus dulu, tunggu grace period selesai

    try:
        for sp in session_files:
            # Stop check
            if stop_event and stop_event.is_set():
                print(f"🛑 Stop requested. Moving remaining sessions to CANCELLED.")
                for remaining_sp in session_files[session_files.index(sp):]:
                    move_to_cancelled(remaining_sp)
                    cancelled_count += 1
                break

            phone_fallback = parse_phone_from_filename(sp.name) or sp.stem
            _resolved = [None]
            if event_cb:
                async def _tracked(event_type, phone, extra="", _r=_resolved):
                    if event_type == "phone_sent":
                        _r[0] = phone
                    await event_cb(event_type, phone, extra)
                _cb = _tracked
            else:
                _cb = None

            try:
                res = await sell_one_session(
                    api_id=api_id,
                    api_hash=api_hash,
                    admin_client=admin_client,
                    bot_username=bot_username,
                    session_path=sp,
                    otp_timeout=90,
                    event_cb=_cb,
                    grace_tasks=grace_tasks,
                    stop_event=stop_event,
                )
            except StoppedError:
                print(f"🛑 Stopped during session {sp.name}, cancelling remaining.")
                for remaining_sp in session_files[session_files.index(sp):]:
                    move_to_cancelled(remaining_sp)
                    cancelled_count += 1
                break
            except CapacityFullError:
                print(
                    f"\n⛔ Buyer capacity FULL untuk {bot_username}. "
                    f"Stop proses batch. Summary sementara → OK: {ok} | ❌ Fail: {fail}"
                )
                break

            actual_phone = _resolved[0] or phone_fallback
            if res is True:
                ok += 1
                if event_cb:
                    await event_cb("success", actual_phone)
                # JANGAN hapus dulu — tunggu grace period selesai
                # Kalau late reject masuk, file perlu di-recover
                success_paths.append(sp)
            elif res == "recovered":
                recovered_count += 1
                if event_cb:
                    await event_cb("recovered", actual_phone)
            elif res == "already_sold":
                already_sold_count += 1
                if event_cb:
                    await event_cb("already_sold", actual_phone)
            else:
                fail += 1
                if event_cb:
                    await event_cb("fail", actual_phone)
            if progress_cb:
                await progress_cb(ok + recovered_count, fail, total, sp.name)

        if event_cb:
            await event_cb("batch_done", "")

    except Exception as e:
        print(f"❌ Error batch: {e}")

    # ── Tunggu semua grace period selesai SEBELUM disconnect ──
    if grace_tasks:
        print(f"⏳ Menunggu {len(grace_tasks)} grace period task selesai...")
        await asyncio.gather(*grace_tasks, return_exceptions=True)
        print("✅ Semua grace period task selesai.")

    # Hapus session file yang berhasil terjual DAN tidak di-recover saat grace period
    for sp in success_paths:
        if sp.exists():
            try:
                sp.unlink(missing_ok=True)
            except Exception:
                pass

    # Recount recovered dari filesystem (grace period mungkin menambah file)
    if RECOVERED_DIR.exists():
        actual_recovered = len(list(RECOVERED_DIR.rglob("*.session")))
        late_rejected = actual_recovered - recovered_count
        if late_rejected > 0:
            ok -= late_rejected
            recovered_count = actual_recovered
            print(f"🔄 Late rejection: {late_rejected} session berhasil di-recover saat grace period")

    print(f"\n✅ Selesai. Terjual: {ok} | 🔄 Recovered: {recovered_count} | ❌ Fail: {fail}")

    try:
        await admin_client.disconnect()
        print("🔌 Admin client disconnected setelah semua proses selesai.")
    except Exception as e:
        print(f"⚠️ Gagal disconnect admin client: {e}")

    return {"success": ok, "recovered": recovered_count, "already_sold": already_sold_count, "cancelled": cancelled_count, "fail": fail}


async def sell_sessions_with_reply_bot(
    api_id: int,
    api_hash: str,
    admin_client: TelegramClient,
    bot_username: str,
    session_files: list[Path],
    max_parallel: int = 10,
    progress_cb=None,
    event_cb=None,
    stop_event: asyncio.Event | None = None,
):
    """
    Batch jual pakai buyer reply-mode.
    - max_parallel: berapa session yg dikerjain bareng (dari config.py mis. 10)
    """
    ok, fail, recovered_count, already_sold_count, cancelled_count = 0, 0, 0, 0, 0
    total = len(session_files)
    sem = asyncio.Semaphore(max_parallel)
    grace_tasks: list[asyncio.Task] = []
    success_paths: list[Path] = []  # Jangan hapus dulu, tunggu grace period selesai

    capacity_full = False

    try:
        async def worker(sp: Path):
            nonlocal ok, fail, recovered_count, capacity_full
            phone_fallback = parse_phone_from_filename(sp.name) or sp.stem
            _resolved = [None]
            if event_cb:
                async def _tracked(event_type, phone, extra="", _r=_resolved):
                    if event_type == "phone_sent":
                        _r[0] = phone
                    await event_cb(event_type, phone, extra)
                _cb = _tracked
            else:
                _cb = None

            async with sem:
                if capacity_full:
                    return  # Skip jika capacity full
                try:
                    res = await sell_one_session_reply_mode(
                    api_id=api_id,
                    api_hash=api_hash,
                    admin_client=admin_client,
                    bot_username=bot_username,
                    session_path=sp,
                    otp_timeout=90,
                    event_cb=_cb,
                    grace_tasks=grace_tasks,
                    stop_event=stop_event,
                )
                except StoppedError:
                    print(f"🛑 [REPLY] Stopped during session {sp.name}")
                    move_to_cancelled(sp)
                    return
                except CapacityFullError:
                    print(f"⛔ [REPLY] Capacity full! Stop batch.")
                    capacity_full = True
                    move_to_cancelled(sp)
                    return

                actual_phone = _resolved[0] or phone_fallback
                if res is True:
                    ok += 1
                    if event_cb:
                        await event_cb("success", actual_phone)
                    # JANGAN hapus dulu — tunggu grace period selesai
                    success_paths.append(sp)
                elif res == "recovered":
                    recovered_count += 1
                    if event_cb:
                        await event_cb("recovered", actual_phone)
                elif res == "already_sold":
                    already_sold_count += 1
                    if event_cb:
                        await event_cb("already_sold", actual_phone)
                else:
                    fail += 1
                    if event_cb:
                        await event_cb("fail", actual_phone)
                if progress_cb:
                    await progress_cb(ok + recovered_count, fail, total, sp.name)

        # Stagger: launch task satu per satu dengan jeda
        tasks = []
        for i, sp in enumerate(session_files):
            # Capacity full check
            if capacity_full:
                print(f"⛔ [REPLY] Capacity full, cancelling {len(session_files) - i} remaining sessions.")
                for remaining_sp in session_files[i:]:
                    move_to_cancelled(remaining_sp)
                    cancelled_count += 1
                break

            # Stop check
            if stop_event and stop_event.is_set():
                print(f"🛑 [REPLY] Stop requested. Cancelling remaining sessions.")
                for remaining_sp in session_files[i:]:
                    move_to_cancelled(remaining_sp)
                    cancelled_count += 1
                break

            tasks.append(asyncio.create_task(worker(sp)))
            if i < len(session_files) - 1:
                stagger = random.uniform(TASK_STAGGER_MIN, TASK_STAGGER_MAX)
                print(f"⏳ Stagger: jeda {stagger:.1f}s sebelum launch task berikutnya...")
                if await _wait_stop(stop_event, stagger):
                    print(f"🛑 [REPLY] Stop requested during stagger.")
                    for remaining_sp in session_files[i+1:]:
                        move_to_cancelled(remaining_sp)
                        cancelled_count += 1
                    break

        # Wait for launched tasks
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if event_cb:
            await event_cb("batch_done", "")

    except Exception as e:
        print(f"❌ Error batch reply: {e}")

    # ── Tunggu semua grace period selesai SEBELUM disconnect ──
    if grace_tasks:
        print(f"⏳ [REPLY] Menunggu {len(grace_tasks)} grace period task selesai...")
        await asyncio.gather(*grace_tasks, return_exceptions=True)
        print("✅ [REPLY] Semua grace period task selesai.")

    # Hapus session file yang berhasil terjual DAN tidak di-recover saat grace period
    for sp in success_paths:
        if sp.exists():
            try:
                sp.unlink(missing_ok=True)
            except Exception:
                pass

    # Recount recovered dari filesystem (grace period mungkin menambah file)
    if RECOVERED_DIR.exists():
        actual_recovered = len(list(RECOVERED_DIR.rglob("*.session")))
        late_rejected = actual_recovered - recovered_count
        if late_rejected > 0:
            ok -= late_rejected
            recovered_count = actual_recovered
            print(f"🔄 [REPLY] Late rejection: {late_rejected} session berhasil di-recover saat grace period")

    print(f"\n✅ [REPLY] Batch selesai. Terjual: {ok} | 🔄 Recovered: {recovered_count} | ❌ Fail: {fail}")

    try:
        await admin_client.disconnect()
        print("🔌 [REPLY] Admin client disconnected setelah semua proses selesai.")
    except Exception as e:
        print(f"⚠️ [REPLY] Gagal disconnect admin client: {e}")

    return {"success": ok, "recovered": recovered_count, "already_sold": already_sold_count, "cancelled": cancelled_count, "fail": fail}
