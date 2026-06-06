# sell.py
# WSL Seller — Console (gabungan receiversell.py + replysetor.py)
# Jalankan: python sell.py

import asyncio
import sys
from pathlib import Path

from config import (
    ensure_dirs,
    BOT_BUYERS,
    BOT_BUYERS_REPLY,
    ADMIN_DIR,
    SESSIONS_DIR,
    get_api,
    REPLY_MAX_PARALLEL,
)
from engine.admin_session import create_admin_session_interactive, get_admin_clients
from engine.seller import (
    sell_sessions_with_bot,
    sell_sessions_with_reply_bot,
)
from utils.file_manager import list_sessions
from utils.logger import init_logs

BANNER = """
=============================
   🚀 WSL Seller — Console
=============================
1. Buat Session Admin
2. JUAL AKUN
0. Keluar
"""


def prompt(msg: str) -> str:
    try:
        return input(msg)
    except KeyboardInterrupt:
        print("\n❌ Dibatalkan.")
        sys.exit(0)


def menu_choose_admin_session() -> str:
    admins = sorted([p.name for p in ADMIN_DIR.glob("*.session")])
    if not admins:
        print("❌ Belum ada admin session. Buat dulu di menu (1).")
        return ""
    print("\n📁 Admin Sessions:")
    for i, s in enumerate(admins, 1):
        print(f"{i}. {s}")
    idx = prompt("Pilih admin session [nomor]: ").strip()
    if not idx.isdigit():
        print("❌ Input invalid.")
        return ""
    idx = int(idx)
    if idx < 1 or idx > len(admins):
        print("❌ Pilihan tidak ada.")
        return ""
    return admins[idx - 1]


def menu_choose_bot(mode: str) -> str:
    bot_list = BOT_BUYERS_REPLY if mode == "2" else BOT_BUYERS
    label = "Bot Buyer (Reply Mode)" if mode == "2" else "Bot Buyer (Receiver Mode)"
    print(f"\n🤖 {label}:")
    for i, b in enumerate(bot_list, 1):
        print(f"{i}. {b}")
    idx = prompt("Pilih bot buyer [nomor]: ").strip()
    if not idx.isdigit():
        print("❌ Input invalid.")
        return ""
    idx = int(idx)
    if idx < 1 or idx > len(bot_list):
        print("❌ Pilihan tidak ada.")
        return ""
    return bot_list[idx - 1]


def menu_choose_sessions_to_sell() -> list[Path]:
    sessions = list_sessions(SESSIONS_DIR)
    if not sessions:
        print("❌ Tidak ada file .session di ./SESSIONS")
        return []
    print("\n📦 Sessions untuk dijual:")
    for i, s in enumerate(sessions, 1):
        print(f"{i}. {s.name}")
    ok = prompt("Jual semua di atas? (y/n): ").strip().lower()
    if ok == "y":
        return sessions
    picks = prompt("Masukkan nomor (pisah koma), atau kosong untuk batal: ").strip()
    if not picks:
        return []
    selected = []
    for p in picks.split(","):
        p = p.strip()
        if p.isdigit():
            i = int(p)
            if 1 <= i <= len(sessions):
                selected.append(sessions[i - 1])
    return selected


def menu_choose_mode() -> str:
    """
    1 = Receiver (sequential)
    2 = Reply (parallel)
    """
    print("\n🎛 Mode Buyer:")
    print("1. Receiver (sequential, satu per satu)")
    print("2. Reply (parallel, banyak sekaligus)")
    mode = prompt("Pilih mode [1/2]: ").strip()
    if mode not in ("1", "2"):
        print("❌ Mode tidak dikenal.")
        return ""
    return mode


async def main():
    ensure_dirs()
    init_logs()
    api_id, api_hash = get_api()

    while True:
        print(BANNER)
        choice = prompt("Pilih menu: ").strip()

        if choice == "1":
            print("📱 Buat Session Admin")
            session_label = prompt("Nama file session admin (tanpa .session): ").strip()
            if not session_label:
                print("❌ Nama tidak boleh kosong.")
                continue
            phone = prompt("Nomor HP admin (format +62xxxx): ").strip()
            if not phone.startswith("+"):
                print("❌ Format nomor harus termasuk kode negara, contoh +62...")
                continue
            await create_admin_session_interactive(api_id, api_hash, session_label, phone)

        elif choice == "2":
            mode = menu_choose_mode()
            if not mode:
                continue

            selected_admin = menu_choose_admin_session()
            if not selected_admin:
                continue

            bot_username = menu_choose_bot(mode)
            if not bot_username:
                continue

            targets = menu_choose_sessions_to_sell()
            if not targets:
                print("❌ Tidak ada target.")
                continue

            admin_clients = await get_admin_clients(api_id, api_hash, [selected_admin])
            if not admin_clients:
                print("❌ Gagal open admin session.")
                continue

            admin_client = admin_clients[0]
            mode_label = "Receiver (Sequential)" if mode == "1" else "Reply (Parallel)"
            print(f"\n🚀 Mulai jual | Mode={mode_label} | Bot={bot_username} | Session={len(targets)}")

            if mode == "1":
                # Receiver mode — sequential, bisa banyak session
                await sell_sessions_with_bot(
                    api_id=api_id,
                    api_hash=api_hash,
                    admin_client=admin_client,
                    bot_username=bot_username,
                    session_files=targets,
                )
            else:
                # Reply mode — parallel sekaligus
                await sell_sessions_with_reply_bot(
                    api_id=api_id,
                    api_hash=api_hash,
                    admin_client=admin_client,
                    bot_username=bot_username,
                    session_files=targets,
                    max_parallel=REPLY_MAX_PARALLEL,
                )

        elif choice == "0":
            print("👋 Bye.")
            break
        else:
            print("❌ Menu tidak dikenal.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n❌ Dibatalkan oleh user.")
