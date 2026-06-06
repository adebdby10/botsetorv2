# bot/handler.py
# FSM multi-user + worker isolation untuk Telegram Bot

import asyncio
import io
import shutil
import zipfile
from pathlib import Path

from telethon import events, Button, TelegramClient

from config import (
    ADMIN_DIR,
    SESSIONS_DIR,
    TWO_FA_ON_DIR,
    OTHER_DEVICE_DIR,
    UNAUTH_DIR,
    REJECTED_DIR,
    ALLOWED_USERS,
    REPLY_MAX_PARALLEL,
    ensure_dirs,
    get_api,
)
from engine.admin_session import get_admin_clients
from engine.seller import (
    sell_sessions_with_bot,
    sell_sessions_with_reply_bot,
)
from utils.logger import init_logs

# ─── States ────────────────────────────────────────────────────────────────────
STATE_IDLE          = "idle"
STATE_WAIT_ADMIN    = "wait_admin"
STATE_WAIT_SESSIONS = "wait_sessions"
STATE_WAIT_BOT      = "wait_bot"
STATE_WAIT_MODE     = "wait_mode"
STATE_PROCESSING    = "processing"

# ─── Global stores ─────────────────────────────────────────────────────────────
# State FSM tiap user — key = user_id, tidak ada data yang bisa bocor antar user
_user_states: dict[int, dict] = {}

# Task worker yang sedang berjalan — key = user_id, satu task per user
_active_workers: dict[int, asyncio.Task] = {}


# ─── State helpers ─────────────────────────────────────────────────────────────

def _get_state(user_id: int) -> dict:
    if user_id not in _user_states:
        _reset_state(user_id)
    return _user_states[user_id]


def _reset_state(user_id: int):
    _user_states[user_id] = {
        "state":           STATE_IDLE,
        "admin_file":      None,      # Path file admin.session yang diupload
        "sessions":        [],        # list[Path] session yang mau disetor
        "bot_username":    None,      # "@xxx"
        "mode":            None,      # "receiver" | "reply"
        "counter_msg_id":  None,      # id pesan tombol counter
    }


def _is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


def _user_admin_dir(user_id: int) -> Path:
    return ADMIN_DIR / str(user_id)


def _user_sessions_dir(user_id: int) -> Path:
    return SESSIONS_DIR / str(user_id)


def _get_saved_admin(user_id: int) -> Path | None:
    """Return path session admin tersimpan jika ada, else None."""
    admin_dir = _user_admin_dir(user_id)
    if admin_dir.exists():
        sessions = list(admin_dir.glob("*.session"))
        if sessions:
            return sessions[0]
    return None


async def _show_admin_prompt(event, user_id: int, st: dict):
    """Tampilkan tombol 'pakai admin lama' jika ada, atau prompt upload langsung."""
    saved = _get_saved_admin(user_id)
    st["state"] = STATE_WAIT_ADMIN
    await event.answer()
    if saved:
        await event.edit(
            f"📁 Admin session tersimpan: `{saved.name}`\n\nGunakan admin lama atau upload baru?",
            buttons=[[
                Button.inline("✅ Pakai Admin Lama", b"use_saved_admin"),
                Button.inline("📤 Upload Admin Baru", b"upload_new_admin"),
            ]],
            parse_mode="md",
        )
    else:
        await event.edit(
            "📁 Upload **1 file .session sebagai ADMIN**.\n\n"
            "_(Ini akun yang digunakan untuk chat ke bot buyer)_",
            parse_mode="md",
        )


def _cleanup_user_files(user_id: int):
    """Hapus file temp milik user ini. ADMIN/<user_id>/ sengaja dipertahankan
    agar bisa ditawarkan 'pakai admin lama' pada sesi berikutnya."""
    shutil.rmtree(_user_sessions_dir(user_id), ignore_errors=True)
    # Hapus file admin sementara yang di-copy ke ADMIN/ root (bukan subfolder)
    for f in ADMIN_DIR.glob(f"u{user_id}_*.session"):
        f.unlink(missing_ok=True)


def _is_worker_running(user_id: int) -> bool:
    task = _active_workers.get(user_id)
    return task is not None and not task.done()


# ─── Welcome & UI helpers ──────────────────────────────────────────────────────

