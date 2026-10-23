import asyncio
import os
import ssl

import aiohttp
import certifi
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
_tasks = set()


def make_session():
    """Соединение с Telegram со списком сертификатов certifi.
    Без этого на некоторых Mac запросы молча падают с ошибкой сертификата."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10), connector=aiohttp.TCPConnector(ssl=ctx)
    )


async def send_message(tg_id, text):
    """Личное сообщение игроку от бота. Ошибки (например, игрок заблокировал бота) не ломают приложение."""
    if not tg_id:
        return
    try:
        async with make_session() as s:
            async with s.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": tg_id, "text": text},
            ) as r:
                data = await r.json()
                if not data.get("ok"):
                    print("уведомление не доставлено:", tg_id, data.get("description"))
    except Exception as e:
        print("ошибка уведомления:", repr(e))


def fire(notes):
    """Отправляет уведомления в фоне, чтобы не задерживать ответ окну. notes = [(tg_id, текст), ...]"""
    notes = [n for n in (notes or []) if n and n[0]]
    if not notes:
        return

    async def _run():
        await asyncio.gather(*(send_message(t, x) for t, x in notes))

    task = asyncio.create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
