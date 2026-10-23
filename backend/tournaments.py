import asyncpg
import random
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from backend.engine import add_mmr, apply_player, check_titles, record_result, score_info
from backend.main import BOT_TOKEN, verify_init_data
from backend.notify import fire, make_session

router = APIRouter(prefix="/api")


async def current_user(request: Request, x_init_data: str = Header(default="")):
    tg = verify_init_data(x_init_data)
    row = await request.app.state.pool.fetchrow(
        "SELECT id, role, nickname, is_banned, ban_reason FROM users WHERE tg_id = $1", tg["id"]
    )
    if not row:
        raise HTTPException(404, "Сначала напиши боту /start")
    if row["is_banned"]:
        why = f". Причина: {row['ban_reason']}" if row["ban_reason"] else ""
        raise HTTPException(403, "Аккаунт заблокирован" + why)
    photo = tg.get("photo_url")      # запоминаем аватарку из Телеграма, чтобы показывать её другим
    if photo:
        await request.app.state.pool.execute(
            "UPDATE users SET avatar_url = $2 WHERE id = $1 AND avatar_url IS DISTINCT FROM $2",
            row["id"], photo,
        )
    return row


def need_admin(user):
    if user["role"] not in ("admin", "owner"):
        raise HTTPException(403, "Только для админов")


async def advance(conn, match_id, winner_id):
    """Записывает победителя и переносит его в следующий матч сетки."""
    m = await conn.fetchrow(
        """UPDATE bracket_matches SET winner_id = $2 WHERE id = $1
           RETURNING tournament_id, next_match_id, next_slot""",
        match_id, winner_id,
    )
    if m["next_match_id"]:
        col = "player1_id" if m["next_slot"] == 1 else "player2_id"
        await conn.execute(
            f"UPDATE bracket_matches SET {col} = $1 WHERE id = $2", winner_id, m["next_match_id"]
        )
    else:
        await conn.execute("UPDATE tournaments SET status = 'finished' WHERE id = $1", m["tournament_id"])


class NewTournament(BaseModel):
    title: str
    format: str
    max_players: int | None = None


class Winner(BaseModel):
    winner_id: int
    brilliant: bool = False


@router.get("/tournaments")
async def list_tournaments(request: Request, user=Depends(current_user)):
    rows = await request.app.state.pool.fetch(
        """SELECT t.id, t.title, t.status, t.max_players, f.code AS format,
                  (SELECT count(*) FROM tournament_registrations r
                   WHERE r.tournament_id = t.id) AS players
           FROM tournaments t JOIN formats f ON f.id = t.format_id
           WHERE t.status <> 'cancelled' ORDER BY t.id DESC LIMIT 30"""
    )
    return [dict(r) for r in rows]


@router.post("/tournaments")
async def create_tournament(body: NewTournament, request: Request, user=Depends(current_user)):
    need_admin(user)
    pool = request.app.state.pool
    title = body.title.strip()[:120]
    if not title:
        raise HTTPException(400, "Введи название")
    fmt = await pool.fetchrow(
        "SELECT id FROM formats WHERE code = $1 AND affects_mmr AND is_active", body.format
    )
    if not fmt:
        raise HTTPException(400, "Неизвестный формат")
    if body.max_players is not None and not 2 <= body.max_players <= 256:
        raise HTTPException(400, "Мест должно быть от 2 до 256")
    tid = await pool.fetchval(
        "INSERT INTO tournaments (format_id, title, max_players, created_by) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        fmt["id"], title, body.max_players, user["id"],
    )
    return {"id": tid}


@router.get("/tournaments/{tid}")
async def tournament_detail(tid: int, request: Request, user=Depends(current_user)):
    pool = request.app.state.pool
    t = await pool.fetchrow(
        """SELECT t.id, t.title, t.status, t.max_players, f.code AS format
           FROM tournaments t JOIN formats f ON f.id = t.format_id WHERE t.id = $1""",
        tid,
    )
    if not t:
        raise HTTPException(404, "Турнир не найден")
    regs = await pool.fetch(
        """SELECT u.id, u.nickname, u.public_id FROM tournament_registrations r
           JOIN users u ON u.id = r.user_id WHERE r.tournament_id = $1
           ORDER BY r.registered_at""",
        tid,
    )
    matches = await pool.fetch(
        """SELECT m.id, m.round, m.position, m.winner_id,
                  m.player1_id, p1.nickname AS p1_name,
                  m.player2_id, p2.nickname AS p2_name
           FROM bracket_matches m
           LEFT JOIN users p1 ON p1.id = m.player1_id
           LEFT JOIN users p2 ON p2.id = m.player2_id
           WHERE m.tournament_id = $1 ORDER BY m.round, m.position""",
        tid,
    )
    return {
        **dict(t),
        "me": user["id"],
        "is_admin": user["role"] in ("admin", "owner"),
        "registered": [dict(r) for r in regs],
        "matches": [dict(m) for m in matches],
    }