async def _send_welcome(bot: TelegramClient, chat_id: int):
    await bot.send_message(
        chat_id,
        "👋 *Selamat datang di Bot Setor Akun!*\n\n"
        "Tekan tombol di bawah untuk memulai proses setor akun ke bot buyer.",
        buttons=[[Button.inline("🚀 Mulai Setor Akun", b"start_setor")]],
        parse_mode="md",
    )


async def _update_session_counter(bot: TelegramClient, chat_id: int, user_id: int):
    """Edit/kirim pesan counter session + tombol konfirmasi."""
    st = _get_state(user_id)
    count = len(st["sessions"])
    text = (
        f"📥 *{count} session* sudah diterima."
    )
    buttons = [[Button.inline(f"✅ Proses {count} Session Ini", b"confirm_sessions")]]

    if st["counter_msg_id"]:
        try:
            await bot.edit_message(chat_id, st["counter_msg_id"], text,
                                   buttons=buttons, parse_mode="md")
            return
        except Exception:
            pass  # pesan lama tidak bisa diedit, kirim baru

    sent = await bot.send_message(chat_id, text, buttons=buttons, parse_mode="md")
    st["counter_msg_id"] = sent.id


# ─── ZIP extraction ───────────────────────────────────────────────────────────

def _extract_sessions_from_zip(zip_path: Path, dest_dir: Path) -> list[Path]:
    """
    Ekstrak ZIP ke folder sementara, lalu cari semua *.session secara rekursif
    (rglob) — menangani session yang berada di dalam subfolder manapun.
    Hasil di-flatten ke dest_dir. Hapus ZIP dan folder sementara setelah selesai.
    """
    tmp_dir = dest_dir / f"_tmp_{zip_path.stem}"
    extracted: list[Path] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_dir)

        # *.session di semua kedalaman subfolder
        for session_file in tmp_dir.rglob("*.session"):
            dest = dest_dir / session_file.name
            shutil.move(str(session_file), str(dest))
            extracted.append(dest)

    except zipfile.BadZipFile:
        pass  # bukan ZIP valid — caller yang handle
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        zip_path.unlink(missing_ok=True)
    return extracted


# ─── Register all handlers ─────────────────────────────────────────────────────

def _startup_cleanup():
    """
    Hapus folder temp yang tertinggal dari sesi sebelumnya (bot restart / crash).
    SESSIONS/<user_id>/ selalu dihapus (data proses sementara).
    ADMIN/<user_id>/ sengaja dipertahankan (tersimpan untuk reuse admin lama).
    """
    cleaned = 0
    for user_dir in SESSIONS_DIR.glob("*"):
        if user_dir.is_dir() and user_dir.name.isdigit():
            shutil.rmtree(user_dir, ignore_errors=True)
            cleaned += 1
    for f in ADMIN_DIR.glob("u[0-9]*.session"):
        f.unlink(missing_ok=True)
        cleaned += 1
    if cleaned:
        print(f"🧹 Startup cleanup: hapus {cleaned} item sisa sesi sebelumnya.")


