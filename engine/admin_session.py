# engine/admin_session.py
# Login & load userbot sessions (untuk komunikasi dengan bot buyer)

import asyncio
from pathlib import Path
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from config import USERBOT_DIR


async def create_userbot_session_interactive(
    api_id: int, api_hash: str, session_label: str, phone: str
):
    """
    Buat session userbot di ./USERBOT/<session_label>.session
    Login interaktif (input code/password di console).
    """
    session_path = USERBOT_DIR / f"{session_label}.session"
    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        print(f"📩 Kirim OTP ke {phone} ...")
        try:
            await client.send_code_request(phone)
        except Exception as e:
            print(f"❌ Gagal send_code_request: {e}")
            await client.disconnect()
            return

        code = input("Masukkan OTP (5/6 digit): ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            pwd = input("Password 2FA: ").strip()
            await client.sign_in(password=pwd)
        except Exception as e:
            print(f"❌ Gagal sign_in: {e}")
            await client.disconnect()
            return

    me = await client.get_me()
    print(f"✅ Userbot login: {me.id} ({me.username or me.first_name})")
    await client.disconnect()


async def get_userbot_clients(api_id: int, api_hash: str, session_files: list[str]):
    """
    Open multiple userbot clients (return list of connected clients).
    session_files: list nama file .session (di ./USERBOT).
    """
    clients = []
    for name in session_files:
        sp = USERBOT_DIR / name
        if not sp.exists():
            print(f"❌ Userbot session tidak ditemukan: {name}")
            continue
        c = TelegramClient(str(sp), api_id, api_hash)
        try:
            await c.connect()
            if not await c.is_user_authorized():
                print(f"❌ Userbot session belum authorized: {name}")
                await c.disconnect()
                continue
            clients.append(c)
            print(f"✅ Userbot siap: {name}")
        except Exception as e:
            print(f"❌ Gagal open userbot {name}: {e}")
    return clients
