"""Ingestion d'une réponse de fournisseur dans le journal à l'instant T.

Un passage = une transaction :
  1. verrou par (fournisseur, périmètre) et contrôle que le temps avance ;
  2. stockage de la réponse brute (dédupliquée par empreinte) ;
  3. résolution des entités (compétition, équipes, bookmakers, événement) ;
  4. couverture (poll_event) et horaire (event_schedule, si changé) ;
  5. journal des cotes : une ligne par cote nouvelle ou modifiée, une ligne price=NULL
     par cote qui a disparu, et mise à jour de la projection odds_current.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import TupleRow

from parisportif.models import EventSnapshot, ProviderResponse
from parisportif.normalize import normalize_team_name

Conn = psycopg.Connection[TupleRow]

# Deux références d'un même match chez deux sources : mêmes équipes, coup d'envoi proche.
EVENT_MATCH_WINDOW = timedelta(hours=36)


class OutOfOrderError(Exception):
    """L'observation est antérieure ou égale à une observation déjà enregistrée."""


@dataclass(frozen=True, slots=True)
class IngestStats:
    poll_id: int
    events_seen: int
    quotes_inserted: int
    quotes_removed: int
    schedule_changes: int
    new_events: int
    new_teams: int


def start_poll(
    conn: Conn, *, provider: str, scope: str, markets: tuple[str, ...], request_url: str | None
) -> int:
    """Crée le passage avant l'appel réseau, pour tracer aussi les échecs. Commit immédiat."""
    with conn.transaction():
        conn.execute("INSERT INTO provider (key) VALUES (%s) ON CONFLICT DO NOTHING", (provider,))
        row = conn.execute(
            """
            INSERT INTO poll_run (provider, scope, markets, request_url)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (provider, scope, list(markets), request_url),
        ).fetchone()
    assert row is not None
    return int(row[0])


def fail_poll(conn: Conn, poll_id: int, *, error: str, http_status: int | None = None) -> None:
    with conn.transaction():
        conn.execute(
            """
            UPDATE poll_run
            SET status = 'error', error = %s, http_status = %s, finished_at = now()
            WHERE id = %s AND status = 'running'
            """,
            (error[:2000], http_status, poll_id),
        )


class _Resolver:
    """Résolution d'entités avec cache local au passage."""

    def __init__(self, conn: Conn, provider: str, roles: Mapping[str, str]) -> None:
        self.conn = conn
        self.provider = provider
        self.roles = roles
        self.new_events = 0
        self.new_teams = 0
        self._teams: dict[str, int] = {}
        self._competitions: dict[str, int] = {}
        self._bookmakers: dict[str, str] = {}

    def competition(self, ref: str, name: str) -> int:
        if ref in self._competitions:
            return self._competitions[ref]
        row = self.conn.execute(
            "SELECT competition_id FROM competition_source"
            " WHERE provider = %s AND provider_ref = %s",
            (self.provider, ref),
        ).fetchone()
        if row is None:
            created = self.conn.execute(
                "INSERT INTO competition (name) VALUES (%s) RETURNING id", (name,)
            ).fetchone()
            assert created is not None
            comp_id = int(created[0])
            self.conn.execute(
                """
                INSERT INTO competition_source (provider, provider_ref, competition_id)
                VALUES (%s, %s, %s)
                """,
                (self.provider, ref, comp_id),
            )
        else:
            comp_id = int(row[0])
        self._competitions[ref] = comp_id
        return comp_id

    def team(self, name: str) -> int:
        norm = normalize_team_name(name)
        if norm in self._teams:
            return self._teams[norm]
        row = self.conn.execute(
            "SELECT team_id FROM team_alias WHERE alias_norm = %s", (norm,)
        ).fetchone()
        if row is None:
            # Nom inconnu : on crée l'équipe plutôt que de deviner. Un doublon se voit et se
            # rattache (`parisportif alias`) ; une fusion à tort corromprait l'historique.
            # Si une autre source a déjà rempli le référentiel, l'équipe est à vérifier.
            other = self.conn.execute(
                "SELECT EXISTS (SELECT 1 FROM team_alias WHERE provider <> %s)",
                (self.provider,),
            ).fetchone()
            review = bool(other and other[0])
            created = self.conn.execute(
                "INSERT INTO team (name, needs_review) VALUES (%s, %s) RETURNING id",
                (name, review),
            ).fetchone()
            assert created is not None
            team_id = int(created[0])
            self.conn.execute(
                """
                INSERT INTO team_alias (alias_norm, team_id, origin, provider)
                VALUES (%s, %s, 'auto', %s)
                """,
                (norm, team_id, self.provider),
            )
            self.new_teams += 1
        else:
            team_id = int(row[0])
        self._teams[norm] = team_id
        return team_id

    def bookmaker(self, ref: str, name: str) -> str:
        """Clé canonique du bookmaker pour CETTE source.

        Une clé n'appartient qu'à une source : si une autre source livre déjà « winamax_fr »,
        celle-ci écrit sous « <source>:winamax_fr ». Deux flux d'un même book ne s'écrasent
        donc jamais, et un retrait vu par l'un n'efface pas la cote que l'autre voit encore.
        """
        if ref in self._bookmakers:
            return self._bookmakers[ref]
        row = self.conn.execute(
            "SELECT bookmaker FROM bookmaker_source WHERE provider = %s AND provider_ref = %s",
            (self.provider, ref),
        ).fetchone()
        if row is None:
            taken = self.conn.execute(
                "SELECT EXISTS (SELECT 1 FROM bookmaker WHERE key = %s)", (ref,)
            ).fetchone()
            key = f"{self.provider}:{ref}" if taken and taken[0] else ref
            self.conn.execute(
                """
                INSERT INTO bookmaker (key, name, role) VALUES (%s, %s, %s)
                ON CONFLICT (key) DO NOTHING
                """,
                (key, name, self.roles.get(key, self.roles.get(ref, "unknown"))),
            )
            self.conn.execute(
                """
                INSERT INTO bookmaker_source (provider, provider_ref, bookmaker)
                VALUES (%s, %s, %s)
                """,
                (self.provider, ref, key),
            )
        else:
            key = str(row[0])
            # Rôle ajouté au fichier après la découverte : appliqué tant que la base dit
            # 'unknown' (un rôle fixé à la main avec set-role n'est jamais écrasé).
            role = self.roles.get(key, self.roles.get(ref))
            if role is not None and role != "unknown":
                self.conn.execute(
                    "UPDATE bookmaker SET role = %s WHERE key = %s AND role = 'unknown'",
                    (role, key),
                )
        self._bookmakers[ref] = key
        return key

    def event(self, snap: EventSnapshot) -> int:
        row = self.conn.execute(
            "SELECT event_id FROM event_source WHERE provider = %s AND provider_ref = %s",
            (self.provider, snap.provider_ref),
        ).fetchone()
        if row is not None:
            return int(row[0])
        comp_id = self.competition(snap.competition_ref, snap.competition_name)
        home_id, away_id = self.team(snap.home_team), self.team(snap.away_team)
        # Même match déjà connu (autre référence, autre source) ?
        match = self.conn.execute(
            """
            SELECT e.id
            FROM event e
            CROSS JOIN LATERAL (SELECT kickoff_as_of(e.id, 'infinity') AS commence_time) k
            WHERE e.home_team_id = %s AND e.away_team_id = %s
              AND k.commence_time BETWEEN %s AND %s
            ORDER BY abs(extract(epoch FROM k.commence_time - %s))
            LIMIT 1
            """,
            (
                home_id,
                away_id,
                snap.commence_time - EVENT_MATCH_WINDOW,
                snap.commence_time + EVENT_MATCH_WINDOW,
                snap.commence_time,
            ),
        ).fetchone()
        if match is not None:
            event_id = int(match[0])
        else:
            created = self.conn.execute(
                """
                INSERT INTO event (competition_id, home_team_id, away_team_id)
                VALUES (%s, %s, %s) RETURNING id
                """,
                (comp_id, home_id, away_id),
            ).fetchone()
            assert created is not None
            event_id = int(created[0])
            self.new_events += 1
        self.conn.execute(
            "INSERT INTO event_source (provider, provider_ref, event_id) VALUES (%s, %s, %s)",
            (self.provider, snap.provider_ref, event_id),
        )
        return event_id


def _payload_sha(body: object) -> tuple[str, str]:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest(), text


def ingest(
    conn: Conn, poll_id: int, resp: ProviderResponse, *, roles: Mapping[str, str] | None = None
) -> IngestStats:
    """Écrit un passage réussi. Tout ou rien : en cas d'erreur, rien n'est écrit."""
    roles = roles or {}
    obs = resp.observed_at
    if obs.tzinfo is None:
        raise ValueError("observed_at doit porter un fuseau (UTC)")
    obs = obs.astimezone(UTC)

    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"poll:{resp.provider}:{resp.scope}",)
        )
        last = conn.execute(
            """
            SELECT max(observed_at) FROM poll_run
            WHERE provider = %s AND scope = %s AND status = 'ok'
            """,
            (resp.provider, resp.scope),
        ).fetchone()
        if last is not None and last[0] is not None and last[0] >= obs:
            raise OutOfOrderError(f"observation {obs.isoformat()} <= dernier passage {last[0]}")

        sha, text = _payload_sha(resp.body)
        conn.execute(
            "INSERT INTO raw_payload (sha256, body) VALUES (%s, %s::jsonb) ON CONFLICT DO NOTHING",
            (sha, text),
        )

        resolver = _Resolver(conn, resp.provider, roles)
        snaps: dict[int, EventSnapshot] = {}
        for snap in resp.events:
            # Deux références source pour un même match : la première l'emporte.
            snaps.setdefault(resolver.event(snap), snap)
        event_ids = sorted(snaps)

        # Verrou par match (ordre croissant : pas d'interblocage) : deux passages qui touchent
        # les mêmes matchs, quelle que soit leur source, s'exécutent l'un après l'autre.
        for event_id in event_ids:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"event:{event_id}",))

        # Contrôle d'ordre propre à CETTE source : une autre source peut écrire plus tard.
        if event_ids:
            seen = conn.execute(
                """
                SELECT greatest(
                    (SELECT max(observed_at) FROM odds_current
                     WHERE event_id = ANY(%(ev)s) AND provider = %(p)s),
                    (SELECT max(observed_at) FROM event_schedule
                     WHERE event_id = ANY(%(ev)s) AND provider = %(p)s)
                )
                """,
                {"ev": event_ids, "p": resp.provider},
            ).fetchone()
            if seen is not None and seen[0] is not None and seen[0] >= obs:
                raise OutOfOrderError(f"cette source a déjà écrit à {seen[0]} pour ces matchs")

        schedule_changes = 0
        rows: list[tuple[object, ...]] = []
        for event_id in event_ids:
            snap = snaps[event_id]
            conn.execute(
                "INSERT INTO poll_event (poll_id, event_id, observed_at) VALUES (%s, %s, %s)",
                (poll_id, event_id, obs),
            )
            current = conn.execute(
                """
                SELECT commence_time FROM event_schedule
                WHERE event_id = %s AND provider = %s
                ORDER BY observed_at DESC LIMIT 1
                """,
                (event_id, resp.provider),
            ).fetchone()
            if current is None or current[0] != snap.commence_time:
                conn.execute(
                    """
                    INSERT INTO event_schedule
                        (event_id, provider, observed_at, commence_time, poll_id)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (event_id, resp.provider, obs, snap.commence_time, poll_id),
                )
                schedule_changes += 1
            for q in snap.quotes:
                rows.append(
                    (
                        event_id,
                        resolver.bookmaker(q.bookmaker_ref, q.bookmaker_name),
                        q.market,
                        q.line,
                        q.selection,
                        q.price,
                        q.source_updated_at,
                    )
                )

        inserted = removed = 0
        if event_ids:
            inserted, removed = _write_quotes(conn, poll_id, resp, obs, event_ids, rows)

        conn.execute(
            """
            UPDATE poll_run SET
                status = 'ok', observed_at = %s, finished_at = now(), raw_sha256 = %s,
                http_status = %s, quota_remaining = %s, quota_used = %s, quota_last_cost = %s,
                events_seen = %s, quotes_inserted = %s, quotes_removed = %s,
                unmapped_outcomes = %s
            WHERE id = %s AND status = 'running'
            """,
            (
                obs,
                sha,
                resp.http_status,
                resp.quota_remaining,
                resp.quota_used,
                resp.quota_last_cost,
                len(event_ids),
                inserted,
                removed,
                resp.unmapped_outcomes,
                poll_id,
            ),
        )
        updated = conn.execute("SELECT status FROM poll_run WHERE id = %s", (poll_id,)).fetchone()
        if updated is None or updated[0] != "ok":
            raise RuntimeError(f"passage {poll_id} introuvable ou déjà clos")

    return IngestStats(
        poll_id=poll_id,
        events_seen=len(event_ids),
        quotes_inserted=inserted,
        quotes_removed=removed,
        schedule_changes=schedule_changes,
        new_events=resolver.new_events,
        new_teams=resolver.new_teams,
    )


def _write_quotes(
    conn: Conn,
    poll_id: int,
    resp: ProviderResponse,
    obs: datetime,
    event_ids: list[int],
    rows: list[tuple[object, ...]],
) -> tuple[int, int]:
    # Table de travail de la session, vidée à chaque passage : fonctionne aussi quand
    # l'appelant a déjà une transaction ouverte (ingest n'est alors qu'un savepoint).
    conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _incoming (
            event_id bigint, bookmaker text, market text, line numeric(6, 2),
            selection text, price numeric(9, 3), source_updated_at timestamptz,
            PRIMARY KEY (event_id, bookmaker, market, line, selection)
        ) ON COMMIT DELETE ROWS
        """
    )
    conn.execute("TRUNCATE _incoming")
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY _incoming (event_id, bookmaker, market, line, selection, price,"
            " source_updated_at) FROM STDIN"
        ) as copy,
    ):
        for row in rows:
            copy.write_row(row)

    params = {
        "obs": obs,
        "poll": poll_id,
        "provider": resp.provider,
        "events": event_ids,
        "markets": list(resp.markets),
    }

    # Retraits : cote courante de CE fournisseur, sur un marché couvert par ce passage,
    # pour un événement vu dans ce passage, mais absente de la réponse.
    removed_row = conn.execute(
        """
        WITH gone AS (
            INSERT INTO odds_quote (event_id, bookmaker, market, line, selection,
                                    observed_at, price, source_updated_at, poll_id)
            SELECT c.event_id, c.bookmaker, c.market, c.line, c.selection,
                   %(obs)s, NULL, NULL, %(poll)s
            FROM odds_current c
            WHERE c.event_id = ANY(%(events)s)
              AND c.market = ANY(%(markets)s)
              AND c.provider = %(provider)s
              AND c.price IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM _incoming i
                  WHERE i.event_id = c.event_id AND i.bookmaker = c.bookmaker
                    AND i.market = c.market AND i.line = c.line AND i.selection = c.selection
              )
            RETURNING event_id, bookmaker, market, line, selection
        ), upd AS (
            UPDATE odds_current c
            SET price = NULL, observed_at = %(obs)s, poll_id = %(poll)s
            FROM gone g
            WHERE c.event_id = g.event_id AND c.bookmaker = g.bookmaker
              AND c.market = g.market AND c.line = g.line AND c.selection = g.selection
            RETURNING 1
        )
        SELECT count(*) FROM upd
        """,
        params,
    ).fetchone()

    # Nouvelles cotes ou cotes modifiées (y compris réapparition après un retrait).
    inserted_row = conn.execute(
        """
        WITH changed AS (
            INSERT INTO odds_quote (event_id, bookmaker, market, line, selection,
                                    observed_at, price, source_updated_at, poll_id)
            SELECT i.event_id, i.bookmaker, i.market, i.line, i.selection,
                   %(obs)s, i.price, i.source_updated_at, %(poll)s
            FROM _incoming i
            LEFT JOIN odds_current c
              ON c.event_id = i.event_id AND c.bookmaker = i.bookmaker
             AND c.market = i.market AND c.line = i.line AND c.selection = i.selection
            WHERE c.price IS DISTINCT FROM i.price
            RETURNING event_id, bookmaker, market, line, selection, price
        ), up AS (
            INSERT INTO odds_current (event_id, bookmaker, market, line, selection,
                                      price, observed_at, provider, poll_id)
            SELECT event_id, bookmaker, market, line, selection, price,
                   %(obs)s, %(provider)s, %(poll)s
            FROM changed
            ON CONFLICT (event_id, bookmaker, market, line, selection) DO UPDATE
            SET price = EXCLUDED.price, observed_at = EXCLUDED.observed_at,
                provider = EXCLUDED.provider, poll_id = EXCLUDED.poll_id
            WHERE odds_current.observed_at < EXCLUDED.observed_at
            RETURNING 1
        )
        SELECT count(*) FROM up
        """,
        params,
    ).fetchone()
    assert removed_row is not None and inserted_row is not None
    return int(inserted_row[0]), int(removed_row[0])


def utcnow() -> datetime:
    return datetime.now(UTC)