@router.post("/tournaments/{tid}/register")
async def register(tid: int, request: Request, user=Depends(current_user)):
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            t = await conn.fetchrow(
                "SELECT status, max_players FROM tournaments WHERE id = $1 FOR UPDATE", tid
            )
            if not t or t["status"] != "registration":
                raise HTTPException(400, "Регистрация закрыта")
            count = await conn.fetchval(
                "SELECT count(*) FROM tournament_registrations WHERE tournament_id = $1", tid
            )
            already = await conn.fetchval(
                "SELECT 1 FROM tournament_registrations WHERE tournament_id = $1 AND user_id = $2",
                tid, user["id"],
            )
            if not already:
                if t["max_players"] and count >= t["max_players"]:
                    raise HTTPException(400, "Мест нет")
                await conn.execute(
                    "INSERT INTO tournament_registrations (tournament_id, user_id) VALUES ($1, $2)",
                    tid, user["id"],
                )
                # Места закончились: набор закрывается сам
                if t["max_players"] and count + 1 >= t["max_players"]:
                    await conn.execute(
                        "UPDATE tournaments SET status = 'bracket_ready' WHERE id = $1", tid
                    )
    return {"ok": True}


@router.post("/tournaments/{tid}/close")
async def close_registration(tid: int, request: Request, user=Depends(current_user)):
    need_admin(user)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            status = await conn.fetchval(
                "SELECT status FROM tournaments WHERE id = $1 FOR UPDATE", tid
            )
            if status != "registration":
                raise HTTPException(400, "Набор уже закрыт")
            count = await conn.fetchval(
                "SELECT count(*) FROM tournament_registrations WHERE tournament_id = $1", tid
            )
            if count < 2:
                raise HTTPException(400, "Нужно минимум 2 игрока")
            await conn.execute("UPDATE tournaments SET status = 'bracket_ready' WHERE id = $1", tid)
    return {"ok": True}


class BracketBody(BaseModel):
    mode: str = "auto"                          # auto | manual
    pairs: list[list[int | None]] | None = None  # для manual: [[игрок, соперник|None], ...]


