# bot/handler.py
# FSM multi-user + worker isolation untuk Telegram Bot

import asyncio
import io
import shutil
import zipfile
from pathlib import Path

from telethon import events, Button, TelegramClient, errors

from config import (
    USERBOT_DIR,
    SESSIONS_DIR,
    TWO_FA_ON_DIR,
    OTHER_DEVICE_DIR,
    UNAUTH_DIR,
    REJECTED_DIR,
    RECOVERED_DIR,
    ALREADY_SOLD_DIR,
    CANCELLED_DIR,
    ARCHIVE_DIR,
    ALLOWED_USERS,
    REPLY_MAX_PARALLEL,
    WORLD_V1_BOT,
    WORLD_V1_DIR,
    ensure_dirs,
    get_api,
)
from engine.admin_session import get_userbot_clients
from engine.seller import (
    sell_sessions_with_bot,
    sell_sessions_with_reply_bot,
    _zip_recovered_sessions,
    _zip_already_sold_sessions,
    _zip_cancelled_sessions,
)
from engine.reg_world_v1 import run_batch as run_world_v1_batch
from utils.logger import init_logs

# ─── States ────────────────────────────────────────────────────────────────────
STATE_IDLE          = "idle"
STATE_WAIT_USERBOT  = "wait_userbot"
STATE_WAIT_SESSIONS = "wait_sessions"
STATE_WAIT_BOT      = "wait_bot"
STATE_WAIT_MODE     = "wait_mode"
STATE_PROCESSING    = "processing"
STATE_WAIT_UB_PHONE = "wait_ub_phone"
STATE_WAIT_UB_OTP   = "wait_ub_otp"
STATE_WAIT_UB_2FA   = "wait_ub_2fa"
STATE_WAIT_COUNTRY  = "wait_country"

# ─── Blockquote helper ────────────────────────────────────────────────────────
# ─── Country code → phone prefix mapping ────────────────────────────────────
COUNTRY_PREFIXES: dict[str, str] = {
    "ID": "62",  "IN": "91",  "PH": "63",  "TH": "66",  "VN": "84",
    "MY": "60",  "SG": "65",  "BD": "880", "PK": "92",  "LK": "94",
    "MM": "95",  "KH": "855", "LA": "856", "CN": "86",  "TW": "886",
    "KR": "82",  "JP": "81",  "US": "1",   "GB": "44",  "RU": "7",
    "BR": "55",  "NG": "234", "EG": "20",  "TR": "90",  "SA": "966",
    "AE": "971", "UA": "380", "AF": "93",  "IR": "98",  "IQ": "964",
    "IT": "39",  "DE": "49",  "FR": "33",  "ES": "34",  "PT": "351",
    "NL": "31",  "PL": "48",  "RO": "40",  "CZ": "420", "HU": "36",
    "CO": "57",  "MX": "52",  "AR": "54",  "CL": "56",  "PE": "51",
    "ZA": "27",  "KE": "254", "GH": "233", "MA": "212", "DZ": "213",
    "UZ": "998", "KZ": "7",   "BY": "375", "GE": "995", "AM": "374",
    "AZ": "994", "NP": "977", "LK": "94",  "BN": "673",
}


def _detect_country(phone: str) -> str | None:
    """Deteksi kode negara dari nomor HP (tanpa +)."""
    clean = phone.lstrip("+")
    for cc, prefix in COUNTRY_PREFIXES.items():
        if clean.startswith(prefix):
            return cc
    return None


def _q(text: str) -> str:
    """Wrap text in Telegram blockquote (HTML)."""
    return f"<blockquote>{text}</blockquote>"

# ─── Global stores ─────────────────────────────────────────────────────────────
# State FSM tiap user — key = user_id, tidak ada data yang bisa bocor antar user
_user_states: dict[int, dict] = {}

# Task worker yang sedang berjalan — key = user_id, satu task per user
_active_workers: dict[int, asyncio.Task] = {}
_stop_events: dict[int, asyncio.Event] = {}


# ─── State helpers ─────────────────────────────────────────────────────────────

def _get_state(user_id: int) -> dict:
    if user_id not in _user_states:
        _reset_state(user_id)
    return _user_states[user_id]


def _reset_state(user_id: int):
    # Bersihkan ub_client jika masih aktif
    old = _user_states.get(user_id)
    if old and old.get("ub_client"):
        try:
            asyncio.get_event_loop().create_task(old["ub_client"].disconnect())
        except Exception:
            pass
    _user_states[user_id] = {
        "state":           STATE_IDLE,
        "userbot_file":    None,      # Path file userbot.session yang diupload
        "sessions":        [],        # list[Path] session yang mau disetor
        "bot_username":    None,      # "@xxx"
        "mode":            None,      # "receiver" | "reply"
        "counter_msg_id":  None,      # id pesan tombol counter
        "ub_phone":        None,      # phone untuk login manual
        "ub_phone_code_hash": None,   # hash dari send_code_request
        "ub_client":       None,      # TelegramClient sementara untuk login
        "country_filter":  None,      # label sortir aktif ("+62", "2FA OFF", "#1-10", dll)
        "_sortir_mode":   None,      # internal: "country" | "id"
    }


def _is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


def _user_userbot_dir(user_id: int) -> Path:
    return USERBOT_DIR / str(user_id)


def _user_sessions_dir(user_id: int) -> Path:
    return SESSIONS_DIR / str(user_id)


def _get_saved_userbot(user_id: int) -> Path | None:
    """Return path session userbot tersimpan jika ada, else None."""
    userbot_dir = _user_userbot_dir(user_id)
    if userbot_dir.exists():
        sessions = list(userbot_dir.glob("*.session"))
        if sessions:
            return sessions[0]
    return None


async def _show_userbot_prompt(event, user_id: int, st: dict):
    """Tampilkan tombol 'pakai userbot lama' jika ada, atau prompt upload langsung."""
    saved = _get_saved_userbot(user_id)
    st["state"] = STATE_WAIT_USERBOT
    await event.answer()
    if saved:
        await event.edit(
            _q(
                f"📁 Userbot session tersimpan: <code>{saved.name}</code>\n\n"
                "Gunakan userbot lama atau upload baru?"
            ),
            buttons=[[
                Button.inline("✅ Pakai Userbot Lama", b"use_saved_userbot"),
            ], [
                Button.inline("📤 Upload Userbot Baru", b"upload_new_userbot"),
                Button.inline("🔐 Login Userbot", b"login_userbot"),
            ]],
            parse_mode="html",
        )
    else:
        await event.edit(
            _q(
                "📁 Upload <b>1 file .session sebagai Userbot atau login manual Userbot</b>.\n\n"
                "<i>(Userbot adalah akun Telegram yang berfungsi sebagai penyetor — "
                "digunakan untuk komunikasi dengan bot buyer)</i>"
            ),
            buttons=[[
                Button.inline("🔐 Login Userbot", b"login_userbot"),
            ]],
            parse_mode="html",
        )


