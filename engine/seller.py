# engine/seller.py
# Flow jual akun dengan bot buyer menggunakan admin_client

import asyncio
import random  # buat delay tambahan floodwait & kirim pesan
import re  # buat parse detik
from pathlib import Path
from telethon import TelegramClient, events
from telethon.errors import RPCError, FreshResetAuthorisationForbiddenError
from telethon.tl.functions.account import (
    GetPasswordRequest,
    GetAuthorizationsRequest,
    ResetAuthorizationRequest,
)
from telethon.tl.functions.contacts import GetContactsRequest, ImportContactsRequest
from telethon.tl.types import InputPhoneContact

from engine.otp_listener import wait_for_otp_from_777000, get_otp_from_history
from utils.file_manager import (
    move_to_invalid_2fa,
    move_to_2fa_on,
    move_to_other_device,
    move_to_unauth,
    move_to_rejected,
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
) -> bool:

    selling_client = TelegramClient(str(session_path), api_id, api_hash)
    user_id = None
    try:
        await selling_client.connect()
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
        await asyncio.sleep(_warmup)

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
                try:
                    await selling_client.disconnect()
                except Exception:
                    pass
                return True

            if trigger == "already_sold":
                print(f"⏭ Nomor {phone} sudah pernah terjual, skip.")
                log_failed(phone, bot_username, "already_sold")
                if event_cb:
                    await event_cb("already_sold", phone)
                try:
                    await selling_client.disconnect()
                except Exception:
                    pass
                return False

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
                    outcome = await asyncio.wait_for(result_future, timeout=180)
                except asyncio.TimeoutError:
                    print("⚠️ Tidak ada update dari bot setelah OTP → unknown_state")
                    log_failed(phone, bot_username, "unknown_state")
                    await selling_client.disconnect()
                    return False

                if outcome == "success":
                    print(f"🎉 Sukses jual (via handler): {phone}")
                    log_success(phone, bot_username, "sold")
                    try:
                        await selling_client.disconnect()
                    except Exception:
                        pass
                    return True

                elif outcome == "rejected":
                    print(f"🚫 Account {phone} ditolak buyer (contact restriction), dikembalikan.")
                    log_failed(phone, bot_username, "rejected_by_buyer")
                    if event_cb:
                        await event_cb("rejected", phone)
                    try:
                        await selling_client.disconnect()
                    except Exception:
                        pass
                    await asyncio.sleep(2.0)
                    move_to_rejected(session_path)
                    return False

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

    except Exception as e:
        print(f"❌ Error conv bot: {e}")
        log_failed(phone, bot_username, f"conv_error: {e}")
        try:
            await selling_client.disconnect()
        except Exception:
            pass
        return False


def _normalize_phone(s: str) -> str:
    """Ambil cuma digit, buang + dan karakter lain buat perbandingan."""
    return re.sub(r"\D", "", s or "")


async def sell_one_session_reply_mode(
    api_id: int,
    api_hash: str,
    admin_client: TelegramClient,
    bot_username: str,
    session_path: Path,
    otp_timeout: int = 90,
    event_cb=None,
) -> bool:
    """
    Mode buyer yg formatnya:
    1. Kirim nomor (bisa banyak)
    2. Buyer balas: '📨 OTP to +62xxxx ... Reply to this message'
    3. Kita BALAS (reply) ke pesan itu dengan OTP
    4. Buyer balas:
       '✅ Successfully\n📱 Number: +62...\n...'
    """

    selling_client = TelegramClient(str(session_path), api_id, api_hash)
    user_id = None
    try:
        # connect & auth
        await selling_client.connect()
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
        await asyncio.sleep(_warmup)

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
        delay = random.uniform(1.0, 5.0)
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
            outcome = await asyncio.wait_for(result_future, timeout=180)
        except asyncio.TimeoutError:
            print("⚠️ [REPLY] Tidak ada update hasil setelah OTP → unknown_state")
            log_failed(phone, bot_username, "unknown_state_reply")
            await selling_client.disconnect()
            return False

        if outcome == "success":
            print(f"🎉 [REPLY] Sukses jual (reply-mode): {phone}")
            log_success(phone, bot_username, "sold_reply_mode")
            try:
                await selling_client.disconnect()
            except Exception:
                pass
            return True

        elif outcome == "rejected":
            print(f"🚫 [REPLY] Account {phone} ditolak buyer, dikembalikan.")
            log_failed(phone, bot_username, "rejected_by_buyer_reply")
            if event_cb:
                await event_cb("rejected", phone)
            try:
                await selling_client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(2.0)
            move_to_rejected(session_path)
            return False

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


