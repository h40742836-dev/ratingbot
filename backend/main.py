import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DATABASE_URL)
    yield
    await app.state.pool.close()


app = FastAPI(lifespan=lifespan)


def verify_init_data(init_data: str, max_age_sec: int = 86400) -> dict:
    """Проверяет подпись Телеграма и возвращает данные пользователя.
    Без этой проверки любой мог бы подделать чужой профиль."""
    if not init_data:
        raise HTTPException(401, "Открой приложение через бота в Телеграме")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Нет подписи")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Неверная подпись")
    if time.time() - int(pairs.get("auth_date", 0)) > max_age_sec:
        raise HTTPException(401, "Сессия устарела, открой приложение заново")

    return json.loads(pairs["user"])


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "miniapp" / "index.html")


@app.get("/api/me")
async def me(x_init_data: str = Header(default="")):
    tg_user = verify_init_data(x_init_data)

    row = await app.state.pool.fetchrow(
        """SELECT u.nickname, u.public_id, u.role, u.mmr, u.peak_mmr,
                  u.is_calibrated, u.calibration_games,
                  s.total_games, s.wins, s.losses, r.name AS rank_name
           FROM users u
           JOIN user_stats s ON s.user_id = u.id
           LEFT JOIN ranks r ON r.code = u.rank_code
           WHERE u.tg_id = $1""",
        tg_user["id"],
    )
    if not row:
        raise HTTPException(404, "Ты ещё не зарегистрирован. Напиши боту /start")

    games = row["total_games"]
    return {
        "nickname": row["nickname"],
        "public_id": row["public_id"],
        "role": row["role"],
        "is_calibrated": row["is_calibrated"],
        "calibration_games": row["calibration_games"],
        # MMR и звание скрыты до конца калибровки
        "mmr": row["mmr"] if row["is_calibrated"] else None,
        "peak_mmr": row["peak_mmr"] if row["is_calibrated"] else None,
        "rank_name": row["rank_name"] if row["is_calibrated"] else None,
        "total_games": games,
        "wins": row["wins"],
        "losses": row["losses"],
        "winrate": round(row["wins"] / games * 100) if games else None,
    }

from backend.tournaments import router  # noqa: E402
app.include_router(router)