def _cleanup_user_files(user_id: int):
    """Hapus file temp milik user ini. USERBOT/<user_id>/ sengaja dipertahankan
    agar bisa ditawarkan 'pakai userbot lama' pada sesi berikutnya."""
    shutil.rmtree(_user_sessions_dir(user_id), ignore_errors=True)
    # Hapus file userbot sementara yang di-copy ke USERBOT/ root (bukan subfolder)
    for f in USERBOT_DIR.glob(f"u{user_id}_*.session"):
        f.unlink(missing_ok=True)


def _archive_batch(user_id: int, uploaded_sessions: list[Path], failed_categories: list[tuple[Path, str]]):
    """Archive uploaded sessions dan failed sessions per batch."""
    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_dir = ARCHIVE_DIR / str(user_id) / f"batch_{ts}"
    uploaded_dir = batch_dir / "uploaded"
    failed_dir = batch_dir / "failed"

    # Copy uploaded sessions
    if uploaded_sessions:
        uploaded_dir.mkdir(parents=True, exist_ok=True)
        for sp in uploaded_sessions:
            if sp.exists():
                shutil.copy2(str(sp), str(uploaded_dir / sp.name))

    # Copy failed sessions per kategori
    for folder, category_name in failed_categories:
        if not folder.exists():
            continue
        for f in folder.glob("*.session"):
            dest = failed_dir / category_name
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(f), str(dest / f.name))
        # Also check subfolders (per-user structure)
        for sub in folder.rglob("*.session"):
            dest = failed_dir / category_name
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(sub), str(dest / sub.name))

    print(f"📦 Batch archived: {batch_dir}")


def _is_worker_running(user_id: int) -> bool:
    task = _active_workers.get(user_id)
    return task is not None and not task.done()


# ─── Welcome & UI helpers ──────────────────────────────────────────────────────

async def _send_info(bot: TelegramClient, chat_id: int):
    """Tampilkan info penting sebelum memulai."""
    await bot.send_message(
        chat_id,
        _q(
            "⚠️ <b>Info Penting:</b>\n\n"
            "1️⃣ Siapkan 1 akun sebagai <b>Userbot</b> "
            "(akun Telegram yang berfungsi sebagai penyetor)\n\n"
            "2️⃣ Siapkan file session yang akan di setor ke bot buyer\n\n"
            "3️⃣ Pastikan 2FA OFF pada semua akun\n\n"
            "4️⃣ Pastikan Userbot sudah /start bot buyernya dan sudah join channel/grup\n\n"
            "5️⃣ Bot buyer yang ada /modeotp, atur ke mode reply, dan saat setor dengan bot xen TG setor gunakan mode reply\n\n"
            "6️⃣ Bot buyer yang tidak ada /modeotp nya, saat setor dengan bot xen TG setor gunakan mode receiver"
        ),
        buttons=[[Button.inline("Saya Mengerti", b"info_ack")]],
        parse_mode="html",
    )


async def _send_welcome(bot: TelegramClient, chat_id: int):
    await bot.send_message(
        chat_id,
        _q(
            "👋 <b>Selamat datang di Bot Setor Akun!</b>\n\n"
            "Tekan tombol di bawah untuk memulai proses setor akun ke bot buyer."
        ),
        buttons=[[Button.inline("🚀 Mulai Setor Akun", b"start_setor")]],
        parse_mode="html",
    )


