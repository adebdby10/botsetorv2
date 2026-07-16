# bot/main.py
# Telegram Bot — fungsi utama, dipanggil dari run.py

import asyncio
import random

from telethon import TelegramClient

from config import ensure_dirs, get_api, BOT_TOKEN, BOT_SESSION, ROOT, get_next_proxy
from bot.handler import register_handlers
from utils.logger import init_logs

SESSION_PATH = str(ROOT / BOT_SESSION)


async def main():
    ensure_dirs()
    init_logs()
    api_id, api_hash = get_api()

    # Coba konek pake proxy dari DataImpulse
    proxy = get_next_proxy()
    if proxy:
        print(f"🌐 Proxy ENABLED: {proxy[1]}:{proxy[2]}")
    else:
        print("⚠️ No proxy available, connecting directly...")

    print("🤖 Menghubungkan bot...")
    bot = TelegramClient(SESSION_PATH, api_id, api_hash, proxy=proxy)

    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    print(f"✅ Bot online: @{me.username} (ID: {me.id})")

    register_handlers(bot)

    print("📡 Mendengarkan pesan... (Ctrl+C untuk berhenti)")
    await bot.run_until_disconnected()