async def sell_sessions_with_bot(
    api_id: int,
    api_hash: str,
    admin_client: TelegramClient,
    bot_username: str,
    session_files: list[Path],
    progress_cb=None,
    event_cb=None,
):
    ok, fail = 0, 0
    total = len(session_files)
    pending_success: dict[str, Path] = {}
    late_rejected: list[tuple[str, Path]] = []

    @admin_client.on(events.NewMessage(chats=bot_username))
    @admin_client.on(events.MessageEdited(chats=bot_username))
    async def global_late_watcher(evt):
        raw = evt.raw_text or ""
        if not _looks_rejected(raw):
            return
        m = NUMBER_IN_MSG_RE.search(raw)
        if not m:
            return
        norm = _normalize_phone(m.group(1))
        for p, path in list(pending_success.items()):
            if _normalize_phone(p) == norm:
                del pending_success[p]
                late_rejected.append((p, path))
                move_to_rejected(path)
                if event_cb:
                    await event_cb("late_rejected", p)
                break

    try:
        for sp in session_files:
            phone_fallback = parse_phone_from_filename(sp.name) or sp.stem
            # Capture phone aktual dari me.phone (via phone_sent event) agar key session_msgs konsisten
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
                )
            except CapacityFullError:
                print(
                    f"\n⛔ Buyer capacity FULL untuk {bot_username}. "
                    f"Stop proses batch. Summary sementara → OK: {ok} | ❌ Fail: {fail}"
                )
                break

            actual_phone = _resolved[0] or phone_fallback
            if res:
                ok += 1
                if event_cb:
                    await event_cb("success", actual_phone)
                pending_success[actual_phone] = sp
            else:
                fail += 1
                if event_cb:
                    await event_cb("fail", actual_phone)
            if progress_cb:
                await progress_cb(ok, fail, total, sp.name)
            await asyncio.sleep(1)

        # Notifikasi ke user: menunggu window rejection
        if event_cb:
            await event_cb("batch_done", "")

        # Tunggu hingga 60 detik untuk late rejection setelah batch selesai
        if pending_success:
            deadline = asyncio.get_event_loop().time() + 10
            while pending_success and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(5)

        # Hapus file yang tidak di-reject (late-rejected sudah dipindahkan di watcher)
        for _p, path in list(pending_success.items()):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

        # Fallback: retry move untuk late-rejected yang gagal saat watcher (file lock Windows)
        for _p, path in late_rejected:
            if path.exists():
                move_to_rejected(path)

    finally:
        try:
            admin_client.remove_event_handler(global_late_watcher, events.NewMessage)
        except Exception:
            pass
        try:
            admin_client.remove_event_handler(global_late_watcher, events.MessageEdited)
        except Exception:
            pass

    real_ok = ok - len(late_rejected)
    print(f"\n✅ Selesai. OK: {real_ok} | Late-rejected: {len(late_rejected)} | ❌ Fail: {fail}")

    try:
        await admin_client.disconnect()
        print("🔌 Admin client disconnected setelah semua proses selesai.")
    except Exception as e:
        print(f"⚠️ Gagal disconnect admin client: {e}")

    return {"success": real_ok}


