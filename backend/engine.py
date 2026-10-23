from datetime import date, timedelta
from decimal import Decimal

WIN, BRILLIANT, LOSS = 5, 10, -2
CALIBRATION_GAMES = 5


def decade_rule(n: int):
    """(бонус MMR, минимальный win rate за десятку, %)"""
    if n == 1:
        return 100, 0
    if n <= 9:
        return 50, 50
    return 35, 70


async def refresh_rank(conn, user_id):
    # Звание считается только после калибровки
    await conn.execute(
        """UPDATE users SET rank_code = (
               SELECT code FROM ranks
               WHERE min_mmr <= users.mmr AND (max_mmr IS NULL OR max_mmr >= users.mmr)
               ORDER BY min_mmr DESC LIMIT 1)
           WHERE id = $1 AND is_calibrated""",
        user_id,
    )


async def add_mmr(conn, user_id, delta, reason, match_id=None, note=None):
    mmr = await conn.fetchval(
        """UPDATE users SET mmr = GREATEST(0, mmr + $1),
                  peak_mmr = GREATEST(peak_mmr, mmr + $1)
           WHERE id = $2 RETURNING mmr""",
        delta, user_id,
    )
    await conn.execute(
        "INSERT INTO mmr_log (user_id, delta, reason, match_id, note) VALUES ($1,$2,$3,$4,$5)",
        user_id, delta, reason, match_id, note,
    )
    await refresh_rank(conn, user_id)
    return mmr


async def apply_player(conn, user_id, is_win, brilliant, match_id, format_id):
    w, l = int(is_win), int(not is_win)
    delta = (BRILLIANT if brilliant else WIN) if is_win else LOSS
    mmr_after = await add_mmr(conn, user_id, delta, "match", match_id)

    # Общая статистика и серия побед
    await conn.execute(
        """UPDATE user_stats SET total_games = total_games + 1,
               wins = wins + $2, losses = losses + $3,
               win_streak = CASE WHEN $2 = 1 THEN win_streak + 1 ELSE 0 END,
               best_win_streak = GREATEST(best_win_streak,
                                          CASE WHEN $2 = 1 THEN win_streak + 1 ELSE 0 END),
               last_game_at = now()
           WHERE user_id = $1""",
        user_id, w, l,
    )
    # Статистика по формату
    await conn.execute(
        """INSERT INTO user_format_stats (user_id, format_id, wins, losses, games)
           VALUES ($1, $2, $3, $4, 1)
           ON CONFLICT (user_id, format_id) DO UPDATE SET
               wins = user_format_stats.wins + $3,
               losses = user_format_stats.losses + $4,
               games = user_format_stats.games + 1""",
        user_id, format_id, w, l,
    )
    # «Огонёк»: недели подряд, в которых был хотя бы 1 бой
    monday = date.today() - timedelta(days=date.today().weekday())
    s = await conn.fetchrow(
        "SELECT fire_weeks, last_fire_week FROM user_stats WHERE user_id = $1", user_id
    )
    if s["last_fire_week"] != monday:
        weeks = s["fire_weeks"] + 1 if s["last_fire_week"] == monday - timedelta(days=7) else 1
        await conn.execute(
            "UPDATE user_stats SET fire_weeks = $2, last_fire_week = $3 WHERE user_id = $1",
            user_id, weeks, monday,
        )
    # Калибровка: после 10 игр открываем MMR и выдаём звание
    u = await conn.fetchrow(
        "SELECT is_calibrated, calibration_games FROM users WHERE id = $1", user_id
    )
    if not u["is_calibrated"]:
        games = u["calibration_games"] + 1
        done = games >= CALIBRATION_GAMES
        await conn.execute(
            "UPDATE users SET calibration_games = $2, is_calibrated = $3 WHERE id = $1",
            user_id, games, done,
        )
        if done:
            await refresh_rank(conn, user_id)
    # Десятки побед
    d = await conn.fetchrow(
        "SELECT decade_number, wins, losses FROM decades WHERE user_id = $1 AND NOT is_closed",
        user_id,
    )
    if d is None:
        n = await conn.fetchval(
            "SELECT COALESCE(MAX(decade_number), 0) + 1 FROM decades WHERE user_id = $1", user_id
        )
        await conn.execute("INSERT INTO decades (user_id, decade_number) VALUES ($1, $2)", user_id, n)
        d = {"decade_number": n, "wins": 0, "losses": 0}
    n, wins, losses = d["decade_number"], d["wins"] + w, d["losses"] + l
    if wins >= 10:
        winrate = wins * 100 / (wins + losses)
        bonus, min_wr = decade_rule(n)
        ok = winrate >= min_wr
        await conn.execute(
            """UPDATE decades SET wins = $3, losses = $4, is_closed = TRUE, winrate = $5,
                      bonus_awarded = $6, bonus_mmr = $7
               WHERE user_id = $1 AND decade_number = $2""",
            user_id, n, wins, losses, Decimal(str(round(winrate, 2))), ok, bonus if ok else 0,
        )
        if ok:
            await add_mmr(conn, user_id, bonus, "decade_bonus", match_id, f"Бонус {n}-й десятки")
    else:
        await conn.execute(
            "UPDATE decades SET wins = $3, losses = $4 WHERE user_id = $1 AND decade_number = $2",
            user_id, n, wins, losses,
        )
    return delta, mmr_after


