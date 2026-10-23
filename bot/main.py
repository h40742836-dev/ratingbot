import asyncio
import os
import random
import sys
from datetime import date, timedelta
from html import escape
from pathlib import Path
from urllib.parse import quote

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message,
                           WebAppInfo)
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # чтобы видеть папку backend
from backend.engine import score_info  # noqa: E402

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_TG_IDS", "").split(",") if x.strip()}

REGISTER_BONUS = 100
REF_FIRST_N, REF_FIRST_BONUS, REF_NEXT_BONUS = 10, 10, 5
CALIBRATION_GAMES = 5

router = Router()


async def register_user(pool, tg_user, ref_tg_id):
    """Создаёт игрока. Возвращает (public_id, is_new, referrer_info)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Блокировка, чтобы два человека одновременно не получили один ID
            await conn.execute("SELECT pg_advisory_xact_lock(1)")

            existing = await conn.fetchrow(
                "SELECT public_id FROM users WHERE tg_id = $1", tg_user.id
            )
            if existing:
                return existing["public_id"], False, None

            # ID новым игрокам: случайный свободный от 0011 до 9999
            # (001-005 остаются у первых пользователей, 0006-0010 свободны для ручной выдачи)
            taken = {r["public_id"] for r in await conn.fetch("SELECT public_id FROM users")}
            free = [f"{n:04d}" for n in range(11, 10000) if f"{n:04d}" not in taken]
            if not free:
                raise RuntimeError("Свободные ID закончились")
            public_id = random.choice(free)

            referrer = None
            if ref_tg_id and ref_tg_id != tg_user.id:
                referrer = await conn.fetchrow(
                    "SELECT id, tg_id FROM users WHERE tg_id = $1", ref_tg_id
                )

            role = "admin" if tg_user.id in ADMIN_IDS else "player"
            nickname = tg_user.full_name[:64] or "Player"

            user_id = await conn.fetchval(
                """INSERT INTO users (tg_id, tg_username, nickname, public_id, role, referred_by)
                   VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                tg_user.id, tg_user.username, nickname, public_id, role,
                referrer["id"] if referrer else None,
            )
            await conn.execute("INSERT INTO user_stats (user_id) VALUES ($1)", user_id)
            await conn.execute(
                """INSERT INTO mmr_log (user_id, delta, reason, note)
                   VALUES ($1, $2, 'register', 'Бонус за регистрацию')""",
                user_id, REGISTER_BONUS,
            )

            ref_info = None
            if referrer:
                count = await conn.fetchval(
                    "SELECT count(*) FROM referrals WHERE referrer_id = $1", referrer["id"]
                )
                bonus = REF_FIRST_BONUS if count < REF_FIRST_N else REF_NEXT_BONUS
                await conn.execute(
                    "INSERT INTO referrals (referred_id, referrer_id, bonus_mmr) VALUES ($1, $2, $3)",
                    user_id, referrer["id"], bonus,
                )
                await conn.execute(
                    """UPDATE users SET mmr = mmr + $1,
                       peak_mmr = GREATEST(peak_mmr, mmr + $1) WHERE id = $2""",
                    bonus, referrer["id"],
                )
                await conn.execute(
                    """INSERT INTO mmr_log (user_id, delta, reason, note)
                       VALUES ($1, $2, 'referral', $3)""",
                    referrer["id"], bonus, f"Приглашён игрок {nickname}",
                )
                ref_info = {"tg_id": referrer["tg_id"], "bonus": bonus}

            return public_id, True, ref_info


CHAT_URL = os.getenv("CHAT_URL", "https://t.me/+H759BW1rjXVlZmY8")   # чат, где объявляют турниры
CHAT_OF = f'<a href="{CHAT_URL}">имхо чата</a>'
CHAT_IN = f'<a href="{CHAT_URL}">имхо чат</a>'
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
SUPPORT_ID = os.getenv("SUPPORT_ID", "8995165459")   # аккаунт поддержки (Telegram ID)
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/+U0PTZgYVaac1MGJk")   # наш ТГК
CHANNEL_LINK = f'<a href="{CHANNEL_URL}">наш ТГК</a>'
CHAT_NOTE = (f"Чтобы повысить рейтинг, зайди в {CHAT_IN} и дождись ближайшего события.\n\n"
             f"А сыграть бои можно здесь: {CHANNEL_LINK} и {CHAT_IN}")