def register_handlers(bot: TelegramClient):
    _startup_cleanup()

    # ── Pesan masuk ──────────────────────────────────────────────────────────
    @bot.on(events.NewMessage(incoming=True))
    async def on_message(event):
        user_id = event.sender_id
        chat_id = event.chat_id
        if not _is_allowed(user_id):
            return

        st = _get_state(user_id)

        # ── File upload ──────────────────────────────────────────────────────
        if event.document:
            attrs = event.document.attributes
            fname = next((getattr(a, "file_name", None) for a in attrs), None) or ""
            flower = fname.lower()

            # Tolak file selain .session / .zip
            if not (flower.endswith(".session") or flower.endswith(".zip")):
                await event.reply(
                    "⚠️ File tidak dikenali.\n"
                    "Kirim file `.session` atau `.zip` berisi file `.session`."
                )
                return

            # ── Upload ADMIN session (harus .session tunggal) ────────────────
            if st["state"] == STATE_WAIT_ADMIN:
                if not flower.endswith(".session"):
                    await event.reply("⚠️ Admin session harus berupa file `.session` langsung, bukan ZIP.")
                    return
                dest_dir = _user_admin_dir(user_id)
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / fname
                await bot.download_media(event.message, file=str(dest))
                st["admin_file"] = dest
                st["state"] = STATE_WAIT_SESSIONS
                st["counter_msg_id"] = None
                await event.reply(
                    f"✅ Admin session diterima: `{fname}`\n\n"
                    "Sekarang upload file `.session` atau `.zip` berisi session yang mau disetor.\n"
                    "Boleh kirim berkali-kali."
                )
                return

            # ── Upload SESSION untuk disetor (.session atau .zip) ────────────
            if st["state"] == STATE_WAIT_SESSIONS:
                dest_dir = _user_sessions_dir(user_id)
                dest_dir.mkdir(parents=True, exist_ok=True)

                if flower.endswith(".session"):
                    # File .session tunggal
                    dest = dest_dir / fname
                    await bot.download_media(event.message, file=str(dest))
                    if dest not in st["sessions"]:
                        st["sessions"].append(dest)
                    await _update_session_counter(bot, chat_id, user_id)

                else:
                    # File .zip — download dulu, lalu ekstrak
                    zip_dest = dest_dir / fname
                    await bot.download_media(event.message, file=str(zip_dest))
                    extracted = _extract_sessions_from_zip(zip_dest, dest_dir)

                    if not extracted:
                        await event.reply(
                            "⚠️ ZIP tidak mengandung file `.session` yang valid. Upload ulang."
                        )
                        return

                    added = 0
                    for sp in extracted:
                        if sp not in st["sessions"]:
                            st["sessions"].append(sp)
                            added += 1

                    await event.reply(
                        f"📦 ZIP diekstrak: *{added} session* ditambahkan dari `{fname}`."
                    )
                    await _update_session_counter(bot, chat_id, user_id)

                return

            await event.reply(
                "⚠️ Tidak sedang dalam sesi upload.\n"
                "Ketik /start untuk memulai ulang."
            )
            return

        # ── Teks ─────────────────────────────────────────────────────────────
        raw = (event.raw_text or "").strip()

        if raw in ("/start", "/menu") or st["state"] == STATE_IDLE:
            _reset_state(user_id)
            await _send_welcome(bot, chat_id)
            return

        if st["state"] == STATE_WAIT_BOT:
            uname = raw if raw.startswith("@") else f"@{raw}"
            st["bot_username"] = uname
            st["state"] = STATE_WAIT_MODE
            await event.reply(
                f"✅ Bot tujuan: `{uname}`\n\nPilih mode setor:",
                buttons=[[
                    Button.inline("🔁 Receiver Mode", b"mode_receiver"),
                    Button.inline("💬 Reply Mode",    b"mode_reply"),
                ]],
            )
            return

        # State lain yang tidak butuh input teks
        await _send_welcome(bot, chat_id)

    # ── Callback tombol inline ────────────────────────────────────────────────
    @bot.on(events.CallbackQuery())
    async def on_callback(event):
        user_id = event.sender_id
        chat_id = event.chat_id
        if not _is_allowed(user_id):
            await event.answer("⛔ Akses tidak diizinkan.", alert=True)
            return

        data = event.data
        st  = _get_state(user_id)

        # Mulai alur setor
        if data == b"start_setor":
            if _is_worker_running(user_id):
                await event.answer("⚠️ Masih ada proses berjalan, tunggu selesai dulu.", alert=True)
                return
            _reset_state(user_id)
            st = _get_state(user_id)
            await _show_admin_prompt(event, user_id, st)
            return

        # Pakai admin session yang sudah tersimpan
        if data == b"use_saved_admin":
            saved = _get_saved_admin(user_id)
            if not saved:
                await event.answer("Session admin tidak ditemukan, upload baru.", alert=True)
                await event.edit(
                    "📁 Upload **1 file .session sebagai ADMIN**.\n\n"
                    "_(Ini akun yang digunakan untuk chat ke bot buyer)_",
                    parse_mode="md",
                )
                return
            st["admin_file"] = saved
            st["state"] = STATE_WAIT_SESSIONS
            st["counter_msg_id"] = None
            await event.answer()
            await event.edit(
                f"✅ Menggunakan admin lama: `{saved.name}`\n\n"
                "Sekarang upload file `.session` atau `.zip` berisi session yang mau disetor.\n"
                "Boleh kirim berkali-kali.",
                parse_mode="md",
            )
            return

        # Upload admin baru (hapus admin lama dulu)
        if data == b"upload_new_admin":
            shutil.rmtree(_user_admin_dir(user_id), ignore_errors=True)
            st["state"] = STATE_WAIT_ADMIN
            await event.answer()
            await event.edit(
                "📁 Upload **1 file .session sebagai ADMIN**.\n\n"
                "_(Ini akun yang digunakan untuk chat ke bot buyer)_",
                parse_mode="md",
            )
            return

        # Konfirmasi selesai upload session
        if data == b"confirm_sessions":
            if st["state"] != STATE_WAIT_SESSIONS:
                await event.answer("Tidak ada sesi upload aktif.", alert=True)
                return
            if not st["sessions"]:
                await event.answer("Belum ada session yang diupload.", alert=True)
                return
            st["state"] = STATE_WAIT_BOT
            await event.answer()
            await event.edit(
                f"✅ **{len(st['sessions'])} session** siap.\n\n"
                "Ketik username bot tujuan di chat:",
                parse_mode="md",
            )
            return

        # Pilih mode
        if data in (b"mode_receiver", b"mode_reply"):
            if st["state"] != STATE_WAIT_MODE:
                await event.answer("Pilihan mode tidak relevan sekarang.", alert=True)
                return
            if _is_worker_running(user_id):
                await event.answer("Masih ada proses berjalan.", alert=True)
                return

            st["mode"] = "receiver" if data == b"mode_receiver" else "reply"
            mode_label  = "🔁 Receiver (Sequential)" if st["mode"] == "receiver" else "💬 Reply (Parallel)"
            n_sessions  = len(st["sessions"])
            bot_uname   = st["bot_username"]

            st["state"] = STATE_PROCESSING
            await event.answer()
            await event.edit(
                f"✅ Mode: **{mode_label}**\n\n"
                f"🚀 Memulai **{n_sessions} session** → `{bot_uname}`\n\n"
                "_Proses berjalan di background. Hasil akan dikirim setelah selesai..._",
                parse_mode="md",
            )

            task = asyncio.create_task(_run_worker(bot, user_id, chat_id))
            _active_workers[user_id] = task
            return

        # Setor lagi
        if data == b"setor_lagi":
            if _is_worker_running(user_id):
                await event.answer("Masih ada proses berjalan.", alert=True)
                return
            _reset_state(user_id)
            st = _get_state(user_id)
            await _show_admin_prompt(event, user_id, st)
            return

        await event.answer("Tombol tidak dikenal.", alert=True)