async def check_titles(conn, user_id):
    """Проверяет все титулы игрока: выдаёт новые, у «пересчитываемых» (Непобедимый) включает/выключает."""
    s = await conn.fetchrow("SELECT total_games, wins FROM user_stats WHERE user_id = $1", user_id)
    for t in await conn.fetch("SELECT * FROM titles WHERE is_active"):
        ct = t["condition_type"]
        if ct == "games_total":
            ok = s["total_games"] >= t["condition_value"]
        elif ct == "wins_total":
            ok = s["wins"] >= t["condition_value"]
        elif ct == "format_games":
            g = await conn.fetchval(
                "SELECT games FROM user_format_stats WHERE user_id = $1 AND format_id = $2",
                user_id, t["format_id"],
            )
            ok = (g or 0) >= t["condition_value"]
        elif ct == "winrate_last_n":
            rows = await conn.fetch(
                "SELECT is_winner FROM match_players WHERE user_id = $1 ORDER BY match_id DESC LIMIT $2",
                user_id, t["last_n"],
            )
            ok = (len(rows) >= t["condition_value"] and len(rows) > 0 and
                  sum(r["is_winner"] for r in rows) * 100 / len(rows) >= float(t["min_winrate"]))
        else:
            continue   # 'manual' и прочие выдаются вручную
        have = await conn.fetchrow(
            "SELECT is_active FROM user_titles WHERE user_id = $1 AND title_id = $2", user_id, t["id"]
        )
        if ok and not have:
            await conn.execute(
                "INSERT INTO user_titles (user_id, title_id) VALUES ($1, $2)", user_id, t["id"]
            )
        elif ok and have and not have["is_active"]:
            await conn.execute(
                "UPDATE user_titles SET is_active = TRUE, awarded_at = now() "
                "WHERE user_id = $1 AND title_id = $2", user_id, t["id"],
            )
        elif not ok and have and have["is_active"] and t["is_recheck"]:
            await conn.execute(
                "UPDATE user_titles SET is_active = FALSE WHERE user_id = $1 AND title_id = $2",
                user_id, t["id"],
            )


async def record_result(conn, *, format_id, tournament_id, bracket_match_id,
                        winner_id, loser_id, brilliant, admin_id):
    match_id = await conn.fetchval(
        """INSERT INTO matches (format_id, tournament_id, bracket_match_id, winner_id,
                                is_brilliant, created_by)
           VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
        format_id, tournament_id, bracket_match_id, winner_id, brilliant, admin_id,
    )
    for uid, win in ((winner_id, True), (loser_id, False)):
        delta, after = await apply_player(conn, uid, win, brilliant, match_id, format_id)
        await conn.execute(
            """INSERT INTO match_players (match_id, user_id, is_winner, mmr_delta, mmr_after)
               VALUES ($1, $2, $3, $4, $5)""",
            match_id, uid, win, delta, after,
        )
    for uid in (winner_id, loser_id):
        await check_titles(conn, uid)
    return match_id


# ---------------- Оценка профиля ----------------
# Вес каждого показателя (сумма = 1). Меняется здесь, в одном месте.
W_MMR, W_WINRATE, W_WINRATE10, W_TITLES, W_FIRE = 0.35, 0.15, 0.20, 0.15, 0.15
MIN_PLAYERS_FOR_PERCENT = 20   # пока игроков меньше, процент не показываем


def _score_value(r, monday):
    mmr_p = min(r["mmr"] / 2000, 1) if r["is_calibrated"] else 0
    g = r["total_games"]
    wr_p = (r["wins"] / g) * min(g / 20, 1) if g else 0
    wr10_p = (r["w10"] / r["n10"]) * min(r["n10"] / 10, 1) if r["n10"] else 0
    t_p = min(r["tcount"] / 3, 1)
    alive = r["last_fire_week"] and r["last_fire_week"] >= monday - timedelta(days=7)
    f_p = min((r["fire_weeks"] if alive else 0) / 8, 1)
    total = W_MMR * mmr_p + W_WINRATE * wr_p + W_WINRATE10 * wr10_p + W_TITLES * t_p + W_FIRE * f_p
    return int(total * 10 + 0.5)


async def score_info(pool, uid):
    rows = await pool.fetch(
        """WITH last10 AS (
               SELECT user_id, count(*) AS n, sum(is_winner::int) AS w FROM (
                   SELECT user_id, is_winner,
                          row_number() OVER (PARTITION BY user_id ORDER BY match_id DESC) AS rn
                   FROM match_players) x WHERE rn <= 10 GROUP BY user_id)
           SELECT u.id, u.mmr, u.is_calibrated, s.total_games, s.wins, s.fire_weeks, s.last_fire_week,
                  COALESCE(l.n, 0) AS n10, COALESCE(l.w, 0) AS w10,
                  (SELECT count(*) FROM user_titles ut WHERE ut.user_id = u.id AND ut.is_active) AS tcount
           FROM users u JOIN user_stats s ON s.user_id = u.id
           LEFT JOIN last10 l ON l.user_id = u.id
           WHERE NOT u.is_banned"""
    )
    monday = date.today() - timedelta(days=date.today().weekday())
    scores = {r["id"]: _score_value(r, monday) for r in rows}
    if uid not in scores:
        return {"score": None, "percent": None}
    mine = scores[uid]
    percent = None
    if len(scores) >= MIN_PLAYERS_FOR_PERCENT:
        percent = round(sum(1 for v in scores.values() if v == mine) * 100 / len(scores))
    return {"score": mine, "percent": percent}
