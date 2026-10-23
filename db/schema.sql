-- =====================================================================
-- MMR Rating: Telegram bot + Mini App. PostgreSQL 14+
-- =====================================================================

-- ---------- Справочники (меняются данными, без правки кода) ----------

-- Звания по диапазонам MMR
CREATE TABLE ranks (
    code        VARCHAR(10) PRIMARY KEY,      -- 'S+', 'B', 'LOW_STATIST'
    name        VARCHAR(50) NOT NULL,         -- 'S+ (High S)'
    min_mmr     INT NOT NULL,
    max_mmr     INT,                          -- NULL = без верхней границы
    color       VARCHAR(7) NOT NULL,          -- '#ff5fa2'
    sort_order  INT NOT NULL
);

-- Форматы боёв: PB, BP, PBP, викторины, (5-й потом)
CREATE TABLE formats (
    id            SERIAL PRIMARY KEY,
    code          VARCHAR(20) UNIQUE NOT NULL,   -- 'PB','BP','PBP','QUIZ'
    name          VARCHAR(50) NOT NULL,
    affects_mmr   BOOLEAN NOT NULL DEFAULT TRUE, -- викторины: победы/поражения не пишем
    is_active     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE characters (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(80) UNIQUE NOT NULL,      -- 'Мадара Учиха'
    is_active  BOOLEAN NOT NULL DEFAULT TRUE
);

-- «Форма» персонажа (если у персонажа есть формы)
CREATE TABLE character_forms (
    id            SERIAL PRIMARY KEY,
    character_id  INT NOT NULL REFERENCES characters(id),
    name          VARCHAR(80) NOT NULL,
    UNIQUE (character_id, name)
);

CREATE TABLE arenas (
    id    SERIAL PRIMARY KEY,
    name  VARCHAR(80) UNIQUE NOT NULL            -- 'ИМХО-арена'
);

-- Настройки системы (веса оценки профиля, бонусы и т.д.)
CREATE TABLE settings (
    key    VARCHAR(50) PRIMARY KEY,
    value  JSONB NOT NULL
);
-- Примеры:
-- 'mmr_rules'     -> {"win":5,"brilliant_win":10,"loss":-2,"register_bonus":100,
--                     "calibration_games":10}
-- 'decade_bonus'  -> [{"from":1,"to":1,"bonus":100,"min_winrate":0},
--                     {"from":2,"to":9,"bonus":50,"min_winrate":50},
--                     {"from":10,"to":null,"bonus":35,"min_winrate":70}]
-- 'referral'      -> {"first_n":10,"first_bonus":10,"next_bonus":5}
-- 'score_weights' -> {"mmr":0.40,"last10":0.25,"winrate":0.20,"titles":0.15}

-- ---------- Пользователи ----------

CREATE TABLE users (
    id                BIGSERIAL PRIMARY KEY,
    tg_id             BIGINT UNIQUE NOT NULL,
    tg_username       VARCHAR(64),
    nickname          VARCHAR(64) NOT NULL,
    avatar_url        TEXT,
    public_id         VARCHAR(6) UNIQUE NOT NULL,  -- '001', '0001' (поиск профиля)
    is_vanity_id      BOOLEAN NOT NULL DEFAULT FALSE, -- красивый ID выдан вручную
    role              VARCHAR(10) NOT NULL DEFAULT 'player'
                      CHECK (role IN ('player','admin','owner')),

    -- MMR. Реальный хранится всегда, но показывается только после калибровки
    mmr               INT NOT NULL DEFAULT 100,    -- +100 за регистрацию сразу
    peak_mmr          INT NOT NULL DEFAULT 100,
    potential_mmr     INT,                         -- вписывают админы вручную
    rank_code         VARCHAR(10) REFERENCES ranks(code),
    calibration_games INT NOT NULL DEFAULT 0,      -- считаем до 10
    is_calibrated     BOOLEAN NOT NULL DEFAULT FALSE,

    -- Любимый/активный титул (какой показываем в шапке)
    active_title_id   INT,                         -- FK добавляется ниже

    referred_by       BIGINT REFERENCES users(id),
    is_banned         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_users_mmr ON users (mmr DESC) WHERE is_calibrated;

-- Счётчик просмотров профиля («глазик»): считаем уникальных зрителей
CREATE TABLE profile_views (
    profile_id  BIGINT NOT NULL REFERENCES users(id),
    viewer_id   BIGINT NOT NULL REFERENCES users(id),
    first_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, viewer_id)
);

-- Рефералы (бонус +10 за первых 10, потом +5 безлимитно)
CREATE TABLE referrals (
    referred_id  BIGINT PRIMARY KEY REFERENCES users(id),  -- приглашённого засчитываем 1 раз
    referrer_id  BIGINT NOT NULL REFERENCES users(id),
    bonus_mmr    INT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Турниры и сетки ----------

CREATE TABLE tournaments (
    id          BIGSERIAL PRIMARY KEY,
    format_id   INT NOT NULL REFERENCES formats(id),
    title       VARCHAR(120) NOT NULL,
    status      VARCHAR(15) NOT NULL DEFAULT 'registration'
                CHECK (status IN ('registration','bracket_ready','running','finished','cancelled')),
    starts_at   TIMESTAMPTZ,
    created_by  BIGINT NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Заявки игроков: админу приходит этот список, он строит сетку
CREATE TABLE tournament_registrations (
    tournament_id  BIGINT NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    user_id        BIGINT NOT NULL REFERENCES users(id),
    registered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tournament_id, user_id)
);

-- Узлы сетки. Нажал «победил» -> игрок переходит в next_match_id
CREATE TABLE bracket_matches (
    id             BIGSERIAL PRIMARY KEY,
    tournament_id  BIGINT NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    round          INT NOT NULL,                 -- 1 = первый раунд
    position       INT NOT NULL,                 -- место в раунде
    player1_id     BIGINT REFERENCES users(id),
    player2_id     BIGINT REFERENCES users(id),
    winner_id      BIGINT REFERENCES users(id),
    next_match_id  BIGINT REFERENCES bracket_matches(id),
    next_slot      SMALLINT CHECK (next_slot IN (1,2)),
    UNIQUE (tournament_id, round, position)
);

-- ---------- Результаты боёв ----------

-- Один бой = одна запись (создаётся автоматически при нажатии «победил»)
CREATE TABLE matches (
    id               BIGSERIAL PRIMARY KEY,
    format_id        INT NOT NULL REFERENCES formats(id),
    tournament_id    BIGINT REFERENCES tournaments(id),
    bracket_match_id BIGINT UNIQUE REFERENCES bracket_matches(id),
    arena_id         INT REFERENCES arenas(id),
    winner_id        BIGINT NOT NULL REFERENCES users(id),
    is_brilliant     BOOLEAN NOT NULL DEFAULT FALSE,   -- «блестящая победа» +10
    duration_sec     INT,
    played_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       BIGINT NOT NULL REFERENCES users(id),
    is_cancelled     BOOLEAN NOT NULL DEFAULT FALSE    -- отмена ошибочного результата
);

-- Участники боя: персонаж, форма, изменение MMR (для вкладки «Матчи»)
CREATE TABLE match_players (
    match_id      BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    user_id       BIGINT NOT NULL REFERENCES users(id),
    is_winner     BOOLEAN NOT NULL,
    character_id  INT REFERENCES characters(id),
    form_id       INT REFERENCES character_forms(id),
    mmr_delta     INT NOT NULL DEFAULT 0,
    mmr_after     INT,
    PRIMARY KEY (match_id, user_id)
);
CREATE INDEX idx_mp_user ON match_players (user_id, match_id DESC);

-- Журнал ВСЕХ изменений MMR (бой, бонус десятки, реферал, ручная правка)
CREATE TABLE mmr_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id),
    delta       INT NOT NULL,
    reason      VARCHAR(20) NOT NULL
                CHECK (reason IN ('register','match','decade_bonus','referral','manual')),
    match_id    BIGINT REFERENCES matches(id),
    note        TEXT,
    created_by  BIGINT REFERENCES users(id),     -- кто сделал (для manual)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_mmr_log_user ON mmr_log (user_id, created_at DESC);

-- Десятки побед: каждые 10 побед = одна запись; здесь решается, дан ли бонус
CREATE TABLE decades (
    user_id         BIGINT NOT NULL REFERENCES users(id),
    decade_number   INT NOT NULL,                -- 1, 2, 3...
    wins            INT NOT NULL DEFAULT 0,      -- до 10
    losses          INT NOT NULL DEFAULT 0,
    is_closed       BOOLEAN NOT NULL DEFAULT FALSE,
    winrate         NUMERIC(5,2),                -- wins/(wins+losses)*100 при закрытии
    bonus_awarded   BOOLEAN,
    bonus_mmr       INT,
    PRIMARY KEY (user_id, decade_number)
);

-- ---------- Статистика (кэш, обновляется при каждом бое) ----------

CREATE TABLE user_stats (
    user_id           BIGINT PRIMARY KEY REFERENCES users(id),
    total_games       INT NOT NULL DEFAULT 0,
    wins              INT NOT NULL DEFAULT 0,
    losses            INT NOT NULL DEFAULT 0,
    win_streak        INT NOT NULL DEFAULT 0,
    best_win_streak   INT NOT NULL DEFAULT 0,
    -- «Огонёк»: считаем недели подряд, где был >= 1 бой
    fire_weeks        INT NOT NULL DEFAULT 0,
    last_fire_week    DATE,                       -- понедельник последней активной недели
    last_game_at      TIMESTAMPTZ
);
-- win rate считаем на лету: общий, последние 10, (позже) последние 100
-- из match_players ORDER BY match_id DESC LIMIT N

CREATE TABLE user_format_stats (
    user_id    BIGINT NOT NULL REFERENCES users(id),
    format_id  INT NOT NULL REFERENCES formats(id),
    wins       INT NOT NULL DEFAULT 0,
    losses     INT NOT NULL DEFAULT 0,
    games      INT NOT NULL DEFAULT 0,            -- для викторин: только games
    PRIMARY KEY (user_id, format_id)
);

CREATE TABLE user_character_stats (
    user_id       BIGINT NOT NULL REFERENCES users(id),
    character_id  INT NOT NULL REFERENCES characters(id),
    wins          INT NOT NULL DEFAULT 0,
    losses        INT NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, character_id)
);
CREATE INDEX idx_ucs_main ON user_character_stats (user_id, wins DESC);  -- топ-3 любимых

