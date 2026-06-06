# engine/otp_listener.py
# Listener OTP dari 777000 pada session yang dijual

import asyncio
from datetime import datetime, timezone, timedelta
from telethon import events


async def wait_for_otp_from_777000(client, timeout: int = 90) -> str | None:
    """
    Listen OTP dari 777000. Ambil realtime OTP yang baru masuk.
    """
    loop = asyncio.get_event_loop()
    got: asyncio.Future = loop.create_future()
    start = datetime.now(timezone.utc) - timedelta(seconds=2)

    @client.on(events.NewMessage(from_users=777000))
    async def handler(event):
        try:
            code = "".join(filter(str.isdigit, event.raw_text or ""))
            if code and not got.done():
                got.set_result(code)
                print(f"✅ OTP ketangkep realtime: {code}")
        except Exception as e:
            if not got.done():
                got.set_result(None)
            print(f"❌ Error handler OTP: {e}")

    try:
        return await asyncio.wait_for(got, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        client.remove_event_handler(handler, events.NewMessage)


async def get_otp_from_history(client, limit: int = 5) -> str | None:
    """
    Ambil OTP terbaru dari history chat 777000.
    """
    try:
        async for m in client.iter_messages(777000, limit=limit):
            code = "".join(filter(str.isdigit, m.message or ""))
            if code:
                print(f"📜 OTP fallback dari history: {code} ({m.date})")
                return code
    except Exception as e:
        print(f"⚠️ Error cek history OTP: {e}")
    return None