@router.post("/tournaments/{tid}/bracket")
async def build_bracket(tid: int, request: Request, body: BracketBody = BracketBody(),
                        user=Depends(current_user)):
    need_admin(user)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            status = await conn.fetchval(
                "SELECT status FROM tournaments WHERE id = $1 FOR UPDATE", tid
            )
            if status != "bracket_ready":
                raise HTTPException(400, "Сначала закрой набор игроков")
            rows = await conn.fetch(
                "SELECT user_id FROM tournament_registrations WHERE tournament_id = $1", tid
            )
            ids = [r["user_id"] for r in rows]
            n = len(ids)
            size = 1
            while size < n:
                size *= 2
            rounds = size.bit_length() - 1

            if body.mode == "manual":
                pairs = [list(x) for x in (body.pairs or [])]
                for x in pairs:
                    if len(x) != 2 or x[0] is None or x[0] == x[1]:
                        raise HTTPException(400, "Неверная пара")
                flat = [v for x in pairs for v in x if v is not None]
                if sorted(flat) != sorted(ids):
                    raise HTTPException(400, "Нужно распределить всех игроков по одному разу")
                if len(pairs) != size // 2:
                    raise HTTPException(400, f"Нужно пар: {size // 2}, собрано: {len(pairs)}")
                first = [(x[0], x[1]) for x in pairs]
            else:
                random.shuffle(ids)
                byes = size - n
                queue = ids[:]
                first = []
                for pos in range(size // 2):
                    p1 = queue.pop(0)
                    p2 = queue.pop(0) if pos >= byes else None
                    first.append((p1, p2))

            # Создаём матчи от финала к первому раунду, чтобы знать next_match_id
            slots = {}
            for r in range(rounds, 0, -1):
                for pos in range(size >> r):
                    nxt = slots.get((r + 1, pos // 2)) if r < rounds else None
                    slots[(r, pos)] = await conn.fetchval(
                        """INSERT INTO bracket_matches
                           (tournament_id, round, position, next_match_id, next_slot)
                           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                        tid, r, pos, nxt, (pos % 2 + 1) if nxt else None,
                    )
            for pos, (p1, p2) in enumerate(first):
                mid = slots[(1, pos)]
                await conn.execute(
                    "UPDATE bracket_matches SET player1_id = $1, player2_id = $2 WHERE id = $3",
                    p1, p2, mid,
                )
                if p2 is None:
                    await advance(conn, mid, p1)
            await conn.execute("UPDATE tournaments SET status = 'running' WHERE id = $1", tid)
    return {"ok": True}


@router.post("/matches/{mid}/winner")
async def set_winner(mid: int, body: Winner, request: Request, user=Depends(current_user)):
    need_admin(user)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            m = await conn.fetchrow(
                """SELECT m.*, t.format_id FROM bracket_matches m
                   JOIN tournaments t ON t.id = m.tournament_id
                   WHERE m.id = $1 FOR UPDATE OF m""",
                mid,
            )
            if not m:
                raise HTTPException(404, "Матч не найден")
            if m["winner_id"]:
                raise HTTPException(400, "Результат уже записан")
            p1, p2 = m["player1_id"], m["player2_id"]
            if not p1 or not p2:
                raise HTTPException(400, "Соперники ещё не определены")
            if body.winner_id not in (p1, p2):
                raise HTTPException(400, "Победитель не из этого матча")
            loser = p2 if body.winner_id == p1 else p1
            before = {u: await _snap(conn, u) for u in (body.winner_id, loser)}
            match_id = await record_result(
                conn, format_id=m["format_id"], tournament_id=m["tournament_id"],
                bracket_match_id=mid, winner_id=body.winner_id, loser_id=loser,
                brilliant=body.brilliant, admin_id=user["id"],
            )
            await advance(conn, mid, body.winner_id)
            notes = await _match_notes(conn, match_id, m["tournament_id"], body.winner_id, loser,
                                       body.brilliant, before)
    fire(notes)
    return {"ok": True}


@router.post("/tournaments/{tid}/cancel")
async def cancel_tournament(tid: int, request: Request, user=Depends(current_user)):
    need_admin(user)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            status = await conn.fetchval(
                "SELECT status FROM tournaments WHERE id = $1 FOR UPDATE", tid
            )
            if not status:
                raise HTTPException(404, "Турнир не найден")
            if status in ("finished", "cancelled"):
                raise HTTPException(400, "Турнир уже завершён")
            played = await conn.fetchval(
                "SELECT 1 FROM matches WHERE tournament_id = $1 AND NOT is_cancelled LIMIT 1", tid
            )
            if played:
                raise HTTPException(400, "В турнире уже есть результаты, отменить нельзя")
            await conn.execute("UPDATE tournaments SET status = 'cancelled' WHERE id = $1", tid)
    return {"ok": True}


async def _build_profile(pool, uid):
    await _ensure_fav(pool)
    u = await pool.fetchrow(
        """SELECT u.nickname, u.public_id, u.role, u.mmr, u.peak_mmr, u.potential_mmr,
                  u.is_calibrated, u.calibration_games, u.is_banned, u.avatar_url, u.favorite_character,
                  r.code AS rank_code, r.name AS rank_name, r.color AS rank_color,
                  (SELECT t2.name FROM user_titles ut JOIN titles t2 ON t2.id = ut.title_id
                   WHERE ut.user_id = u.id AND ut.is_active
                   ORDER BY ut.awarded_at DESC LIMIT 1) AS title_name,
                  s.total_games, s.wins, s.losses, s.win_streak, s.best_win_streak,
                  s.fire_weeks, s.last_fire_week
           FROM users u
           JOIN user_stats s ON s.user_id = u.id
           LEFT JOIN ranks r ON r.code = u.rank_code
           WHERE u.id = $1""",
        uid,
    )
    if not u:
        return None
    last = await pool.fetch(
        "SELECT is_winner FROM match_players WHERE user_id = $1 ORDER BY match_id DESC LIMIT 10", uid
    )
    last100 = await pool.fetch(
        "SELECT is_winner FROM match_players WHERE user_id = $1 ORDER BY match_id DESC LIMIT 100", uid
    )
    fmts = await pool.fetch(
        """SELECT f.code, s.wins, s.losses, s.games FROM user_format_stats s
           JOIN formats f ON f.id = s.format_id WHERE s.user_id = $1 ORDER BY f.id""",
        uid,
    )
    views = await pool.fetchval("SELECT count(*) FROM profile_views WHERE profile_id = $1", uid)
    sc = await score_info(pool, uid)
    games, cal = u["total_games"], u["is_calibrated"]
    monday = date.today() - timedelta(days=date.today().weekday())
    fire_alive = u["last_fire_week"] and u["last_fire_week"] >= monday - timedelta(days=7)
    return {
        "nickname": u["nickname"], "public_id": u["public_id"], "role": u["role"],
        "is_banned": u["is_banned"], "views": views, "avatar_url": u["avatar_url"],
        "favorite_character": u["favorite_character"],
        "score": sc["score"], "score_percent": sc["percent"],
        "is_calibrated": cal, "calibration_games": u["calibration_games"],
        "mmr": u["mmr"],
        "peak_mmr": u["peak_mmr"],
        "potential_mmr": u["potential_mmr"],
        "rank_code": u["rank_code"] if cal else None,
        "rank_name": u["rank_name"] if cal else None,
        "rank_color": u["rank_color"] if cal else None,
        "title_name": u["title_name"],
        "total_games": games, "wins": u["wins"], "losses": u["losses"],
        "winrate": round(u["wins"] * 100 / games) if games else None,
        "winrate_last10": round(sum(r["is_winner"] for r in last) * 100 / len(last)) if last else None,
        "winrate_last100": round(sum(r["is_winner"] for r in last100) * 100 / len(last100)) if last100 else None,
        "games_last100": len(last100),
        "win_streak": u["win_streak"],
        "max_win_streak": u["best_win_streak"],
        "fire_weeks": u["fire_weeks"] if fire_alive else 0,
        "formats": [dict(f) for f in fmts],
        "titles": [dict(x) for x in await pool.fetch(
            """SELECT t.name, t.description, (ut.is_active IS TRUE) AS earned
               FROM titles t LEFT JOIN user_titles ut ON ut.title_id = t.id AND ut.user_id = $1
               WHERE t.is_active AND (t.condition_type <> 'manual' OR ut.is_active IS TRUE)
               ORDER BY t.id""", uid)],
    }


@router.get("/profile")
async def my_profile(request: Request, user=Depends(current_user)):
    return await _build_profile(request.app.state.pool, user["id"])


@router.get("/top")
async def top_players(request: Request, user=Depends(current_user)):
    rows = await request.app.state.pool.fetch(
        """SELECT u.public_id, u.nickname, u.avatar_url, u.mmr, r.code AS rank_code, r.name AS rank_name, r.color AS rank_color
           FROM users u LEFT JOIN ranks r ON r.code = u.rank_code
           WHERE u.is_calibrated AND NOT u.is_banned
           ORDER BY u.mmr DESC, u.created_at, u.id LIMIT 100"""
    )
    return [{"place": i + 1, **dict(r)} for i, r in enumerate(rows)]


@router.get("/players/{public_id}")
async def player_profile(public_id: str, request: Request, user=Depends(current_user)):
    pool = request.app.state.pool
    uid = await pool.fetchval("SELECT id FROM users WHERE public_id = $1", public_id.strip())
    if not uid:
        raise HTTPException(404, "Игрок с таким ID не найден")
    staff = user["role"] in ("admin", "owner")
    is_self = uid == user["id"]
    if not is_self:
        banned = await pool.fetchval("SELECT is_banned FROM users WHERE id = $1", uid)
        if banned and not staff:
            raise HTTPException(404, "Игрок с таким ID не найден")
        # Считаем уникальных зрителей, повторные заходы не накручивают счётчик
        await pool.execute(
            "INSERT INTO profile_views (profile_id, viewer_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            uid, user["id"],
        )
    d = await _build_profile(pool, uid)
    if not staff and not is_self:
        d["potential_mmr"] = None   # мнение админов видят только админы и сам игрок
        d["role"] = "player"        # значки админов обычным игрокам не показываем
    d["is_self"] = is_self
    return d


@router.get("/profile/matches")
async def my_matches(request: Request, user=Depends(current_user)):
    pool = request.app.state.pool
    rows = await pool.fetch(
        """SELECT m.id, m.played_at, f.code AS format, mp.is_winner, mp.mmr_delta,
                  m.is_brilliant, m.duration_sec, opp.nickname AS opponent, c.name AS character,
                  (SELECT l.delta FROM mmr_log l
                   WHERE l.user_id = mp.user_id AND l.match_id = m.id
                     AND l.reason = 'decade_bonus' LIMIT 1) AS bonus
           FROM match_players mp
           JOIN matches m ON m.id = mp.match_id
           JOIN formats f ON f.id = m.format_id
           LEFT JOIN match_players op ON op.match_id = m.id AND op.user_id <> mp.user_id
           LEFT JOIN users opp ON opp.id = op.user_id
           LEFT JOIN characters c ON c.id = mp.character_id
           WHERE mp.user_id = $1 AND NOT m.is_cancelled
           ORDER BY m.id DESC LIMIT 10""",
        user["id"],
    )
    return [dict(r) for r in rows]


# ---------------- Админка: игроки ----------------

class BanBody(BaseModel):
    banned: bool
    reason: str | None = None


def _perms(actor, target_id, target_role):
    owner = actor["role"] == "owner"
    can_target = (owner and target_role != "owner") or (actor["role"] == "admin" and target_role == "player")
    can_ban = bool(can_target and target_id != actor["id"])
    return {
        "can_reset": bool(owner),   # сброс, удаление и ручные победы: только создатель
        "can_ban": can_ban,         # бан: создатель и админы (админы только игроков)
        "can_delete": bool(owner and can_ban and target_role == "player"),
        "can_role": bool(owner and target_role in ("player", "admin") and target_id != actor["id"]),
        "can_id": bool(owner),      # красивые ID выдаёт только создатель
        "can_mmr": bool(owner),     # менять основной MMR вручную может только создатель
        "can_tester": bool(owner),  # титул «Тестер» выдаёт и снимает только создатель
    }


async def _get_target(conn, actor, uid, action):
    t = await conn.fetchrow("SELECT id, role FROM users WHERE id = $1 FOR UPDATE", uid)
    if not t:
        raise HTTPException(404, "Игрок не найден")
    if not _perms(actor, t["id"], t["role"])["can_" + action]:
        raise HTTPException(403, "Нет прав на это действие")
    return t


PLAYER_TABLES = ("match_players", "mmr_log", "decades", "user_format_stats",
                 "user_character_stats", "user_titles", "profile_scores")


async def _wipe_player_data(conn, uid):
    for table in PLAYER_TABLES:
        await conn.execute(f"DELETE FROM {table} WHERE user_id = $1", uid)


async def _drop_orphan_matches(conn):
    await conn.execute(
        """DELETE FROM matches m WHERE NOT EXISTS
           (SELECT 1 FROM match_players p WHERE p.match_id = m.id)"""
    )


@router.get("/admin/users")
async def admin_users(request: Request, user=Depends(current_user)):
    need_admin(user)
    rows = await request.app.state.pool.fetch(
        """SELECT u.id, u.public_id, u.nickname, u.tg_username, u.role, u.is_banned, u.ban_reason, u.potential_mmr,
                  u.mmr, s.total_games,
                  COALESCE((SELECT f.games FROM user_format_stats f JOIN formats ff ON ff.id = f.format_id
                            WHERE f.user_id = u.id AND ff.code = 'QUIZ'), 0) AS quiz_games,
                  COALESCE((SELECT f.wins FROM user_format_stats f JOIN formats ff ON ff.id = f.format_id
                            WHERE f.user_id = u.id AND ff.code = 'QUIZ'), 0) AS quiz_wins,
                  EXISTS(SELECT 1 FROM user_titles ut JOIN titles t ON t.id = ut.title_id
                         WHERE ut.user_id = u.id AND ut.is_active AND t.name = 'Тестер') AS is_tester
           FROM users u JOIN user_stats s ON s.user_id = u.id
           ORDER BY u.created_at, u.id"""
    )
    return [{**dict(r), **_perms(user, r["id"], r["role"])} for r in rows]


@router.post("/admin/users/{uid}/ban")
async def admin_ban(uid: int, body: BanBody, request: Request, user=Depends(current_user)):
    need_admin(user)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            await _get_target(conn, user, uid, "ban")
            reason = ((body.reason or "").strip()[:300] or None) if body.banned else None
            await conn.execute(
                "UPDATE users SET is_banned = $2, ban_reason = $3 WHERE id = $1",
                uid, body.banned, reason,
            )
            tg = await conn.fetchval("SELECT tg_id FROM users WHERE id = $1", uid)
    if body.banned:
        fire([(tg, "Твой аккаунт заблокирован." + (f"\nПричина: {reason}" if reason else ""))])
    else:
        fire([(tg, "Твой аккаунт разблокирован.")])
    return {"ok": True}


@router.post("/admin/users/{uid}/reset")
async def admin_reset(uid: int, request: Request, user=Depends(current_user)):
    need_admin(user)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            await _get_target(conn, user, uid, "reset")
            await _wipe_player_data(conn, uid)
            await conn.execute(
                """UPDATE user_stats SET total_games = 0, wins = 0, losses = 0, win_streak = 0,
                       best_win_streak = 0, fire_weeks = 0, last_fire_week = NULL,
                       last_game_at = NULL WHERE user_id = $1""", uid)
            await conn.execute(
                """UPDATE users SET mmr = 100, peak_mmr = 100, potential_mmr = NULL,
                       rank_code = NULL, calibration_games = 0, is_calibrated = FALSE,
                       active_title_id = NULL WHERE id = $1""", uid)
            await conn.execute(
                "INSERT INTO mmr_log (user_id, delta, reason, note) "
                "VALUES ($1, 100, 'register', 'Бонус за регистрацию')", uid)
            await _drop_orphan_matches(conn)
    return {"ok": True}


@router.post("/admin/users/{uid}/delete")
async def admin_delete(uid: int, request: Request, user=Depends(current_user)):
    need_admin(user)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            await _get_target(conn, user, uid, "delete")
            await _wipe_player_data(conn, uid)
            # Бои, которые выиграл этот игрок, удаляем целиком (у соперников они пропадут из истории)
            await conn.execute(
                "UPDATE mmr_log SET match_id = NULL WHERE match_id IN "
                "(SELECT id FROM matches WHERE winner_id = $1)", uid)
            await conn.execute("DELETE FROM matches WHERE winner_id = $1", uid)
            for col in ("player1_id", "player2_id", "winner_id"):
                await conn.execute(f"UPDATE bracket_matches SET {col} = NULL WHERE {col} = $1", uid)
            await conn.execute("DELETE FROM tournament_registrations WHERE user_id = $1", uid)
            await conn.execute("DELETE FROM profile_views WHERE profile_id = $1 OR viewer_id = $1", uid)
            await conn.execute("DELETE FROM referrals WHERE referred_id = $1 OR referrer_id = $1", uid)
            await conn.execute("UPDATE users SET referred_by = NULL WHERE referred_by = $1", uid)
            await conn.execute("DELETE FROM user_stats WHERE user_id = $1", uid)
            await conn.execute("DELETE FROM users WHERE id = $1", uid)
            await _drop_orphan_matches(conn)
    return {"ok": True}


class ManualWin(BaseModel):
    user_id: int
    format: str
    brilliant: bool = False


@router.post("/admin/manual-win")
async def admin_manual_win(body: ManualWin, request: Request, user=Depends(current_user)):
    """Победа, внесённая вручную (например, перенос со старого сайта). Идёт через ту же
    систему, что и обычный бой: MMR, статистика, калибровка, десятка, история матчей."""
    need_admin(user)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            t = await conn.fetchrow("SELECT id, role, tg_id FROM users WHERE id = $1 FOR UPDATE", body.user_id)
            if not t:
                raise HTTPException(404, "Игрок не найден")
            if user["role"] != "owner":
                raise HTTPException(403, "Только создатель может вносить результаты вручную")
            fmt = await conn.fetchrow(
                "SELECT id FROM formats WHERE code = $1 AND affects_mmr AND is_active", body.format
            )
            if not fmt:
                raise HTTPException(400, "Неизвестный формат")
            before = await _snap(conn, t["id"])
            match_id = await conn.fetchval(
                """INSERT INTO matches (format_id, winner_id, is_brilliant, created_by)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                fmt["id"], t["id"], body.brilliant, user["id"],
            )
            delta, after = await apply_player(conn, t["id"], True, body.brilliant, match_id, fmt["id"])
            await conn.execute(
                """INSERT INTO match_players (match_id, user_id, is_winner, mmr_delta, mmr_after)
                   VALUES ($1, $2, TRUE, $3, $4)""",
                match_id, t["id"], delta, after,
            )
            await check_titles(conn, t["id"])
            after = await _snap(conn, t["id"])
            head = (f"Тебе записана победа ({body.format}): +{10 if body.brilliant else 5} MMR"
                    + (" (блестящая победа)" if body.brilliant else ""))
            notes = [(t["tg_id"], "\n".join([head] + _extras(before, after)))]
    fire(notes)
    return {"ok": True}


class RoleBody(BaseModel):
    role: str


@router.post("/admin/users/{uid}/role")
async def admin_role(uid: int, body: RoleBody, request: Request, user=Depends(current_user)):
    need_admin(user)
    if body.role not in ("admin", "player"):
        raise HTTPException(400, "Неизвестная роль")
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            await _get_target(conn, user, uid, "role")
            await conn.execute("UPDATE users SET role = $2 WHERE id = $1", uid, body.role)
            tg = await conn.fetchval("SELECT tg_id FROM users WHERE id = $1", uid)
    fire([(tg, "Тебе выдали права админа. Во вкладке «Админ» теперь есть управление." if body.role == "admin"
           else "Права админа сняты.")])
    return {"ok": True}


# ---------------- Частые вопросы (FAQ) ----------------
_faq_ready = False


async def _ensure_faq(pool):
    global _faq_ready
    if not _faq_ready:
        await pool.execute(
            """CREATE TABLE IF NOT EXISTS faq (
                   id SERIAL PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL,
                   position INT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
        )
        _faq_ready = True


class FaqBody(BaseModel):
    question: str
    answer: str


def _clean_faq(body):
    q, a = body.question.strip()[:300], body.answer.strip()[:4000]
    if not q or not a:
        raise HTTPException(400, "Заполни и вопрос, и ответ")
    return q, a


@router.get("/faq")
async def faq_list(request: Request, user=Depends(current_user)):
    pool = request.app.state.pool
    await _ensure_faq(pool)
    rows = await pool.fetch("SELECT id, question, answer FROM faq ORDER BY position, id")
    return [dict(r) for r in rows]


@router.post("/faq")
async def faq_add(body: FaqBody, request: Request, user=Depends(current_user)):
    need_admin(user)
    pool = request.app.state.pool
    await _ensure_faq(pool)
    q, a = _clean_faq(body)
    await pool.execute(
        "INSERT INTO faq (question, answer, position) "
        "VALUES ($1, $2, (SELECT COALESCE(MAX(position), 0) + 1 FROM faq))", q, a)
    return {"ok": True}


@router.put("/faq/{fid}")
async def faq_edit(fid: int, body: FaqBody, request: Request, user=Depends(current_user)):
    need_admin(user)
    pool = request.app.state.pool
    await _ensure_faq(pool)
    q, a = _clean_faq(body)
    if not await pool.fetchval("UPDATE faq SET question = $2, answer = $3 WHERE id = $1 RETURNING id", fid, q, a):
        raise HTTPException(404, "Вопрос не найден")
    return {"ok": True}


@router.delete("/faq/{fid}")
async def faq_delete(fid: int, request: Request, user=Depends(current_user)):
    need_admin(user)
    pool = request.app.state.pool
    await _ensure_faq(pool)
    await pool.execute("DELETE FROM faq WHERE id = $1", fid)
    return {"ok": True}


class PublicIdBody(BaseModel):
    public_id: str


@router.post("/admin/users/{uid}/public-id")
async def admin_set_public_id(uid: int, body: PublicIdBody, request: Request, user=Depends(current_user)):
    need_admin(user)
    if user["role"] != "owner":
        raise HTTPException(403, "Красивые ID выдаёт только создатель")
    new = body.public_id.strip()
    if not (new.isascii() and new.isdigit() and 3 <= len(new) <= 6):
        raise HTTPException(400, "ID должен состоять из 3-6 цифр")
    try:
        async with request.app.state.pool.acquire() as conn:
            async with conn.transaction():
                t = await conn.fetchrow("SELECT id, public_id FROM users WHERE id = $1 FOR UPDATE", uid)
                if not t:
                    raise HTTPException(404, "Игрок не найден")
                if new != t["public_id"]:
                    if await conn.fetchval("SELECT 1 FROM users WHERE public_id = $1", new):
                        raise HTTPException(400, "Этот ID уже занят")
                    await conn.execute(
                        "UPDATE users SET public_id = $2, is_vanity_id = TRUE WHERE id = $1", uid, new
                    )
    except asyncpg.UniqueViolationError:
        raise HTTPException(400, "Этот ID уже занят")
    return {"ok": True}


class PotentialBody(BaseModel):
    value: int | None = None


@router.post("/admin/users/{uid}/potential")
async def admin_potential(uid: int, body: PotentialBody, request: Request, user=Depends(current_user)):
    need_admin(user)
    if body.value is not None and not 0 <= body.value <= 5000:
        raise HTTPException(400, "Потенциальный MMR: число от 0 до 5000")
    done = await request.app.state.pool.fetchval(
        "UPDATE users SET potential_mmr = $2 WHERE id = $1 RETURNING id", uid, body.value
    )
    if not done:
        raise HTTPException(404, "Игрок не найден")
    tg = await request.app.state.pool.fetchval("SELECT tg_id FROM users WHERE id = $1", uid)
    fire([(tg, f"Администрация обновила твой потенциальный MMR: {body.value}" if body.value is not None
           else "Администрация убрала твой потенциальный MMR.")])
    return {"ok": True}


class MmrBody(BaseModel):
    value: int


@router.post("/admin/users/{uid}/mmr")
async def admin_set_mmr(uid: int, body: MmrBody, request: Request, user=Depends(current_user)):
    need_admin(user)
    if user["role"] != "owner":
        raise HTTPException(403, "Менять MMR вручную может только создатель")
    if not 0 <= body.value <= 10000:
        raise HTTPException(400, "MMR: число от 0 до 10000")
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            cur = await conn.fetchval("SELECT mmr FROM users WHERE id = $1 FOR UPDATE", uid)
            if cur is None:
                raise HTTPException(404, "Игрок не найден")
            notes = []
            if body.value != cur:
                before = await _snap(conn, uid)
                await add_mmr(conn, uid, body.value - cur, "manual",
                              note=f"Изменено создателем: {cur} → {body.value}")
                after = await _snap(conn, uid)
                tg = await conn.fetchval("SELECT tg_id FROM users WHERE id = $1", uid)
                notes = [(tg, _mmr_change_text(before, after))]
    fire(notes)
    return {"ok": True}


# ---------------- Сравнение игроков ----------------

@router.get("/players")
async def players_list(request: Request, user=Depends(current_user)):
    rows = await request.app.state.pool.fetch(
        "SELECT id, public_id, nickname FROM users WHERE NOT is_banned ORDER BY nickname LIMIT 1000"
    )
    return [{"public_id": r["public_id"], "nickname": r["nickname"], "is_me": r["id"] == user["id"]}
            for r in rows]


@router.get("/compare")
async def compare_players(a: str, b: str, request: Request, user=Depends(current_user)):
    pool = request.app.state.pool
    q = "SELECT id, nickname, public_id, mmr, is_calibrated FROM users WHERE public_id = $1 AND NOT is_banned"
    ua, ub = await pool.fetchrow(q, a.strip()), await pool.fetchrow(q, b.strip())
    if not ua or not ub:
        raise HTTPException(404, "Игрок не найден")
    if ua["id"] == ub["id"]:
        raise HTTPException(400, "Выбери двух разных игроков")
    rows = await pool.fetch(
        """SELECT m.played_at, m.winner_id FROM matches m
           JOIN match_players pa ON pa.match_id = m.id AND pa.user_id = $1
           JOIN match_players pb ON pb.match_id = m.id AND pb.user_id = $2
           WHERE NOT m.is_cancelled ORDER BY m.played_at DESC, m.id DESC""",
        ua["id"], ub["id"],
    )
    names = {ua["id"]: ua["nickname"], ub["id"]: ub["nickname"]}
    forecast = None
    if ua["is_calibrated"] and ub["is_calibrated"]:
        pa = round(100 / (1 + 10 ** ((ub["mmr"] - ua["mmr"]) / 400)))   # формула Эло
        forecast = {"a": pa, "b": 100 - pa}
    return {
        "a": {"nickname": ua["nickname"], "public_id": ua["public_id"]},
        "b": {"nickname": ub["nickname"], "public_id": ub["public_id"]},
        "score_a": sum(1 for r in rows if r["winner_id"] == ua["id"]),
        "score_b": sum(1 for r in rows if r["winner_id"] == ub["id"]),
        "forecast": forecast,
        "meetings": [{"played_at": r["played_at"], "winner": names[r["winner_id"]]} for r in rows],
    }


# ---------------- Викторины: только счётчики (участие и победы, без поражений) ----------------

class QuizDelta(BaseModel):
    kind: str      # 'part' (участие) или 'win' (победа)
    delta: int     # +1 или -1


@router.post("/admin/users/{uid}/quiz")
async def admin_quiz_counter(uid: int, body: QuizDelta, request: Request, user=Depends(current_user)):
    need_admin(user)
    if body.kind not in ("part", "win") or body.delta not in (1, -1):
        raise HTTPException(400, "Неверный запрос")
    games_d = body.delta                                 # победа тоже считается участием
    wins_d = body.delta if body.kind == "win" else 0
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", uid):
                raise HTTPException(404, "Игрок не найден")
            fid = await conn.fetchval("SELECT id FROM formats WHERE code = 'QUIZ'")
            await conn.execute(
                """INSERT INTO user_format_stats (user_id, format_id, wins, losses, games)
                   VALUES ($1, $2, GREATEST($3, 0), 0, GREATEST($4, 0))
                   ON CONFLICT (user_id, format_id) DO UPDATE SET
                       wins = GREATEST(user_format_stats.wins + $3, 0),
                       games = GREATEST(user_format_stats.games + $4, 0)""",
                uid, fid, wins_d, games_d,
            )
            await conn.execute(      # побед не может быть больше, чем участий
                "UPDATE user_format_stats SET wins = LEAST(wins, games) WHERE user_id = $1 AND format_id = $2",
                uid, fid,
            )
            if body.delta > 0:
                await check_titles(conn, uid)            # например, «ГИК» за 100 викторин
            row = await conn.fetchrow(
                "SELECT games, wins FROM user_format_stats WHERE user_id = $1 AND format_id = $2", uid, fid
            )
    return {"games": row["games"], "wins": row["wins"]}


# ---------------- Любимый персонаж (игрок вписывает сам) ----------------
_fav_ready = False


async def _ensure_fav(pool):
    global _fav_ready
    if not _fav_ready:
        await pool.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS favorite_character TEXT")
        _fav_ready = True


class FavBody(BaseModel):
    name: str = ""


@router.post("/profile/favorite")
async def set_favorite(body: FavBody, request: Request, user=Depends(current_user)):
    pool = request.app.state.pool
    await _ensure_fav(pool)
    name = " ".join(body.name.split())[:40] or None     # пустое поле = убрать персонажа
    await pool.execute("UPDATE users SET favorite_character = $2 WHERE id = $1", user["id"], name)
    return {"ok": True}


# ---------------- Топ активных и топ олдов ----------------

@router.get("/top/active")
async def top_active(request: Request, user=Depends(current_user)):
    """Больше всего боёв за последние 30 дней."""
    rows = await request.app.state.pool.fetch(
        """SELECT u.public_id, u.nickname, count(*) AS games
           FROM match_players mp
           JOIN matches m ON m.id = mp.match_id
           JOIN users u ON u.id = mp.user_id
           WHERE m.played_at >= now() - interval '30 days' AND NOT m.is_cancelled AND NOT u.is_banned
           GROUP BY u.id ORDER BY games DESC, u.created_at, u.id LIMIT 100"""
    )
    return [{"place": i + 1, **dict(r)} for i, r in enumerate(rows)]


@router.get("/top/old")
async def top_old(request: Request, user=Depends(current_user)):
    """Самые первые зарегистрированные участники."""
    rows = await request.app.state.pool.fetch(
        "SELECT public_id, nickname, created_at FROM users WHERE NOT is_banned ORDER BY created_at, id LIMIT 100"
    )
    return [{"place": i + 1, "public_id": r["public_id"], "nickname": r["nickname"],
             "registered": r["created_at"].strftime("%d.%m.%Y")} for i, r in enumerate(rows)]


# ---------------- Уведомления: сборка текстов ----------------

async def _snap(conn, uid):
    """Снимок состояния игрока (до и после действия), чтобы понять, что изменилось."""
    r = await conn.fetchrow(
        """SELECT u.mmr, u.is_calibrated, u.rank_code, rk.name AS rank_name
           FROM users u LEFT JOIN ranks rk ON rk.code = u.rank_code WHERE u.id = $1""", uid)
    titles = {x["name"] for x in await conn.fetch(
        "SELECT t.name FROM user_titles ut JOIN titles t ON t.id = ut.title_id "
        "WHERE ut.user_id = $1 AND ut.is_active", uid)}
    return {"mmr": r["mmr"], "cal": r["is_calibrated"], "rank": r["rank_code"],
            "rank_name": r["rank_name"], "titles": titles}


def _extras(b, a, bonus=None):
    lines = [f"Теперь у тебя {a['mmr']} MMR."]
    if bonus:
        lines.append(f"Бонус за десятку: +{bonus} MMR")
    if a["cal"] and not b["cal"]:
        lines.append(f"Калибровка пройдена! Твой ранг: {a['rank_name']}")
    elif a["cal"] and b["rank"] != a["rank"]:
        lines.append(f"Ранг изменился: {b['rank_name']} → {a['rank_name']}")
    for name in sorted(a["titles"] - b["titles"]):
        lines.append(f"Новый титул: {name}")
    return lines


def _mmr_change_text(b, a):
    lines = [f"Создатель изменил твой MMR: {b['mmr']} → {a['mmr']}"]
    if a["cal"] and b["rank"] != a["rank"]:
        lines.append(f"Ранг изменился: {b['rank_name'] or '—'} → {a['rank_name'] or '—'}")
    return "\n".join(lines)


async def _match_notes(conn, match_id, tournament_id, winner, loser, brilliant, before):
    tour = await conn.fetchval("SELECT title FROM tournaments WHERE id = $1", tournament_id)
    people = {r["id"]: r for r in await conn.fetch(
        "SELECT id, nickname, tg_id FROM users WHERE id = ANY($1::bigint[])", [winner, loser])}
    notes = []
    for uid, win in ((winner, True), (loser, False)):
        opp = people[loser if win else winner]["nickname"]
        a = await _snap(conn, uid)
        bonus = await conn.fetchval(
            "SELECT delta FROM mmr_log WHERE user_id = $1 AND match_id = $2 AND reason = 'decade_bonus'",
            uid, match_id)
        if win:
            head = (f"Победа над {opp} в турнире «{tour}»: +{10 if brilliant else 5} MMR"
                    + (" (блестящая победа)" if brilliant else ""))
        else:
            head = f"Поражение от {opp} в турнире «{tour}»: −2 MMR"
        notes.append((people[uid]["tg_id"], "\n".join([head] + _extras(before[uid], a, bonus))))
    return notes


# ---------------- Титул «Тестер» (выдаётся и снимается вручную) ----------------

class TesterBody(BaseModel):
    on: bool


@router.post("/admin/users/{uid}/tester")
async def admin_tester(uid: int, body: TesterBody, request: Request, user=Depends(current_user)):
    need_admin(user)
    if user["role"] != "owner":
        raise HTTPException(403, "Титул «Тестер» выдаёт только создатель")
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval("SELECT 1 FROM users WHERE id = $1", uid):
                raise HTTPException(404, "Игрок не найден")
            tid = await conn.fetchval("SELECT id FROM titles WHERE name = 'Тестер'")
            if tid is None:
                tid = await conn.fetchval(
                    "INSERT INTO titles (name, description, condition_type) "
                    "VALUES ('Тестер', '(Выдаётся создателем)', 'manual') RETURNING id")
            if body.on:
                await conn.execute(
                    """INSERT INTO user_titles (user_id, title_id) VALUES ($1, $2)
                       ON CONFLICT (user_id, title_id) DO UPDATE SET is_active = TRUE, awarded_at = now()""",
                    uid, tid)
            else:
                await conn.execute(
                    "UPDATE user_titles SET is_active = FALSE WHERE user_id = $1 AND title_id = $2", uid, tid)
            tg = await conn.fetchval("SELECT tg_id FROM users WHERE id = $1", uid)
    fire([(tg, "Тебе выдан титул «Тестер»." if body.on else "Титул «Тестер» снят.")])
    return {"ok": True}
