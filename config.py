# config.py

from pathlib import Path

# 📌 API Telegram Desktop (hardcode) #GANTI SESUAI CUSTOMER (ANGGA, ANSAR, MEGA dsb)
API_ID = 5214566
API_HASH = "03ee5a4be9848535eb9aace996f5202d"

# Directories
ROOT = Path(__file__).parent.resolve()
ADMIN_DIR        = ROOT / "ADMIN"
SESSIONS_DIR     = ROOT / "SESSIONS"
SOLD_DIR         = ROOT / "SOLD"
TWO_FA_ON_DIR    = ROOT / "2FA_ON"
OTHER_DEVICE_DIR = ROOT / "OTHER_DEVICE"
UNAUTH_DIR       = ROOT / "UNAUTH"
REJECTED_DIR     = ROOT / "REJECTED"

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
WARMUP_DELAY_MIN  = 4    # detik min jeda setelah connect, sebelum API call pertama
WARMUP_DELAY_MAX  = 10   # detik max
TASK_STAGGER_MIN  = 2    # detik min jeda antar launch task di reply mode
TASK_STAGGER_MAX  = 5    # detik max


def ensure_dirs():
    for d in [ADMIN_DIR, SESSIONS_DIR, SOLD_DIR, TWO_FA_ON_DIR, OTHER_DEVICE_DIR, UNAUTH_DIR, REJECTED_DIR]:
        d.mkdir(exist_ok=True)


def get_api() -> tuple[int, str]:
    """Langsung return API Telegram Desktop (hardcoded)."""
    return API_ID, API_HASH