-- ---------- Титулы (условия в данных, без кода) ----------

CREATE TABLE titles (
    id               SERIAL PRIMARY KEY,
    name             VARCHAR(50) UNIQUE NOT NULL,   -- 'Ветеран'
    description      VARCHAR(200) NOT NULL,         -- '(Сыграйте 100 боёв)'
    condition_type   VARCHAR(20) NOT NULL
                     CHECK (condition_type IN ('games_total','wins_total','format_games',
                                               'winrate_last_n','win_streak','manual')),
    condition_value  INT,                           -- 50, 100, 500...
    format_id        INT REFERENCES formats(id),    -- для Пбшер/Бпшер/ГИК
    last_n           INT,                           -- для Непобедимого: 10
    min_winrate      NUMERIC(5,2),                  -- 90
    is_recheck       BOOLEAN NOT NULL DEFAULT FALSE,-- пересчитывать периодически (Непобедимый)
    is_active        BOOLEAN NOT NULL DEFAULT TRUE
);
-- Примеры:
-- Опытный:     games_total, 50
-- Ветеран:     games_total, 100
-- Полубог:     wins_total, 500
-- Бог-ФД:      wins_total, 1000
-- Пбшер:       format_games, 100, format=PB
-- ГИК:         format_games, 100, format=QUIZ
-- Непобедимый: winrate_last_n, condition_value=10 (мин. боёв), last_n=10,
--              min_winrate=90, is_recheck=TRUE