# ─── Problem session ZIP helper ───────────────────────────────────────────────

async def _send_back_problem_sessions(
    bot: TelegramClient, chat_id: int, user_id: int
) -> dict[str, int]:
    """
    Bundle semua session gagal ke satu gagal.zip dengan subfolder per kategori.
    Termasuk session yang masih tersisa di SESSIONS/<user_id>/ (gagal misc: OTP, buyer error, dsb).
    Kirim ke user, bersihkan folder problem setelah kirim.
    SESSIONS/<user_id>/ dibersihkan oleh _cleanup_user_files.
    """
    categories = [
        (TWO_FA_ON_DIR    / str(user_id), "2FA ON",                 "2fa_on",   True),
        (OTHER_DEVICE_DIR / str(user_id), "Device lain terdeteksi", "device",   True),
        (UNAUTH_DIR       / str(user_id), "Unauth",                 "unauth",   True),
        (REJECTED_DIR     / str(user_id), "Rejected",               "rejected", True),
        (SESSIONS_DIR     / str(user_id), "Error",                  "misc",     False),
    ]
    all_files: list[tuple[Path, str]] = []
    counts: dict[str, int] = {"2fa_on": 0, "device": 0, "unauth": 0, "rejected": 0, "misc": 0}

    for folder, subfolder_name, key, _ in categories:
        if not folder.exists():
            continue
        for f in folder.glob("*.session"):
            all_files.append((f, subfolder_name))
            counts[key] += 1

    if not all_files:
        return counts

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f, subfolder in all_files:
            zf.write(str(f), arcname=f"{subfolder}/{f.name}")
    buf.seek(0)
    buf.name = "gagal.zip"

    await bot.send_file(
        chat_id,
        buf,
        caption=(
            "Berikut session yang gagal diproses.\n"
            "Folder di dalam ZIP:\n"
            "- 2FA ON → matikan 2FA lalu upload ulang\n"
            "- Device lain terdeteksi → tunggu 24 jam lalu upload ulang\n"
            "- Unauth → session tidak valid/expired\n"
            "- Rejected → session masih valid, bisa dijual ke buyer lain\n"
            "- Error → OTP gagal/buyer error, coba upload ulang"
        ),
        force_document=True,
    )

    # Bersihkan folder problem saja; SESSIONS/<user_id>/ dibersihkan oleh _cleanup_user_files
    for folder, _, __, cleanup in categories:
        if cleanup:
            shutil.rmtree(folder, ignore_errors=True)

    return counts