ABOUT_TEXT = (
    f"MMR.Imho-project: официальный проект {CHAT_OF}.\n\n"
    "• ранги от Статиста до S+, у каждого свой значок\n"
    "• турниры PB, BP и PBP с сеткой\n"
    "• топ игроков, сравнение и личная статистика\n"
    "• титулы, «огонёк» и оценка профиля\n\n"
    f"{CHAT_NOTE}\n\n"
    "Нажми /start, чтобы открыть меню."
)


async def webapp_url(bot: Bot):
    """Адрес окна берём из настройки Menu Button в BotFather (запасной вариант: WEBAPP_URL в .env)."""
    url = None
    try:
        mb = await bot.get_chat_menu_button()
        url = getattr(getattr(mb, "web_app", None), "url", None)
    except Exception:
        pass
    return url or os.getenv("WEBAPP_URL")


def _with_tab(url, tab):
    if not tab:
        return url
    return url + ("&" if "?" in url else "?") + "tab=" + tab


async def main_menu(bot: Bot):
    """Главное меню: большие кнопки под сообщением, как в других ботах."""
    url = await webapp_url(bot)
    rows = []

    def app_btn(text, tab=None, primary=False):
        kwargs = {"text": text, "web_app": WebAppInfo(url=_with_tab(url, tab))}
        if primary:
            kwargs["style"] = "primary"      # синяя кнопка (цветные кнопки Telegram, Bot API 9.4+)
        return InlineKeyboardButton(**kwargs)

    if url:
        rows.append([app_btn("Открыть рейтинг", primary=True)])
    rows.append([InlineKeyboardButton(text="Мой профиль", callback_data="profile"),
                 InlineKeyboardButton(text="О проекте", callback_data="about")])
    rows.append([InlineKeyboardButton(text="Поддержка", callback_data="support")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def is_banned(pool, tg_id):
    row = await pool.fetchrow("SELECT is_banned, ban_reason FROM users WHERE tg_id = $1", tg_id)
    if row and row["is_banned"]:
        why = f"\nПричина: {row['ban_reason']}" if row["ban_reason"] else ""
        return "Твой аккаунт заблокирован." + why
    return None


async def is_blocked(message: Message, pool) -> bool:
    text = await is_banned(pool, message.from_user.id)
    if text:
        await message.answer(text)
        return True
    return False


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, pool, bot: Bot):
    if await is_blocked(message, pool):
        return
    # Приглашения друзей отключены: ссылки вида ?start=ref_... больше не дают бонусов
    public_id, is_new, ref_info = await register_user(pool, message.from_user, None)
    name = escape(message.from_user.first_name or "")

    if is_new:
        text = (
            f"Привет, {name}! Это MMR.Imho-project, официальный проект {CHAT_OF}.\n\n"
            f"Твой ID: {public_id}\n"
            f"Тебе начислено +{REGISTER_BONUS} MMR. После {CALIBRATION_GAMES} калибровочных игр "
            f"ты получишь ранг.\n\n"
            f"{CHAT_NOTE}"
        )
    else:
        text = f"С возвращением, {name}! Твой ID: {public_id}\n\n{CHAT_NOTE}"
    await message.answer(text, reply_markup=await main_menu(bot),
                         parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)


@router.message(Command("menu"))
async def cmd_menu(message: Message, pool, bot: Bot):
    if await is_blocked(message, pool):
        return
    await message.answer("Выбирай, что открыть:", reply_markup=await main_menu(bot))


async def build_profile_text(pool, tg_id):
    row = await pool.fetchrow(
        """SELECT u.id, u.nickname, u.public_id, u.mmr, u.potential_mmr, u.is_calibrated,
                  u.calibration_games, u.favorite_character, r.name AS rank_name,
                  s.total_games, s.wins, s.losses, s.win_streak, s.best_win_streak,
                  s.fire_weeks, s.last_fire_week,
                  (SELECT t.name FROM user_titles ut JOIN titles t ON t.id = ut.title_id
                   WHERE ut.user_id = u.id AND ut.is_active ORDER BY ut.awarded_at DESC LIMIT 1) AS title_name
           FROM users u
           JOIN user_stats s ON s.user_id = u.id
           LEFT JOIN ranks r ON r.code = u.rank_code
           WHERE u.tg_id = $1""",
        tg_id,
    )
    if not row:
        return None

    uid = row["id"]
    last100 = await pool.fetch(
        "SELECT is_winner FROM match_players WHERE user_id = $1 ORDER BY match_id DESC LIMIT 100", uid
    )
    quiz = await pool.fetchrow(
        """SELECT f.games, f.wins FROM user_format_stats f JOIN formats ff ON ff.id = f.format_id
           WHERE f.user_id = $1 AND ff.code = 'QUIZ'""", uid)
    sc = await score_info(pool, uid)

    def rate(rows):
        return f"{sum(r['is_winner'] for r in rows) * 100 / len(rows):.0f}%" if rows else "—"

    games = row["total_games"]
    wr = f"{row['wins'] / games * 100:.0f}%" if games else "—"
    monday = date.today() - timedelta(days=date.today().weekday())
    alive = row["last_fire_week"] and row["last_fire_week"] >= monday - timedelta(days=7)
    fire = row["fire_weeks"] if alive else 0

    if row["is_calibrated"]:
        top = f"MMR: {row['mmr']}"
    else:
        top = f"Калибровка: {row['calibration_games']}/{CALIBRATION_GAMES} · MMR: {row['mmr']}"
    pot = row["potential_mmr"]
    score = "—"
    if sc["score"] is not None:
        score = f"{sc['score']}/10 ({sc['percent']}%)" if sc["percent"] is not None else f"{sc['score']}/10"
    lines = [
        row["nickname"], f"ID: {row['public_id']}", "", top,
        f"Боёв: {games} · W/L {row['wins']}/{row['losses']}",
        f"Общий Win rate: {wr}",
        f"Win rate (10): {rate(last100[:10])}",
        f"Win rate (100): {rate(last100)}",
    ]
    if row["is_calibrated"]:
        lines.append(f"Ранг: {row['rank_name'] or '—'}")
    lines += [
        f"Потенциальный MMR: {pot if pot is not None else '—'}",
        f"Текущая серия побед: {row['win_streak']}",
        f"Макс. серия побед: {row['best_win_streak']}",
        f"Огонёк (недель): {fire}",
        f"Всего викторин: {quiz['games'] if quiz else 0}",
        f"Побед в викторинах: {quiz['wins'] if quiz else 0}",
        f"Титул: {row['title_name'] or 'отсутствует'}",
        f"Любимый персонаж: {row['favorite_character'] or '—'}",
        f"Оценка профиля: {score}",
    ]
    return "\n".join(lines)


@router.message(Command("profile"))
async def cmd_profile(message: Message, pool, bot: Bot):
    if await is_blocked(message, pool):
        return
    text = await build_profile_text(pool, message.from_user.id)
    if text is None:
        await message.answer("Ты ещё не зарегистрирован. Нажми /start")
        return
    await message.answer(text, reply_markup=await main_menu(bot))


def back_kb(extra=None):
    """Клавиатура экрана с кнопкой «Назад» (возврат в главное меню)."""
    rows = [list(extra)] if extra else []
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_screen(cb: CallbackQuery, text: str, kb, html: bool = False):
    """Меняет текущее сообщение на нужный экран, чтобы чат не засорялся новыми сообщениями."""
    opts = {"parse_mode": ParseMode.HTML, "link_preview_options": NO_PREVIEW} if html else {}
    try:
        await cb.message.edit_text(text, reply_markup=kb, **opts)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await cb.message.answer(text, reply_markup=kb, **opts)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await show_screen(cb, "Выбирай, что открыть:", await main_menu(bot))


@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery, pool):
    banned = await is_banned(pool, cb.from_user.id)
    if banned:
        await cb.answer(banned, show_alert=True)
        return
    await cb.answer()
    text = await build_profile_text(pool, cb.from_user.id) or "Ты ещё не зарегистрирован. Нажми /start"
    await show_screen(cb, text, back_kb())


@router.callback_query(F.data == "about")
async def cb_about(cb: CallbackQuery):
    await cb.answer()
    await show_screen(cb, ABOUT_TEXT, back_kb(), html=True)


@router.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery):
    await cb.answer()
    # Сначала ссылка, которую дал заказчик; если Telegram её не примет, запасная ссылка на профиль
    for link in (f"tg://openmessage?user_id={SUPPORT_ID}", f"tg://user?id={SUPPORT_ID}"):
        text = f'Здравствуй! По всем вопросам обращайся сюда: <a href="{link}">поддержка</a>'
        try:
            await cb.message.edit_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML,
                                       link_preview_options=NO_PREVIEW)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    await cb.message.edit_text(
        f"Здравствуй! По всем вопросам обращайся в поддержку. ID аккаунта: {SUPPORT_ID}",
        reply_markup=back_kb(),
    )


async def main():
    pool = await asyncpg.create_pool(DATABASE_URL)
    await pool.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS favorite_character TEXT")
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp["pool"] = pool
    dp.include_router(router)
    print("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
