# config.py

from pathlib import Path

# 📌 API Telegram Desktop (hardcode) #GANTI SESUAI CUSTOMER (ANGGA, ANSAR, MEGA dsb)
API_ID = 5214566
API_HASH = "03ee5a4be9848535eb9aace996f5202d"

# Directories
ROOT = Path(__file__).parent.resolve()
USERBOT_DIR      = ROOT / "USERBOT"
SESSIONS_DIR     = ROOT / "SESSIONS"
SOLD_DIR         = ROOT / "SOLD"
TWO_FA_ON_DIR    = ROOT / "2FA_ON"
OTHER_DEVICE_DIR = ROOT / "OTHER_DEVICE"
UNAUTH_DIR       = ROOT / "UNAUTH"
REJECTED_DIR     = ROOT / "REJECTED"
RECOVERED_DIR    = ROOT / "RECOVERED"
ALREADY_SOLD_DIR = ROOT / "ALREADY_SOLD"
CANCELLED_DIR    = ROOT / "CANCELLED"
ARCHIVE_DIR      = ROOT / "ARCHIVE"

# World V1 (Registrasi khusus @WORLD_V1_FAST_BOT)
WORLD_V1_BOT     = "@WORLD_V1_FAST_BOT"
WORLD_V1_DIR     = ROOT / "WORLD_V1"

# Grace period setelah buyer bilang "Successfully" sebelum logout.
# Buyer kadang kirim reject belakangan (late rejection).
# 60 detik cukup untuk deteksi late reject.
GRACE_PERIOD_SECONDS = 40

# Default bot buyers (receiver mode / flow biasa)
BOT_BUYERS = [
    "@GencuReceiver_bot",
    "@ax_Global1Bot",
    "@CNTReceiver2_bot",
    "@Power_Receiver10bot",
    "@JIAVirtualBot",
    "@XrReceiver4_bot",
    "@Ax_GlobalBot",
    "@Power_Receiver10bot",
    "@tgsipshopBot",
    "@lawasglobal_bot"
]

# Bot buyer yang pakai mode REPLY (OTP harus di-reply ke message)
# ➜ bebas lo isi sendiri sesuai buyer:
BOT_BUYERS_REPLY = [
    #contoh:
    "@CNTReceiver2_bot",
    "@ax_Global1Bot",
    "@JIAVirtualBot",
    "@XrReceiver4_bot",
    "@Ax_GlobalBot",
    "@Power_Receiver10bot",
    "@GencuReceiver_bot",
    "@tgsipshopBot",
    "@lawasglobal_bot"
]

# Max parallel untuk reply-mode (bisa diubah sesuai selera)
REPLY_MAX_PARALLEL = 10

# Telegram Bot (token dari BotFather)
BOT_TOKEN   = "8106722859:AAEnpXFu-eDH2yqLVqV3MpDwC1FHc0Il7NM"
BOT_SESSION = "tgbot"          # nama file session cache untuk bot token

# Akses — kosong = semua user boleh; isi user_id untuk whitelist
ALLOWED_USERS: list[int] = []

# Anti-freeze: perlindungan agar session tidak di-freeze Telegram
# Alasan: session dibuat di satu IP/device, lalu langsung dieksekusi dari
# IP/device server yang berbeda → Telegram deteksi rapid-action → freeze.
WARMUP_DELAY_MIN  = 2    # detik min jeda setelah connect, sebelum API call pertama
WARMUP_DELAY_MAX  = 5   # detik max
TASK_STAGGER_MIN  = 1    # detik min jeda antar launch task di reply mode
TASK_STAGGER_MAX  = 3    # detik max

# ── Proxy ──────────────────────────────────────────
# Format proxies.txt: user:pass@host:port
# Port dari range 10001-19999 akan dipakai PER-SESSION secara dinamis.
PROXY_FILE = ROOT / "proxies.txt"

# (user, pwd, host) — di-load sekali dari baris pertama proxies.txt
PROXY_CREDENTIALS: tuple[str, str, str] | None = None
_proxy_index = 0


def _load_proxy_credentials() -> bool:
    """Ambil credentials proxy dari baris pertama proxies.txt."""
    global PROXY_CREDENTIALS
    if PROXY_CREDENTIALS is not None:
        return True
    if not PROXY_FILE.exists():
        return False
    for line in PROXY_FILE.read_text().strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            creds, host_port = line.split("@")
            user, pwd = creds.split(":")
            host, port = host_port.split(":")
            PROXY_CREDENTIALS = (user, pwd, host)
            return True
        except Exception:
            continue
    return False


def get_next_proxy():
    """Dynamic proxy per-session. Port dari range 10001-19999.
    Returns Telethon-compatible proxy tuple: ('socks5', host, port, rdns, username, password)
    """
    global _proxy_index
    if not _load_proxy_credentials():
        return None
    user, pwd, host = PROXY_CREDENTIALS
    port = 10001 + (_proxy_index % 9999)
    _proxy_index += 1
    return ('socks5', host, port, True, user, pwd)


def ensure_dirs():
    for d in [USERBOT_DIR, SESSIONS_DIR, SOLD_DIR, TWO_FA_ON_DIR, OTHER_DEVICE_DIR, UNAUTH_DIR, REJECTED_DIR, RECOVERED_DIR, ALREADY_SOLD_DIR, CANCELLED_DIR, WORLD_V1_DIR]:
        d.mkdir(exist_ok=True)


def get_api() -> tuple[int, str]:
    """Langsung return API Telegram Desktop (hardcoded)."""
    return API_ID, API_HASH