CREATE TABLE user_titles (
    user_id     BIGINT NOT NULL REFERENCES users(id),
    title_id    INT NOT NULL REFERENCES titles(id),
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,     -- Непобедимый: гасится/возвращается
    awarded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, title_id)
);

ALTER TABLE users
    ADD CONSTRAINT fk_users_active_title
    FOREIGN KEY (active_title_id) REFERENCES titles(id);

-- ---------- Оценка профиля и «редкость» ----------

CREATE TABLE profile_scores (
    user_id        BIGINT PRIMARY KEY REFERENCES users(id),
    score          NUMERIC(5,2) NOT NULL,           -- 0..100
    top_percent    NUMERIC(5,2) NOT NULL,           -- «ты в топ 13%»
    calculated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Начальные данные ----------

INSERT INTO ranks (code, name, min_mmr, max_mmr, color, sort_order) VALUES
 ('LOW_STAT', 'Низший статист',  0,    99,   '#8a8f98', 1),
 ('STAT',     'Статист',         100,  199,  '#a0a5ad', 2),
 ('HIGH_STAT','Высший статист',  200,  299,  '#b8bdc6', 3),
 ('C',        'C',               300,  399,  '#8c9eff', 4),
 ('C+',       'C+',              400,  499,  '#b388ff', 5),
 ('B',        'B',               500,  599,  '#64b5f6', 6),
 ('B+',       'B+',              600,  699,  '#4dd0e1', 7),
 ('A',        'A',               700,  799,  '#69f0ae', 8),
 ('A+',       'A+',              800,  999,  '#00e676', 9),
 ('S-',       'S- (Low S)',      1000, 1499, '#ffd54f', 10),
 ('S',        'S (Mid S)',       1500, 1999, '#ff9e57', 11),
 ('S+',       'S+ (High S)',     2000, NULL, '#ff5fa2', 12);

INSERT INTO formats (code, name, affects_mmr) VALUES
 ('PB',   'PB',        TRUE),
 ('BP',   'BP',        TRUE),
 ('PBP',  'PBP',       TRUE),
 ('QUIZ', 'Викторины', FALSE);

INSERT INTO titles (name, description, condition_type, condition_value, format_id, last_n, min_winrate, is_recheck)
VALUES
 ('Опытный',      '(Сыграйте 50 боёв)',          'games_total', 50,   NULL, NULL, NULL, FALSE),
 ('Ветеран',      '(Сыграйте 100 боёв)',         'games_total', 100,  NULL, NULL, NULL, FALSE),
 ('Полубог',      '(Одержите 500 побед)',        'wins_total',  500,  NULL, NULL, NULL, FALSE),
 ('Бог-ФД',       '(Одержите 1000 побед)',       'wins_total',  1000, NULL, NULL, NULL, FALSE),
 ('Непобедимый',  '(Win rate 90%+ в последних 10 боях, от 10 боёв)',
                                                  'winrate_last_n', 10, NULL, 10, 90, TRUE);
-- Пбшер / Бпшер / ГИК добавляются после заполнения formats через format_id.