# ─── Worker (isolated per user) ───────────────────────────────────────────────

async def _run_worker(bot: TelegramClient, user_id: int, chat_id: int):
    """
    Satu worker per user, berjalan di asyncio.create_task().
    Tidak ada semaphore global — asyncio handle ratusan concurrent coroutine secara native
    karena semua proses bersifat I/O-bound (network calls ke Telegram API).
    Satu-satunya batasan: setiap user hanya boleh punya 1 job aktif (lihat _is_worker_running).
    """
    await _do_automation(bot, user_id, chat_id)


async def _do_automation(bot: TelegramClient, user_id: int, chat_id: int):
    """Eksekusi automation untuk satu user secara terisolasi."""
    st = _user_states.get(user_id)
    if not st:
        return

    try:
        ensure_dirs()
        init_logs()
        api_id, api_hash = get_api()
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Setup gagal: `{e}`\nCoba beberapa saat lagi.")
        _cleanup_user_files(user_id)
        _reset_state(user_id)
        return

    # Salin admin.session ke ADMIN/ root agar get_admin_clients() bisa baca
    admin_src: Path = st["admin_file"]
    admin_dst = ADMIN_DIR / f"u{user_id}_{admin_src.name}"
    try:
        shutil.copy(str(admin_src), str(admin_dst))
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Gagal copy admin session: `{e}`")
        _cleanup_user_files(user_id)
        _reset_state(user_id)
        return

    admin_clients = await get_admin_clients(api_id, api_hash, [admin_dst.name])
    if not admin_clients:
        await bot.send_message(
            chat_id,
            "❌ Gagal load admin session.\n"
            "Pastikan file `.session` valid dan sudah pernah login.",
        )
        _cleanup_user_files(user_id)
        admin_dst.unlink(missing_ok=True)
        _reset_state(user_id)
        return

    admin_client   = admin_clients[0]
    sessions       = st["sessions"]       # list[Path] di SESSIONS/<user_id>/
    bot_username   = st["bot_username"]
    mode           = st["mode"]
    total_sessions = len(sessions)

    # ── Progress message realtime ──────────────────────────────────────────────
    import time as _time

    def _make_progress_text(ok: int, fail: int, total: int) -> str:
        done   = ok + fail
        filled = int(done / total * 10) if total else 0
        bar    = "=" * filled + (">" if done < total else "=") + " " * max(0, 9 - filled)
        return (
            f"Memproses {total} session ke {bot_username}...\n\n"
            f"Berhasil : {ok}\n"
            f"Gagal    : {fail}\n"
            f"Sisa     : {total - done}\n\n"
            f"[{bar}] {done}/{total}"
        )

    prog_msg     = await bot.send_message(chat_id, _make_progress_text(0, 0, total_sessions))
    prog_msg_id  = prog_msg.id
    last_edit    = 0.0

    async def on_progress(ok: int, fail: int, total: int, _session_name: str):
        nonlocal last_edit
        now  = _time.monotonic()
        done = ok + fail
        if now - last_edit < 3.0 and done < total:
            return
        last_edit = now
        try:
            await bot.edit_message(chat_id, prog_msg_id, _make_progress_text(ok, fail, total))
        except Exception:
            pass

    # Per-session realtime messages: 1 pesan per nomor, di-edit tiap tahap
    session_msgs:  dict[str, int] = {}   # phone → message_id
    session_final: set[str]       = set()  # phones dengan final state (jangan ditimpa)

    async def event_cb(event_type: str, phone: str, extra: str = ""):
        try:
            if event_type == "phone_sent":
                msg = await bot.send_message(
                    chat_id,
                    f"📩 Kirim {phone} → {bot_username}",
                )
                session_msgs[phone] = msg.id

            elif event_type == "otp_sent":
                text = f"🔑 OTP {phone}: {extra} → dikirim ke {bot_username}"
                msg_id = session_msgs.get(phone)
                if msg_id:
                    await bot.edit_message(chat_id, msg_id, text)
                else:
                    msg = await bot.send_message(chat_id, text)
                    session_msgs[phone] = msg.id

            elif event_type == "rejected":
                text = f"⚠️ {phone} ditolak buyer (dikembalikan)"
                msg_id = session_msgs.get(phone)
                if msg_id:
                    await bot.edit_message(chat_id, msg_id, text)
                else:
                    await bot.send_message(chat_id, text)
                session_final.add(phone)

            elif event_type == "success":
                if phone in session_final:
                    return
                text = f"✅ {phone} berhasil disetor"
                msg_id = session_msgs.pop(phone, None)
                if msg_id:
                    await bot.edit_message(chat_id, msg_id, text)
                else:
                    await bot.send_message(chat_id, text)
                session_final.add(phone)

            elif event_type == "fail":
                if phone in session_final:
                    return
                text = f"❌ {phone} gagal"
                msg_id = session_msgs.pop(phone, None)
                if msg_id:
                    await bot.edit_message(chat_id, msg_id, text)
                else:
                    await bot.send_message(chat_id, text)

            elif event_type == "late_rejected":
                text = f"⚠️ {phone} ditolak buyer (dikembalikan)"
                msg_id = session_msgs.get(phone)
                if msg_id:
                    try:
                        await bot.edit_message(chat_id, msg_id, text)
                    except Exception:
                        await bot.send_message(chat_id, text)
                else:
                    await bot.send_message(chat_id, text)

            elif event_type == "batch_done":
                try:
                    await bot.send_message(
                        chat_id,
                        "⏳ Menunggu akun rejected dari buyer...\n(maks. 10 detik)"
                    )
                except Exception:
                    pass

            elif event_type == "already_sold":
                if phone in session_final:
                    return
                text = f"⏭ {phone} sudah pernah terjual (di-skip)"
                msg_id = session_msgs.pop(phone, None)
                if msg_id:
                    await bot.edit_message(chat_id, msg_id, text)
                else:
                    await bot.send_message(chat_id, text)
                session_final.add(phone)

        except Exception as e:
            print(f"⚠️ event_cb [{event_type}][{phone}]: {e}")

    try:
        if mode == "receiver":
            # Flow receiversell.py — sequential, setor satu per satu (bisa banyak)
            result = await sell_sessions_with_bot(
                api_id=api_id,
                api_hash=api_hash,
                admin_client=admin_client,
                bot_username=bot_username,
                session_files=sessions,
                progress_cb=on_progress,
                event_cb=event_cb,
            )
        else:
            # Flow replysetor.py — parallel, setor banyak sekaligus
            result = await sell_sessions_with_reply_bot(
                api_id=api_id,
                api_hash=api_hash,
                admin_client=admin_client,
                bot_username=bot_username,
                session_files=sessions,
                max_parallel=REPLY_MAX_PARALLEL,
                progress_cb=on_progress,
                event_cb=event_cb,
            )

        n_success    = (result or {}).get("success", 0)
        counts       = await _send_back_problem_sessions(bot, chat_id, user_id)
        n_rejected   = counts["rejected"]
        n_other_fail = counts["2fa_on"] + counts["device"] + counts["unauth"] + counts["misc"]
        final_text   = (
            f"Selesai memproses {total_sessions} session ke {bot_username}.\n\n"
            f"Berhasil : {n_success}\n"
            f"Rejected : {n_rejected}\n"
            f"Gagal    : {n_other_fail}"
        )
        try:
            await bot.delete_messages(chat_id, [prog_msg_id])
        except Exception:
            pass
        await bot.send_message(
            chat_id, final_text,
            buttons=[[Button.inline("🔄 Setor Lagi", b"setor_lagi")]],
        )

    except Exception as e:
        await bot.send_message(
            chat_id,
            f"❌ Error tak terduga saat proses:\n`{e}`\n\n"
            "Silakan coba lagi.",
            buttons=[[Button.inline("🔄 Coba Lagi", b"setor_lagi")]],
        )

    finally:
        admin_dst.unlink(missing_ok=True)
        _cleanup_user_files(user_id)
        _reset_state(user_id)
        _active_workers.pop(user_id, None)
