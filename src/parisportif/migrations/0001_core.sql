-- Schéma « à l'instant T » (point-in-time).
--
-- Règle unique : toute donnée observée est AJOUTÉE, jamais modifiée ni supprimée.
-- observed_at = instant où le collecteur a reçu l'information (son horloge, en UTC).
-- C'est la seule date qui répond à « que savait-on à l'instant T ? ». Les dates annoncées
-- par les sources (source_updated_at) sont conservées à titre informatif, jamais utilisées
-- pour le point-in-time : une source peut antidater, postdater ou se tromper.
--
-- Trois familles de tables :
--   référentiel      : competition, team, bookmaker, event (+ correspondances par source)
--   observations     : odds_quote, event_schedule, poll_event, raw_payload (ajout seul)
--   projection       : odds_current (état courant, dérivé du journal, reconstructible)

-- ---------------------------------------------------------------- référentiel

CREATE TABLE provider (
    key         text PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE competition (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE competition_source (
    provider        text NOT NULL REFERENCES provider (key),
    provider_ref    text NOT NULL,
    competition_id  bigint NOT NULL REFERENCES competition (id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, provider_ref)
);

CREATE TABLE team (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text NOT NULL,
    needs_review  boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Un même club porte un nom différent selon la source (« Paris Saint Germain », « PSG »…).
-- alias_norm = nom normalisé (minuscules, sans accents ni suffixes juridiques type FC/AFC).
CREATE TABLE team_alias (
    alias_norm  text PRIMARY KEY,
    team_id     bigint NOT NULL REFERENCES team (id),
    origin      text NOT NULL CHECK (origin IN ('auto', 'manual')),
    provider    text REFERENCES provider (key),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bookmaker (
    key         text PRIMARY KEY,
    name        text NOT NULL,
    -- fr_licensed     : agréé ANJ, là où l'utilisateur peut parier
    -- sharp_reference : sert à estimer la probabilité juste (ex. Pinnacle), jamais un débouché
    role        text NOT NULL DEFAULT 'unknown'
                CHECK (role IN ('fr_licensed', 'sharp_reference', 'exchange', 'other', 'unknown')),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bookmaker_source (
    provider      text NOT NULL REFERENCES provider (key),
    provider_ref  text NOT NULL,
    bookmaker     text NOT NULL REFERENCES bookmaker (key),
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, provider_ref)
);

-- L'horaire n'est PAS une colonne de event : il change (report, décalage TV).
-- Il vit dans event_schedule, comme une observation datée.
CREATE TABLE event (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    competition_id  bigint NOT NULL REFERENCES competition (id),
    home_team_id    bigint NOT NULL REFERENCES team (id),
    away_team_id    bigint NOT NULL REFERENCES team (id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (home_team_id <> away_team_id)
);
CREATE INDEX event_teams_idx ON event (home_team_id, away_team_id);

CREATE TABLE event_source (
    provider      text NOT NULL REFERENCES provider (key),
    provider_ref  text NOT NULL,
    event_id      bigint NOT NULL REFERENCES event (id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, provider_ref)
);

-- Marchés canoniques et jeu complet d'issues (nécessaire pour retirer la marge).
CREATE TABLE market_type (
    key       text PRIMARY KEY,
    has_line  boolean NOT NULL
);

CREATE TABLE market_selection (
    market     text NOT NULL REFERENCES market_type (key),
    selection  text NOT NULL,
    PRIMARY KEY (market, selection)
);

INSERT INTO market_type (key, has_line) VALUES
    ('1x2', false),
    ('total_goals', true),
    ('asian_handicap', true),   -- line exprimée du point de vue de l'équipe à domicile
    ('btts', false),
    ('draw_no_bet', false);

INSERT INTO market_selection (market, selection) VALUES
    ('1x2', 'home'), ('1x2', 'draw'), ('1x2', 'away'),
    ('total_goals', 'over'), ('total_goals', 'under'),
    ('asian_handicap', 'home'), ('asian_handicap', 'away'),
    ('btts', 'yes'), ('btts', 'no'),
    ('draw_no_bet', 'home'), ('draw_no_bet', 'away');

-- ---------------------------------------------------------------- collecte

CREATE TABLE raw_payload (
    sha256      text PRIMARY KEY,
    body        jsonb NOT NULL,
    first_seen  timestamptz NOT NULL DEFAULT now()
);

-- Un passage du collecteur sur un périmètre (ex. un championnat).
-- Métadonnée opérationnelle : c'est la seule table « observation » dont le statut évolue.
CREATE TABLE poll_run (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider           text NOT NULL REFERENCES provider (key),
    scope              text NOT NULL,
    markets            text[] NOT NULL,          -- marchés canoniques couverts par ce passage
    request_url        text,                     -- sans la clé d'API
    started_at         timestamptz NOT NULL DEFAULT now(),
    observed_at        timestamptz,              -- réception de la réponse = instant de connaissance
    finished_at        timestamptz,
    status             text NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'ok', 'error')),
    http_status        int,
    error              text,
    raw_sha256         text REFERENCES raw_payload (sha256),
    quota_remaining    int,
    quota_used         int,
    quota_last_cost    int,
    events_seen        int,
    quotes_inserted    int,
    quotes_removed     int,
    unmapped_outcomes  int,
    CHECK (status <> 'ok' OR observed_at IS NOT NULL)
);
CREATE INDEX poll_run_ok_idx ON poll_run (provider, scope, observed_at DESC) WHERE status = 'ok';

-- Couverture : « à observed_at, ce passage a vu l'événement et TOUT ce qui y était coté
-- sur les marchés demandés ». Sert à savoir jusqu'à quand une cote est confirmée (fraîcheur).
CREATE TABLE poll_event (
    poll_id      bigint NOT NULL REFERENCES poll_run (id),
    event_id     bigint NOT NULL REFERENCES event (id),
    observed_at  timestamptz NOT NULL,
    PRIMARY KEY (poll_id, event_id)
);
CREATE INDEX poll_event_event_idx ON poll_event (event_id, observed_at DESC);

-- Horaire de coup d'envoi tel que connu à chaque instant. Une ligne par changement.
CREATE TABLE event_schedule (
    event_id       bigint NOT NULL REFERENCES event (id),
    observed_at    timestamptz NOT NULL,
    commence_time  timestamptz NOT NULL,
    poll_id        bigint REFERENCES poll_run (id),
    PRIMARY KEY (event_id, observed_at)
);

-- Journal des cotes : une ligne par CHANGEMENT d'état d'une cote.
-- price NULL = la cote n'est plus proposée (retrait, suspension) à partir de observed_at.
-- L'état à l'instant T d'une cote = sa dernière ligne avec observed_at <= T.
CREATE TABLE odds_quote (
    event_id           bigint NOT NULL REFERENCES event (id),
    bookmaker          text NOT NULL REFERENCES bookmaker (key),
    market             text NOT NULL,
    line               numeric(6, 2) NOT NULL DEFAULT 0,  -- 0 pour les marchés sans ligne
    selection          text NOT NULL,
    observed_at        timestamptz NOT NULL,
    price              numeric(9, 3),
    source_updated_at  timestamptz,
    poll_id            bigint NOT NULL REFERENCES poll_run (id),
    FOREIGN KEY (market, selection) REFERENCES market_selection (market, selection),
    CHECK (price IS NULL OR price > 1)
);
CREATE UNIQUE INDEX odds_quote_key_idx
    ON odds_quote (event_id, bookmaker, market, line, selection, observed_at DESC);

-- Projection de l'état courant (dernière ligne de chaque cote). Dérivée du journal,
-- maintenue dans la même transaction que lui : elle rend la détection de changements
-- O(cotes courantes) au lieu de O(historique). Reconstructible avec rebuild_odds_current().
CREATE TABLE odds_current (
    event_id     bigint NOT NULL REFERENCES event (id),
    bookmaker    text NOT NULL REFERENCES bookmaker (key),
    market       text NOT NULL,
    line         numeric(6, 2) NOT NULL,
    selection    text NOT NULL,
    price        numeric(9, 3),
    observed_at  timestamptz NOT NULL,
    provider     text NOT NULL REFERENCES provider (key),
    poll_id      bigint NOT NULL REFERENCES poll_run (id),
    PRIMARY KEY (event_id, bookmaker, market, line, selection)
);

-- ---------------------------------------------------------------- ajout seul

CREATE FUNCTION forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % en ajout seul : % interdit (historique à l''instant T)',
        TG_TABLE_NAME, TG_OP;
END $$;

CREATE TRIGGER odds_quote_append_only BEFORE UPDATE OR DELETE ON odds_quote
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER odds_quote_no_truncate BEFORE TRUNCATE ON odds_quote
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER event_schedule_append_only BEFORE UPDATE OR DELETE ON event_schedule
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER event_schedule_no_truncate BEFORE TRUNCATE ON event_schedule
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER poll_event_append_only BEFORE UPDATE OR DELETE ON poll_event
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER poll_event_no_truncate BEFORE TRUNCATE ON poll_event
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER raw_payload_append_only BEFORE UPDATE OR DELETE ON raw_payload
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER raw_payload_no_truncate BEFORE TRUNCATE ON raw_payload
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- requêtes à l'instant T

-- Cotes proposées à l'instant T pour un événement (les retraits sont exclus).
CREATE FUNCTION odds_as_of(p_event_id bigint, p_at timestamptz)
RETURNS TABLE (
    bookmaker text, market text, line numeric, selection text,
    price numeric, observed_at timestamptz
)
LANGUAGE sql STABLE AS $$
    SELECT s.bookmaker, s.market, s.line, s.selection, s.price, s.observed_at
    FROM (
        SELECT DISTINCT ON (q.bookmaker, q.market, q.line, q.selection)
               q.bookmaker, q.market, q.line, q.selection, q.price, q.observed_at
        FROM odds_quote q
        WHERE q.event_id = p_event_id
          AND q.observed_at <= p_at
        ORDER BY q.bookmaker, q.market, q.line, q.selection, q.observed_at DESC
    ) s
    WHERE s.price IS NOT NULL
$$;

-- Horaire de coup d'envoi connu à l'instant T.
CREATE FUNCTION kickoff_as_of(p_event_id bigint, p_at timestamptz)
RETURNS timestamptz
LANGUAGE sql STABLE AS $$
    SELECT s.commence_time
    FROM event_schedule s
    WHERE s.event_id = p_event_id
      AND s.observed_at <= p_at
    ORDER BY s.observed_at DESC
    LIMIT 1
$$;

-- Dernier instant <= T où un passage réussi a vu l'événement ET couvrait ce marché.
-- Une cote n'est fiable à T que si cette confirmation est récente.
CREATE FUNCTION last_confirmed(p_event_id bigint, p_market text, p_at timestamptz)
RETURNS timestamptz
LANGUAGE sql STABLE AS $$
    SELECT max(pe.observed_at)
    FROM poll_event pe
    JOIN poll_run pr ON pr.id = pe.poll_id
    WHERE pe.event_id = p_event_id
      AND pe.observed_at <= p_at
      AND p_market = ANY (pr.markets)
$$;

-- Événements connus à T dont le coup d'envoi (connu à T) tombe dans ]T, T + horizon].
CREATE FUNCTION events_as_of(p_at timestamptz, p_horizon interval)
RETURNS TABLE (
    event_id bigint, competition_id bigint, home_team_id bigint, away_team_id bigint,
    commence_time timestamptz
)
LANGUAGE sql STABLE AS $$
    SELECT e.id, e.competition_id, e.home_team_id, e.away_team_id, k.commence_time
    FROM event e
    CROSS JOIN LATERAL (
        SELECT s.commence_time
        FROM event_schedule s
        WHERE s.event_id = e.id
          AND s.observed_at <= p_at
        ORDER BY s.observed_at DESC
        LIMIT 1
    ) k
    WHERE k.commence_time > p_at
      AND k.commence_time <= p_at + p_horizon
$$;

-- Reconstruit la projection courante à partir du journal (en cas de doute ou de bug).
CREATE FUNCTION rebuild_odds_current() RETURNS void
LANGUAGE sql AS $$
    DELETE FROM odds_current;
    INSERT INTO odds_current (event_id, bookmaker, market, line, selection, price,
                              observed_at, provider, poll_id)
    SELECT DISTINCT ON (q.event_id, q.bookmaker, q.market, q.line, q.selection)
           q.event_id, q.bookmaker, q.market, q.line, q.selection, q.price,
           q.observed_at, pr.provider, q.poll_id
    FROM odds_quote q
    JOIN poll_run pr ON pr.id = q.poll_id
    ORDER BY q.event_id, q.bookmaker, q.market, q.line, q.selection, q.observed_at DESC;
$$;