async def _update_session_counter(bot: TelegramClient, chat_id: int, user_id: int):
    """Edit/kirim pesan counter session + tombol konfirmasi."""
    st = _get_state(user_id)
    count = len(st["sessions"])
    country_label = f" ({st['country_filter']})" if st.get("country_filter") else ""
    text = _q(f"📥 <b>{count} session</b> sudah diterima{country_label}.")
    buttons = [
        [Button.inline(f"✅ Proses {count} Session Ini", b"confirm_sessions")],
        [Button.inline(f"🌍 {WORLD_V1_BOT}", b"start_world_v1")],
    ]

    if st["counter_msg_id"]:
        try:
            await bot.edit_message(chat_id, st["counter_msg_id"], text,
                                   buttons=buttons, parse_mode="html")
            return
        except Exception:
            pass  # pesan lama tidak bisa diedit, kirim baru

    sent = await bot.send_message(chat_id, text, buttons=buttons, parse_mode="html")
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
    USERBOT/<user_id>/ sengaja dipertahankan (tersimpan untuk reuse userbot lama).
    """
    cleaned = 0
    for user_dir in SESSIONS_DIR.glob("*"):
        if user_dir.is_dir() and user_dir.name.isdigit():
            shutil.rmtree(user_dir, ignore_errors=True)
            cleaned += 1
    for f in USERBOT_DIR.glob("u[0-9]*.session"):
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
                    _q(
                        "⚠️ File tidak dikenali.\n"
                        "Kirim file <code>.session</code> atau <code>.zip</code> berisi file <code>.session</code>."
                    ),
                    parse_mode="html",
                )
                return

            # ── Upload USERBOT session (harus .session tunggal) ──────────────
            if st["state"] == STATE_WAIT_USERBOT:
                if not flower.endswith(".session"):
                    await event.reply(
                        _q("⚠️ Userbot session harus berupa file <code>.session</code> langsung, bukan ZIP."),
                        parse_mode="html",
                    )
                    return
                dest_dir = _user_userbot_dir(user_id)
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / fname
                await bot.download_media(event.message, file=str(dest))
                st["userbot_file"] = dest
                st["state"] = STATE_WAIT_SESSIONS
                st["counter_msg_id"] = None
                await event.reply(
                    _q(
                        f"✅ Userbot session diterima: <code>{fname}</code>\n\n"
                        "Sekarang upload file <code>.session</code> atau <code>.zip</code> berisi session yang mau disetor.\n"
                        "Boleh kirim berkali-kali."
                    ),
                    parse_mode="html",
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
                            _q("⚠️ ZIP tidak mengandung file <code>.session</code> yang valid. Upload ulang."),
                            parse_mode="html",
                        )
                        return

                    added = 0
                    for sp in extracted:
                        if sp not in st["sessions"]:
                            st["sessions"].append(sp)
                            added += 1

                    await event.reply(
                        _q(
                            f"📦 ZIP diekstrak: <b>{added} session</b> ditambahkan dari <code>{fname}</code>."
                        ),
                        parse_mode="html",
                    )
                    await _update_session_counter(bot, chat_id, user_id)

                return

            await event.reply(
                _q(
                    "⚠️ Tidak sedang dalam sesi upload.\n"
                    "Ketik /start untuk memulai ulang."
                ),
                parse_mode="html",
            )
            return

        # ── Teks ─────────────────────────────────────────────────────────────
        raw = (event.raw_text or "").strip()

        if raw in ("/start", "/menu") or st["state"] == STATE_IDLE:
            _reset_state(user_id)
            await _send_info(bot, chat_id)
            return

        # ── Input sortir (negara / ID range) ──────────────────────────────────
        if st["state"] == STATE_WAIT_COUNTRY:
            sortir_mode = st.get("_sortir_mode", "country")

            if sortir_mode == "id":
                # ── Sortir by ID range ──
                raw_input = raw.strip()
                selected = []
                try:
                    if "-" in raw_input:
                        parts = raw_input.split("-", 1)
                        start = int(parts[0].strip())
                        end = int(parts[1].strip())
                    else:
                        start = end = int(raw_input)

                    total = len(st["sessions"])
                    if start < 1 or end < 1 or start > total or end > total:
                        await event.reply(
                            _q(f"⚠️ Range di luar jumlah session ({total}). Ketik 1-{total}."),
                            buttons=[[Button.inline("↩️ Batal", b"cancel_sortir")]],
                            parse_mode="html",
                        )
                        return
                    if start > end:
                        start, end = end, start
                    selected = st["sessions"][start - 1:end]
                except ValueError:
                    await event.reply(
                        _q("⚠️ Format salah. Ketik range contoh <code>1-10</code> atau nomor tunggal <code>5</code>."),
                        buttons=[[Button.inline("↩️ Batal", b"cancel_sortir")]],
                        parse_mode="html",
                    )
                    return

                removed = len(st["sessions"]) - len(selected)
                st["sessions"] = selected
                st["country_filter"] = f"#{start}-{end}"
                st.pop("_sortir_mode", None)
                st["state"] = STATE_WAIT_SESSIONS
                if not selected:
                    await event.reply(
                        _q("❌ Tidak ada session terpilih."),
                        buttons=[[Button.inline("🔄 Reset Sortir", b"cancel_sortir")]],
                        parse_mode="html",
                    )
                else:
                    await event.reply(
                        _q(f"🔢 Sortir ID <b>#{start}-{end}</b>: <b>{len(selected)}</b> session dipilih, <b>{removed}</b> dihapus."),
                        parse_mode="html",
                    )
                    await _update_session_counter(bot, chat_id, user_id)
                return

            else:
                # ── Sortir by negara ──
                raw_code = raw.strip().lstrip("+")
                if not raw_code.isdigit():
                    await event.reply(
                        _q(
                            f"⚠️ Input tidak valid.\n\n"
                            "Ketik kode negara (angka saja), contoh:\n"
                            "<code>62</code> = Indonesia\n"
                            "<code>91</code> = India\n"
                            "<code>63</code> = Filipina\n"
                            "<code>1</code> = US/Canada\n"
                            "<code>66</code> = Thailand"
                        ),
                        buttons=[[Button.inline("↩️ Batal Sortir", b"cancel_sortir")]],
                        parse_mode="html",
                    )
                    return

                prefix = raw_code
                filtered = []
                for sp in st["sessions"]:
                    phone = sp.stem.lstrip("+")
                    if phone.startswith(prefix):
                        filtered.append(sp)
                removed = len(st["sessions"]) - len(filtered)
                st["sessions"] = filtered
                st["country_filter"] = f"+{prefix}"
                st["state"] = STATE_WAIT_SESSIONS
                if not filtered:
                    await event.reply(
                        _q(
                            f"❌ Tidak ada session dengan kode <b>+{prefix}</b>.\n"
                            f"Semua {removed} session dihapus dari daftar."
                        ),
                        buttons=[[Button.inline("🔄 Reset Sortir", b"cancel_sortir")]],
                        parse_mode="html",
                    )
                else:
                    await event.reply(
                        _q(
                            f"🌍 Sortir <b>+{prefix}</b>: <b>{len(filtered)}</b> session cocok, "
                            f"<b>{removed}</b> session dihapus."
                        ),
                        parse_mode="html",
                    )
                    await _update_session_counter(bot, chat_id, user_id)
                return

        # ── Login manual: input nomor HP ────────────────────────────────────
        if st["state"] == STATE_WAIT_UB_PHONE:
            phone = raw
            if not phone.startswith("+") or len(phone) < 8:
                await event.reply(
                    _q("⚠️ Format nomor salah. Gunakan format <code>+6281234567890</code>."),
                    parse_mode="html",
                )
                return
            api_id, api_hash = get_api()
            dest_dir = _user_userbot_dir(user_id)
            dest_dir.mkdir(parents=True, exist_ok=True)
            # Nama file session: nomor tanpa +
            session_name = phone.lstrip("+")
            session_path = dest_dir / f"{session_name}.session"
            # Hapus session file lama yg mungkin corrupt/stale
            if session_path.exists():
                session_path.unlink()
                print(f"🗑️ Hapus session lama: {session_path.name}")
            from telethon.sessions import StringSession
            ub_session = StringSession()
            client = TelegramClient(ub_session, api_id, api_hash, device_model="")
            try:
                await client.connect()
                result = await client.send_code_request(phone)
                st["ub_phone"] = phone
                st["ub_phone_code_hash"] = result.phone_code_hash
                st["ub_session_path"] = str(session_path)
                st["ub_client"] = client
                st["state"] = STATE_WAIT_UB_OTP
                print(f"📱 OTP request sent to {phone} (hash={result.phone_code_hash[:10]}...)")
                await event.reply(
                    _q(
                        f"📩 OTP dikirim ke <b>{phone}</b>.\n\n"
                        "Ketik kode OTP (5/6 digit):"
                    ),
                    buttons=[[Button.inline("❌ Batal", b"cancel_ub_login")]],
                    parse_mode="html",
                )
            except Exception as e:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                await event.reply(
                    _q(f"❌ Gagal kirim OTP: <code>{e}</code>\n\nCoba lagi dengan nomor yang benar."),
                    parse_mode="html",
                )
            return

        # ── Login manual: input OTP ─────────────────────────────────────────
        if st["state"] == STATE_WAIT_UB_OTP:
            code = raw.strip()
            if not code.isdigit() or len(code) not in (5, 6, 8):
                await event.reply(
                    _q("⚠️ Kode OTP harus 5/6/8 digit angka."),
                    parse_mode="html",
                )
                return
            client = st.get("ub_client")
            phone = st.get("ub_phone")
            session_path_str = st.get("ub_session_path")
            if not client or not phone:
                await event.reply(
                    _q("❌ Sesi login expired. Ketik /start untuk ulang."),
                    parse_mode="html",
                )
                return
            try:
                # sign_in dengan StringSession + phone_code_hash eksplisit
                phone_code_hash = st.get("ub_phone_code_hash")
                print(f"🔐 sign_in: phone={phone} code={code} hash={phone_code_hash[:10] if phone_code_hash else 'NONE'}...")
                if phone_code_hash:
                    await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                else:
                    await client.sign_in(phone=phone, code=code)
                # Sukses login — simpan StringSession ke file .session
                me = await client.get_me()
                print(f"✅ Login OK: {me.first_name} @{me.username}")

                # Save StringSession → .session file
                api_id_sv, api_hash_sv = get_api()
                session_path = Path(session_path_str) if session_path_str else dest_dir / f"{phone.lstrip('+')}.session"
                from telethon import TelegramClient as _TC
                ss_cl = _TC(str(session_path), api_id_sv, api_hash_sv)
                ss_cl.session._dc_id = client.session._dc_id
                ss_cl.session._server_address = client.session._server_address
                ss_cl.session._port = client.session._port
                ss_cl.session._auth_key = client.session._auth_key
                await ss_cl.connect()
                await ss_cl.disconnect()
                print(f"💾 Session saved: {session_path.name}")

                await client.disconnect()
                st["userbot_file"] = session_path
                st["ub_client"] = None
                st["state"] = STATE_WAIT_SESSIONS
                st["counter_msg_id"] = None
                await event.reply(
                    _q(
                        f"✅ Login berhasil! <b>{me.first_name or ''}</b> (@{me.username or '-'})\n\n"
                        "Sekarang upload file <code>.session</code> atau <code>.zip</code> berisi session yang mau disetor.\n"
                        "Boleh kirim berkali-kali."
                    ),
                    parse_mode="html",
                )
            except errors.SessionPasswordNeededError:
                # 2FA required — langsung minta password, jangan disconnect
                st["state"] = STATE_WAIT_UB_2FA
                await event.reply(
                    _q(
                        "🔒 Akun ini menggunakan 2FA.\n\n"
                        "Ketik password 2FA:"
                    ),
                    buttons=[[Button.inline("❌ Batal", b"cancel_ub_login")]],
                    parse_mode="html",
                )
            except errors.PhoneCodeExpiredError:
                await event.reply(
                    _q(
                        "⚠️ Kode OTP expired.\n\n"
                        "Gunakan tombol <b>🔄 Kirim Ulang OTP</b> untuk minta kode baru."
                    ),
                    buttons=[[Button.inline("🔄 Kirim Ulang OTP", b"resend_otp"), Button.inline("❌ Batal", b"cancel_ub_login")]],
                    parse_mode="html",
                )
            except errors.PhoneCodeInvalidError:
                await event.reply(
                    _q(
                        "⚠️ Kode OTP salah.\n\n"
                        "Ketik ulang kode OTP yang benar:"
                    ),
                    buttons=[[Button.inline("🔄 Kirim Ulang OTP", b"resend_otp"), Button.inline("❌ Batal", b"cancel_ub_login")]],
                    parse_mode="html",
                )
            except errors.FloodWaitError as fw:
                await event.reply(
                    _q(f"⚠️ Flood wait {fw.seconds} detik. Tunggu sebentar."),
                    parse_mode="html",
                )
            except Exception as e:
                err = str(e)
                if "password" in err.lower():
                    # Fallback detection for other password errors
                    st["state"] = STATE_WAIT_UB_2FA
                    await event.reply(
                        _q(
                            "🔒 Akun ini menggunakan 2FA.\n\n"
                            "Ketik password 2FA:"
                        ),
                        buttons=[[Button.inline("❌ Batal", b"cancel_ub_login")]],
                        parse_mode="html",
                    )
                else:
                    await event.reply(
                        _q(
                            f"⚠️ <code>{err}</code>\n\n"
                            "Gunakan tombol <b>🔄 Kirim Ulang OTP</b> untuk coba lagi,"
                            " atau /start untuk ulang dari awal."
                        ),
                        buttons=[[Button.inline("🔄 Kirim Ulang OTP", b"resend_otp"), Button.inline("❌ Batal", b"cancel_ub_login")]],
                        parse_mode="html",
                    )
            return

        # ── Login manual: input 2FA password ────────────────────────────────
        if st["state"] == STATE_WAIT_UB_2FA:
            password = raw
            client = st.get("ub_client")
            phone = st.get("ub_phone")
            session_path_str = st.get("ub_session_path")
            if not client or not phone:
                await event.reply(
                    _q("❌ Sesi login expired. Ketik /start untuk ulang."),
                    parse_mode="html",
                )
                return
            try:
                print(f"🔑 sign_in dengan password untuk {phone}")
                await client.sign_in(password=password)
                me = await client.get_me()
                print(f"✅ Login 2FA OK: {me.first_name}")

                # Save StringSession → file .session
                api_id_2fa, api_hash_2fa = get_api()
                session_path = Path(session_path_str) if session_path_str else None
                if session_path:
                    from telethon import TelegramClient as _TC
                    ss_client = _TC(str(session_path), api_id_2fa, api_hash_2fa)
                    ss_client.session._dc_id = client.session._dc_id
                    ss_client.session._server_address = client.session._server_address
                    ss_client.session._port = client.session._port
                    ss_client.session._auth_key = client.session._auth_key
                    await ss_client.connect()
                    await ss_client.disconnect()
                    print(f"💾 Session saved (2FA): {session_path.name}")

                await client.disconnect()
                st["userbot_file"] = session_path
                st["ub_client"] = None
                st["state"] = STATE_WAIT_SESSIONS
                st["counter_msg_id"] = None
                await event.reply(
                    _q(
                        f"✅ Login berhasil! <b>{me.first_name or ''}</b> (@{me.username or '-'})\n\n"
                        "Sekarang upload file <code>.session</code> atau <code>.zip</code> berisi session yang mau disetor.\n"
                        "Boleh kirim berkali-kali."
                    ),
                    parse_mode="html",
                )
            except errors.SessionPasswordNeededError:
                print("🔒 2FA required (password needed)")
                await event.reply(
                    _q("🔒 Masukkan password 2FA:"),
                    buttons=[[Button.inline("❌ Batal", b"cancel_ub_login")]],
                    parse_mode="html",
                )
            except errors.PasswordHashInvalidError:
                print("❌ 2FA password salah")
                await event.reply(
                    _q("❌ Password 2FA salah. Ketik ulang password:"),
                    buttons=[[Button.inline("❌ Batal", b"cancel_ub_login")]],
                    parse_mode="html",
                )
            except Exception as e2:
                err2 = str(e2)
                print(f"❌ 2FA error: {err2}")
                if "password" in err2.lower() or "invalid" in err2.lower():
                    await event.reply(
                        _q("❌ Password 2FA salah. Ketik ulang password:"),
                        buttons=[[Button.inline("❌ Batal", b"cancel_ub_login")]],
                        parse_mode="html",
                    )
                else:
                    await event.reply(
                        _q(f"❌ <code>{err2}</code>\n\nKetik ulang password atau /start untuk batal."),
                        parse_mode="html",
                    )
            return

        if st["state"] == STATE_WAIT_BOT:
            uname = raw if raw.startswith("@") else f"@{raw}"
            st["bot_username"] = uname

            st["state"] = STATE_WAIT_MODE
            await event.reply(
                _q(
                    f"✅ Bot tujuan: <code>{uname}</code>\n\nPilih mode setor:"
                ),
                buttons=[[
                    Button.inline("🔁 Receiver Mode", b"mode_receiver"),
                    Button.inline("💬 Reply Mode",    b"mode_reply"),
                ]],
                parse_mode="html",
            )
            return

        # State lain yang tidak butuh input teks
        await _send_info(bot, chat_id)

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

        # Info ack → lanjut ke welcome
        if data == b"info_ack":
            await event.answer()
            await _send_welcome(bot, chat_id)
            return

        # Stop batch
        if data and data.startswith(b"stop_batch_"):
            uid = int(data.replace(b"stop_batch_", b""))
            se = _stop_events.get(uid)
            if se:
                se.set()
                print(f"🛑 Stop batch requested by user {uid}")
                try:
                    await event.answer("🛑 Menghentikan proses...", alert=True)
                    await event.edit(buttons=[[Button.inline("⏹ Stopping...", b"noop")]])
                except Exception:
                    pass
            else:
                await event.answer("⚠️ Tidak ada proses yang berjalan.", alert=True)
            return

        # Mulai alur setor
        if data == b"start_setor":
            if _is_worker_running(user_id):
                await event.answer("⚠️ Masih ada proses berjalan, tunggu selesai dulu.", alert=True)
                return
            _reset_state(user_id)
            st = _get_state(user_id)
            await _show_userbot_prompt(event, user_id, st)
            return

        # Pakai userbot session yang sudah tersimpan
        if data == b"use_saved_userbot":
            saved = _get_saved_userbot(user_id)
            if not saved:
                await event.answer("Session userbot tidak ditemukan, upload baru.", alert=True)
                await event.edit(
                    _q(
                        "📁 Upload <b>1 file .session sebagai Userbot</b>.\n\n"
                        "<i>(Userbot adalah akun Telegram yang berfungsi sebagai penyetor — "
                        "digunakan untuk komunikasi dengan bot buyer)</i>"
                    ),
                    parse_mode="html",
                )
                return
            st["userbot_file"] = saved
            st["state"] = STATE_WAIT_SESSIONS
            st["counter_msg_id"] = None
            await event.answer()
            await event.edit(
                _q(
                    f"✅ Menggunakan userbot lama: <code>{saved.name}</code>\n\n"
                    "Sekarang upload file <code>.session</code> atau <code>.zip</code> berisi session yang mau disetor.\n"
                    "Boleh kirim berkali-kali."
                ),
                parse_mode="html",
            )
            return

        # Login userbot manual (input nomor → OTP → 2FA)
        if data == b"login_userbot":
            st["state"] = STATE_WAIT_UB_PHONE
            await event.answer()
            await event.edit(
                _q(
                    "🔐 <b>Login Userbot Manual</b>\n\n"
                    "Ketik nomor HP dengan kode negara, contoh:\n"
                    "<code>+6281234567890</code>"
                ),
                buttons=[[Button.inline("❌ Batal", b"cancel_ub_login")]],
                parse_mode="html",
            )
            return

        # Batal login manual userbot
        if data == b"cancel_ub_login":
            client = st.get("ub_client")
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            # Hapus session file yg belum selesai login
            phone = st.get("ub_phone")
            if phone:
                incomplete = _user_userbot_dir(user_id) / f"{phone.lstrip('+')}.session"
                incomplete.unlink(missing_ok=True)
            st["ub_client"] = None
            st["ub_phone"] = None
            st["ub_phone_code_hash"] = None
            _reset_state(user_id)
            st = _get_state(user_id)
            await event.answer()
            await _show_userbot_prompt(event, user_id, st)
            return

        # Kirim ulang OTP saat login manual
        if data == b"resend_otp":
            client = st.get("ub_client")
            phone = st.get("ub_phone")
            if not client or not phone:
                await event.answer("Sesi login expired. Ketik /start untuk ulang.", alert=True)
                _reset_state(user_id)
                return
            try:
                await event.answer("Mengirim ulang OTP...")
                new_result = await client.send_code_request(phone)
                st["ub_phone_code_hash"] = new_result.phone_code_hash
                st["state"] = STATE_WAIT_UB_OTP
                await event.edit(
                    _q(
                        f"📩 OTP baru sudah dikirim ke <b>{phone}</b>.\n\n"
                        "Ketik kode OTP:"
                    ),
                    buttons=[[Button.inline("🔄 Kirim Ulang OTP", b"resend_otp"), Button.inline("❌ Batal", b"cancel_ub_login")]],
                    parse_mode="html",
                )
            except Exception as e:
                await event.answer(f"Gagal kirim OTP: {e}", alert=True)
            return

        # Upload userbot baru (hapus userbot lama dulu)
        if data == b"upload_new_userbot":
            shutil.rmtree(_user_userbot_dir(user_id), ignore_errors=True)
            st["state"] = STATE_WAIT_USERBOT
            await event.answer()
            await event.edit(
                _q(
                    "📁 Upload <b>1 file .session sebagai Userbot</b>.\n\n"
                    "<i>(Userbot adalah akun Telegram yang berfungsi sebagai penyetor — "
                    "digunakan untuk komunikasi dengan bot buyer)</i>"
                ),
                parse_mode="html",
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
                _q(
                    f"✅ <b>{len(st['sessions'])} session</b> siap.\n\n"
                    "Ketik username bot buyer"
                ),
                parse_mode="html",
            )
            return

        # Mulai registrasi World V1 (langsung tanpa input bot/mode)
        if data == b"start_world_v1":
            if st["state"] != STATE_WAIT_SESSIONS:
                await event.answer("Tidak ada sesi upload aktif.", alert=True)
                return
            if not st["sessions"]:
                await event.answer("Belum ada session yang diupload.", alert=True)
                return
            if _is_worker_running(user_id):
                await event.answer("Masih ada proses berjalan.", alert=True)
                return

            st["mode"] = "world_v1"
            st["bot_username"] = WORLD_V1_BOT
            n_sessions = len(st["sessions"])

            st["state"] = STATE_PROCESSING
            await event.answer()
            await event.edit(
                _q(
                    f"🌍 <b>Mode: Registrasi {WORLD_V1_BOT}</b>\n\n"
                    f"🚀 Memulai <b>{n_sessions} session</b> untuk registrasi\n\n"
                    f"<i>Proses berjalan di background. Hasil akan dikirim setelah selesai...</i>"
                ),
                parse_mode="html",
            )

            task = asyncio.create_task(_run_worker(bot, user_id, chat_id))
            _active_workers[user_id] = task
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
                _q(
                    f"✅ Mode: <b>{mode_label}</b>\n\n"
                    f"🚀 Memulai <b>{n_sessions} session</b> → <code>{bot_uname}</code>\n\n"
                    f"<i>Proses berjalan di background. Hasil akan dikirim setelah selesai...</i>"
                ),
                parse_mode="html",
            )

            task = asyncio.create_task(_run_worker(bot, user_id, chat_id))
            _active_workers[user_id] = task
            return

        # Menu Sortir
        if data == b"menu_sortir":
            if st["state"] != STATE_WAIT_SESSIONS:
                await event.answer("Tidak bisa sortir sekarang.", alert=True)
                return
            n = len(st["sessions"])
            await event.answer()
            await event.edit(
                _q(
                    f"📋 <b>Menu Sortir</b> — {n} session\n\n"
                    "Pilih opsi sortir:"
                ),
                buttons=[
                    [Button.inline("🌍 Sortir by Negara", b"sortir_country")],
                    [Button.inline("🔢 Sortir by ID (range)", b"sortir_id")],
                    [Button.inline("🔓 Sortir 2FA OFF", b"sortir_2fa_off")],
                    [Button.inline("🔒 Sortir 2FA ON", b"sortir_2fa_on")],
                    [Button.inline("↩️ Kembali", b"sortir_back")],
                ],
                parse_mode="html",
            )
            return

        # Kembali dari menu sortir
        if data == b"sortir_back":
            st["state"] = STATE_WAIT_SESSIONS
            await event.answer()
            await _update_session_counter(bot, chat_id, user_id)
            return

        # Sortir by negara → prompt input kode negara
        if data == b"sortir_country":
            if st["state"] != STATE_WAIT_SESSIONS:
                await event.answer("Tidak bisa sortir sekarang.", alert=True)
                return
            st["state"] = STATE_WAIT_COUNTRY
            await event.answer()
            await event.edit(
                _q(
                    "🌍 <b>Sortir by Negara</b>\n\n"
                    "Ketik kode negara (angka), contoh:\n"
                    "<code>62</code> = Indonesia\n"
                    "<code>91</code> = India\n"
                    "<code>63</code> = Filipina\n"
                    "<code>66</code> = Thailand\n"
                    "<code>84</code> = Vietnam\n"
                    "<code>60</code> = Malaysia\n\n"
                    "<i>Bisa juga pakai format +62, 62, dll.\n"
                    "Session di luar kode negara yang dipilih akan dihapus dari daftar.</i>"
                ),
                buttons=[[Button.inline("↩️ Batal", b"cancel_sortir")]],
                parse_mode="html",
            )
            return

        # Sortir by ID (range nomor)
        if data == b"sortir_id":
            if st["state"] != STATE_WAIT_SESSIONS:
                await event.answer("Tidak bisa sortir sekarang.", alert=True)
                return
            st["state"] = STATE_WAIT_COUNTRY  # reuse state, different context
            st["_sortir_mode"] = "id"
            await event.answer()
            await event.edit(
                _q(
                    "🔢 <b>Sortir by ID (Range)</b>\n\n"
                    "Ketik range nomor session, contoh:\n"
                    "<code>1-10</code> = ambil session ke-1 sampai ke-10\n"
                    "<code>5-20</code> = ambil session ke-5 sampai ke-20\n"
                    "<code>3</code> = ambil session ke-3 saja\n\n"
                    f"<i>Total {len(st['sessions'])} session saat ini.</i>"
                ),
                buttons=[[Button.inline("↩️ Batal", b"cancel_sortir")]],
                parse_mode="html",
            )
            return

        # Sortir 2FA OFF
        if data == b"sortir_2fa_off":
            if st["state"] != STATE_WAIT_SESSIONS:
                await event.answer("Tidak bisa sortir sekarang.", alert=True)
                return
            n = len(st["sessions"])
            await event.edit(
                _q(f"🔓 Mengecek 2FA pada <b>{n}</b> session...\n<i>Mohon tunggu, ini bisa memakan waktu.</i>"),
                parse_mode="html",
            )
            filtered = await _sortir_2fa(st["sessions"], api_id=None, api_hash=None, want_2fa=False)
            removed = len(st["sessions"]) - len(filtered)
            st["sessions"] = filtered
            st["country_filter"] = "2FA OFF"
            if not filtered:
                await event.edit(
                    _q(f"❌ Tidak ada session dengan <b>2FA OFF</b>.\n{removed} session dihapus."),
                    buttons=[[Button.inline("🔄 Reset Sortir", b"cancel_sortir")]],
                    parse_mode="html",
                )
            else:
                await event.edit(
                    _q(f"🔓 Sortir <b>2FA OFF</b>: <b>{len(filtered)}</b> cocok, <b>{removed}</b> dihapus."),
                    parse_mode="html",
                )
                st["state"] = STATE_WAIT_SESSIONS
                await _update_session_counter(bot, chat_id, user_id)
            return

        # Sortir 2FA ON
        if data == b"sortir_2fa_on":
            if st["state"] != STATE_WAIT_SESSIONS:
                await event.answer("Tidak bisa sortir sekarang.", alert=True)
                return
            n = len(st["sessions"])
            await event.edit(
                _q(f"🔒 Mengecek 2FA pada <b>{n}</b> session...\n<i>Mohon tunggu, ini bisa memakan waktu.</i>"),
                parse_mode="html",
            )
            filtered = await _sortir_2fa(st["sessions"], api_id=None, api_hash=None, want_2fa=True)
            removed = len(st["sessions"]) - len(filtered)
            st["sessions"] = filtered
            st["country_filter"] = "2FA ON"
            if not filtered:
                await event.edit(
                    _q(f"❌ Tidak ada session dengan <b>2FA ON</b>.\n{removed} session dihapus."),
                    buttons=[[Button.inline("🔄 Reset Sortir", b"cancel_sortir")]],
                    parse_mode="html",
                )
            else:
                await event.edit(
                    _q(f"🔒 Sortir <b>2FA ON</b>: <b>{len(filtered)}</b> cocok, <b>{removed}</b> dihapus."),
                    parse_mode="html",
                )
                st["state"] = STATE_WAIT_SESSIONS
                await _update_session_counter(bot, chat_id, user_id)
            return

        # Batal sortir / reset
        if data == b"cancel_sortir":
            st["state"] = STATE_WAIT_SESSIONS
            st["country_filter"] = None
            st.pop("_sortir_mode", None)
            # Reload all sessions from folder
            dest_dir = _user_sessions_dir(user_id)
            if dest_dir.exists():
                st["sessions"] = sorted(dest_dir.glob("*.session"))
            await event.answer()
            await _update_session_counter(bot, chat_id, user_id)
            return

        # Setor lagi
        if data == b"setor_lagi":
            if _is_worker_running(user_id):
                await event.answer("Masih ada proses berjalan.", alert=True)
                return
            _reset_state(user_id)
            st = _get_state(user_id)
            await _show_userbot_prompt(event, user_id, st)
            return

        await event.answer("Tombol tidak dikenal.", alert=True)


# ─── 2FA Sort Helper ─────────────────────────────────────────────────────────

async def _sortir_2fa(sessions: list[Path], api_id, api_hash, want_2fa: bool) -> list[Path]:
    """Filter sessions berdasarkan status 2FA.
    want_2fa=True → hanya yang 2FA ON
    want_2fa=False → hanya yang 2FA OFF
    Connect ke tiap session secara sequential, cek via GetPasswordRequest.
    """
    from telethon import TelegramClient
    from telethon.tl.functions.account import GetPasswordRequest
    from config import get_api

    _api_id, _api_hash = get_api()
    matched: list[Path] = []

    for sp in sessions:
        client = None
        try:
            client = TelegramClient(str(sp), _api_id, _api_hash, device_model="")
            await client.connect()
            # Cek apakah sudah authorized
            if not await client.is_user_authorized():
                continue
            pwd = await client(GetPasswordRequest())
            if pwd.has_password == want_2fa:
                matched.append(sp)
        except Exception:
            pass  # skip session yang gagal connect
        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    return matched


# ─── Problem session ZIP helper ───────────────────────────────────────────────

def _count_failed_sessions(user_id: int) -> dict[str, int]:
    """Hitung jumlah session gagal per kategori dari folder."""
    counts: dict[str, int] = {"2fa_on": 0, "device": 0, "unauth": 0, "rejected": 0, "recovered": 0, "misc": 0}
    folders = {
        "2fa_on":    TWO_FA_ON_DIR    / str(user_id),
        "device":    OTHER_DEVICE_DIR / str(user_id),
        "unauth":    UNAUTH_DIR       / str(user_id),
        "rejected":  REJECTED_DIR     / str(user_id),
        "recovered": RECOVERED_DIR    / str(user_id),
        "misc":      SESSIONS_DIR     / str(user_id),
    }
    for key, folder in folders.items():
        if folder.exists():
            counts[key] = len(list(folder.glob("*.session")))
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
        await bot.send_message(chat_id, _q(f"❌ Setup gagal: <code>{e}</code>\nCoba beberapa saat lagi."), parse_mode="html")
        _cleanup_user_files(user_id)
        _reset_state(user_id)
        return

    # Salin userbot.session ke USERBOT/ root agar get_userbot_clients() bisa baca
    userbot_src: Path = st["userbot_file"]
    userbot_dst = USERBOT_DIR / f"u{user_id}_{userbot_src.name}"
    try:
        shutil.copy(str(userbot_src), str(userbot_dst))
    except Exception as e:
        await bot.send_message(chat_id, _q(f"❌ Gagal copy userbot session: <code>{e}</code>"), parse_mode="html")
        _cleanup_user_files(user_id)
        _reset_state(user_id)
        return

    userbot_clients = await get_userbot_clients(api_id, api_hash, [userbot_dst.name])
    if not userbot_clients:
        await bot.send_message(
            chat_id,
            _q(
                "❌ Gagal load userbot session.\n"
                "Pastikan file <code>.session</code> valid dan sudah pernah login."
            ),
            parse_mode="html",
        )
        _cleanup_user_files(user_id)
        userbot_dst.unlink(missing_ok=True)
        _reset_state(user_id)
        return

    userbot_client = userbot_clients[0]
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
        return _q(
            f"Memproses <b>{total}</b> session ke <code>{bot_username}</code>...\n\n"
            f"Berhasil : <b>{ok}</b>\n"
            f"Gagal    : <b>{fail}</b>\n"
            f"Sisa     : <b>{total - done}</b>\n\n"
            f"<code>[{bar}] {done}/{total}</code>"
        )

    prog_msg     = await bot.send_message(
        chat_id, _make_progress_text(0, 0, total_sessions),
        parse_mode="html",
        buttons=[[Button.inline("🛑 Stop Batch", f"stop_batch_{user_id}")]],
    )
    prog_msg_id  = prog_msg.id
    stop_event   = asyncio.Event()
    _stop_events[user_id] = stop_event
    last_edit    = 0.0

    async def on_progress(ok: int, fail: int, total: int, _session_name: str):
        nonlocal last_edit
        now  = _time.monotonic()
        done = ok + fail
        if now - last_edit < 3.0 and done < total:
            return
        last_edit = now
        try:
            await bot.edit_message(chat_id, prog_msg_id, _make_progress_text(ok, fail, total), parse_mode="html",
                                       buttons=[[Button.inline("🛑 Stop Batch", f"stop_batch_{user_id}")]])
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
                    _q(f"📩 Kirim <b>{phone}</b> → <code>{bot_username}</code>"),
                    parse_mode="html",
                )
                session_msgs[phone] = msg.id

            elif event_type == "otp_sent":
                text = _q(f"🔑 OTP <b>{phone}</b>: <code>{extra}</code> → dikirim ke <code>{bot_username}</code>")
                msg_id = session_msgs.get(phone)
                if msg_id:
                    await bot.edit_message(chat_id, msg_id, text, parse_mode="html")
                else:
                    msg = await bot.send_message(chat_id, text, parse_mode="html")
                    session_msgs[phone] = msg.id

            elif event_type == "rejected":
                text = _q(f"⚠️ <b>{phone}</b> ditolak buyer")
                msg_id = session_msgs.get(phone)
                if msg_id:
                    await bot.edit_message(chat_id, msg_id, text, parse_mode="html")
                else:
                    await bot.send_message(chat_id, text, parse_mode="html")
                session_final.add(phone)

            elif event_type == "success":
                if phone in session_final:
                    return
                text = _q(f"✅ <b>{phone}</b> berhasil disetor")
                msg_id = session_msgs.pop(phone, None)
                if msg_id:
                    await bot.edit_message(chat_id, msg_id, text, parse_mode="html")
                else:
                    await bot.send_message(chat_id, text, parse_mode="html")
                session_final.add(phone)

            elif event_type == "fail":
                if phone in session_final:
                    return
                text = _q(f"❌ <b>{phone}</b> gagal")
                msg_id = session_msgs.pop(phone, None)
                if msg_id:
                    await bot.edit_message(chat_id, msg_id, text, parse_mode="html")
                else:
                    await bot.send_message(chat_id, text, parse_mode="html")

            elif event_type == "recovered":
                text = _q(f"🔄 <b>{phone}</b> reject → berhasil di-recover")
                msg_id = session_msgs.pop(phone, None)
                if msg_id:
                    await bot.edit_message(chat_id, msg_id, text, parse_mode="html")
                else:
                    await bot.send_message(chat_id, text, parse_mode="html")
                session_final.add(phone)

            elif event_type == "already_sold":
                text = _q(f"⏭ <b>{phone}</b> sudah pernah terjual")
                msg_id = session_msgs.pop(phone, None)
                if msg_id:
                    await bot.edit_message(chat_id, msg_id, text, parse_mode="html")
                else:
                    await bot.send_message(chat_id, text, parse_mode="html")
                session_final.add(phone)

            elif event_type == "late_rejected":
                text = _q(f"⚠️ <b>{phone}</b> ditolak buyer")
                msg_id = session_msgs.get(phone)
                if msg_id:
                    try:
                        await bot.edit_message(chat_id, msg_id, text, parse_mode="html")
                    except Exception:
                        await bot.send_message(chat_id, text, parse_mode="html")
                else:
                    await bot.send_message(chat_id, text, parse_mode="html")

            elif event_type == "batch_done":
                pass  # handled by batch summary

        except Exception as e:
            print(f"⚠️ event_cb [{event_type}][{phone}]: {e}")

    try:
        if mode == "world_v1":
            # Flow World V1 — registrasi via @WORLD_V1_FAST_BOT
            result = await run_world_v1_batch(
                admin_client=userbot_client,
                api_id=api_id,
                api_hash=api_hash,
                session_files=sessions,
                progress_cb=on_progress,
                event_cb=event_cb,
                stop_event=stop_event,
            )

            n_success  = (result or {}).get("success", 0)
            n_error    = (result or {}).get("error", 0)
            n_skipped  = (result or {}).get("skipped", 0)
            n_rejected = (result or {}).get("rejected", 0)
            total_proc = total_sessions

            try:
                await bot.delete_messages(chat_id, [prog_msg_id])
            except Exception:
                pass

            summary_lines = [
                f"🌍 <b>Registrasi {WORLD_V1_BOT} Selesai</b>\n\n",
                f"Total        : <b>{total_proc}</b>",
                f"✅ Berhasil    : <b>{n_success}</b>",
                f"❌ Error       : <b>{n_error}</b>",
                f"⏭ Skipped     : <b>{n_skipped}</b>",
                f"🚫 Rejected    : <b>{n_rejected}</b>",
            ]

            if n_error > 0:
                summary_lines.append(f"\n📂 Session error tersimpan di: <code>WORLD_V1/error/</code>")
            if n_rejected > 0:
                summary_lines.append(f"📂 Session rejected: <code>WORLD_V1/rejected/</code>")

            final_text = _q("\n".join(summary_lines))

            await bot.send_message(
                chat_id, final_text,
                buttons=[[Button.inline("🌍 Mulai Registrasi Lagi", b"setor_lagi")]],
                parse_mode="html",
            )

        elif mode == "receiver":
            # Flow receiver — sequential, setor satu per satu (bisa banyak)
            result = await sell_sessions_with_bot(
                api_id=api_id,
                api_hash=api_hash,
                admin_client=userbot_client,
                bot_username=bot_username,
                session_files=sessions,
                progress_cb=on_progress,
                event_cb=event_cb,
                stop_event=stop_event,
            )
        else:
            # Flow reply — parallel, setor banyak sekaligus
            result = await sell_sessions_with_reply_bot(
                api_id=api_id,
                api_hash=api_hash,
                admin_client=userbot_client,
                bot_username=bot_username,
                session_files=sessions,
                max_parallel=REPLY_MAX_PARALLEL,
                progress_cb=on_progress,
                event_cb=event_cb,
                stop_event=stop_event,
            )

        # ── Result processing untuk mode SELAIN world_v1 ──
        if mode != "world_v1":
            n_success      = (result or {}).get("success", 0)
            n_recovered    = (result or {}).get("recovered", 0)
            n_already_sold = (result or {}).get("already_sold", 0)
            n_cancelled    = (result or {}).get("cancelled", 0)
            n_other_fail   = total_sessions - n_success - n_recovered - n_already_sold - n_cancelled

            # Hitung breakdown gagal per kategori (sebelum archive)
            fail_counts = _count_failed_sessions(user_id)

            # Zip recovered + already_sold + cancelled sessions sebelum archive
            zip_recovered     = _zip_recovered_sessions()
            zip_already_sold  = _zip_already_sold_sessions()
            zip_cancelled     = _zip_cancelled_sessions()

            # Archive batch before cleanup
            failed_categories = [
                (TWO_FA_ON_DIR     / str(user_id), "2fa_on"),
                (OTHER_DEVICE_DIR  / str(user_id), "device"),
                (UNAUTH_DIR        / str(user_id), "unauth"),
                (REJECTED_DIR      / str(user_id), "rejected"),
                (RECOVERED_DIR     / str(user_id), "recovered"),
                (ALREADY_SOLD_DIR  / str(user_id), "already_sold"),
                (CANCELLED_DIR     / str(user_id), "cancelled"),
            ]
            _archive_batch(user_id, sessions, failed_categories)

            # Build gagal detail with per-category breakdown
            gagal_detail = ""
            if n_other_fail > 0:
                gagal_parts = []
                if fail_counts.get("2fa_on", 0) > 0:
                    gagal_parts.append(f"2FA ON = {fail_counts['2fa_on']}")
                if fail_counts.get("device", 0) > 0:
                    gagal_parts.append(f"Device lain = {fail_counts['device']}")
                if fail_counts.get("unauth", 0) > 0:
                    gagal_parts.append(f"Unauth = {fail_counts['unauth']}")
                accounted = fail_counts.get("2fa_on", 0) + fail_counts.get("device", 0) + fail_counts.get("unauth", 0) + fail_counts.get("rejected", 0)
                remaining = n_other_fail - accounted
                if remaining > 0:
                    gagal_parts.append(f"Error = {remaining}")
                gagal_detail = "\n  " + "\n  ".join(gagal_parts)

            processed = n_success + n_recovered + n_already_sold + n_other_fail
            header = f"Selesai memproses <b>{total_sessions}</b> session ke <code>{bot_username}</code>."
            if n_cancelled > 0:
                n_other_fail = total_sessions - n_success - n_recovered - n_already_sold - n_cancelled
                if n_other_fail < 0:
                    n_other_fail = 0
                processed = n_success + n_recovered + n_already_sold + n_other_fail
                header = f"⛔ Buyer Penuh — <b>{processed}/{total_sessions}</b> session diproses ke <code>{bot_username}</code>.\nCapacity Indonesia sudah penuh.\n\n"

            summary_lines = [
                f"{header}\n\n",
                f"Berhasil     : <b>{n_success}</b>",
            ]
            if n_already_sold > 0:
                summary_lines.append(f"Sudah pernah di setor : <b>{n_already_sold}</b>")
            summary_lines.append(f"Recovered    : <b>{n_recovered}</b>")
            summary_lines.append(f"Gagal        : <b>{n_other_fail}</b>{gagal_detail}")
            if n_cancelled > 0:
                summary_lines.append(f"Dibatalkan   : <b>{n_cancelled}</b>")

            final_text = _q("\n".join(summary_lines))

            try:
                await bot.delete_messages(chat_id, [prog_msg_id])
            except Exception:
                pass
            await bot.send_message(
                chat_id, final_text,
                buttons=[[Button.inline("🔄 Setor Lagi", b"setor_lagi")]],
                parse_mode="html",
            )

            # Kirim zip recovered ke user
            if zip_recovered and zip_recovered.exists():
                try:
                    await bot.send_file(
                        chat_id,
                        str(zip_recovered),
                        caption=_q(f"📦 <b>{n_recovered}</b> session berhasil di-recover\n"
                                   f"Akun ini di-reject buyer tapi berhasil diambil alih kembali."),
                        parse_mode="html",
                    )
                    zip_recovered.unlink(missing_ok=True)
                except Exception as e:
                    print(f"⚠️ Gagal kirim zip recovered: {e}")

            # Kirim zip already_sold ke user
            if zip_already_sold and zip_already_sold.exists():
                try:
                    await bot.send_file(
                        chat_id,
                        str(zip_already_sold),
                        caption=_q(f"📦 <b>{n_already_sold}</b> session sudah pernah terjual\n"
                                   f"Akun ini sudah pernah dijual sebelumnya."),
                        parse_mode="html",
                    )
                    zip_already_sold.unlink(missing_ok=True)
                except Exception as e:
                    print(f"⚠️ Gagal kirim zip already_sold: {e}")

            # Kirim zip cancelled ke user
            if zip_cancelled and zip_cancelled.exists():
                try:
                    await bot.send_file(
                        chat_id,
                        str(zip_cancelled),
                        caption=_q(f"📦 <b>{n_cancelled}</b> session dibatalkan\n"
                                   f"Session ini belum diproses dan bisa dipakai lagi."),
                        parse_mode="html",
                    )
                    zip_cancelled.unlink(missing_ok=True)
                except Exception as e:
                    print(f"⚠️ Gagal kirim zip cancelled: {e}")

    except Exception as e:
        await bot.send_message(
            chat_id,
            _q(
                f"❌ Error tak terduga saat proses:\n<code>{e}</code>\n\n"
                "Silakan coba lagi."
            ),
            buttons=[[Button.inline("🔄 Coba Lagi", b"setor_lagi")]],
            parse_mode="html",
        )

    finally:
        userbot_dst.unlink(missing_ok=True)
        _cleanup_user_files(user_id)
        _reset_state(user_id)
        _active_workers.pop(user_id, None)
        _stop_events.pop(user_id, None)