async def sell_sessions_with_reply_bot(
    api_id: int,
    api_hash: str,
    admin_client: TelegramClient,
    bot_username: str,
    session_files: list[Path],
    max_parallel: int = 10,
    progress_cb=None,
    event_cb=None,
):
    """
    Batch jual pakai buyer reply-mode.
    - max_parallel: berapa session yg dikerjain bareng (dari config.py mis. 10)
    """
    ok, fail = 0, 0
    total = len(session_files)
    sem = asyncio.Semaphore(max_parallel)
    pending_success: dict[str, Path] = {}
    late_rejected: list[tuple[str, Path]] = []

    @admin_client.on(events.NewMessage(chats=bot_username))
    @admin_client.on(events.MessageEdited(chats=bot_username))
    async def global_late_watcher_r(evt):
        raw = evt.raw_text or ""
        if not _looks_rejected(raw):
            return
        m = NUMBER_IN_MSG_RE.search(raw)
        if not m:
            return
        norm = _normalize_phone(m.group(1))
        for p, path in list(pending_success.items()):
            if _normalize_phone(p) == norm:
                del pending_success[p]
                late_rejected.append((p, path))
                move_to_rejected(path)
                if event_cb:
                    await event_cb("late_rejected", p)
                break

    try:
        async def worker(sp: Path):
            nonlocal ok, fail
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
                res = await sell_one_session_reply_mode(
                    api_id=api_id,
                    api_hash=api_hash,
                    admin_client=admin_client,
                    bot_username=bot_username,
                    session_path=sp,
                    otp_timeout=90,
                    event_cb=_cb,
                )
                actual_phone = _resolved[0] or phone_fallback
                if res:
                    ok += 1
                    if event_cb:
                        await event_cb("success", actual_phone)
                    pending_success[actual_phone] = sp
                else:
                    fail += 1
                    if event_cb:
                        await event_cb("fail", actual_phone)
                if progress_cb:
                    await progress_cb(ok, fail, total, sp.name)

        # Stagger: launch task satu per satu dengan jeda, agar koneksi selling_client
        # tidak semua terjadi bersamaan dari IP yang sama (mencegah freeze massal).
        tasks = []
        for i, sp in enumerate(session_files):
            tasks.append(asyncio.create_task(worker(sp)))
            if i < len(session_files) - 1:
                stagger = random.uniform(TASK_STAGGER_MIN, TASK_STAGGER_MAX)
                print(f"⏳ Stagger: jeda {stagger:.1f}s sebelum launch task berikutnya...")
                await asyncio.sleep(stagger)

        await asyncio.gather(*tasks)

        # Notifikasi ke user: menunggu window rejection
        if event_cb:
            await event_cb("batch_done", "")

        # Tunggu hingga 60 detik untuk late rejection setelah semua session selesai
        if pending_success:
            deadline = asyncio.get_event_loop().time() + 10
            while pending_success and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(5)

        # Hapus file yang tidak di-reject (late-rejected sudah dipindahkan di watcher)
        for _p, path in list(pending_success.items()):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

        # Fallback: retry move untuk late-rejected yang gagal saat watcher (file lock Windows)
        for _p, path in late_rejected:
            if path.exists():
                move_to_rejected(path)

    finally:
        try:
            admin_client.remove_event_handler(global_late_watcher_r, events.NewMessage)
        except Exception:
            pass
        try:
            admin_client.remove_event_handler(global_late_watcher_r, events.MessageEdited)
        except Exception:
            pass

    real_ok = ok - len(late_rejected)
    print(f"\n✅ [REPLY] Batch selesai. OK: {real_ok} | Late-rejected: {len(late_rejected)} | ❌ Fail: {fail}")

    try:
        await admin_client.disconnect()
        print("🔌 [REPLY] Admin client disconnected setelah semua proses selesai.")
    except Exception as e:
        print(f"⚠️ [REPLY] Gagal disconnect admin client: {e}")

    return {"success": real_ok}
