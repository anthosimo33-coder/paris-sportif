"""Ligne de commande : `parisportif <commande>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from parisportif import db, pit
from parisportif.collector import poll_once, run_forever
from parisportif.config import ROLES, Settings, load_roles
from parisportif.ingest import ingest, start_poll
from parisportif.models import ProviderResponse
from parisportif.normalize import normalize_team_name
from parisportif.providers import the_odds_api

SELECTION_LABELS = {
    "home": "1",
    "draw": "N",
    "away": "2",
    "over": "Plus",
    "under": "Moins",
    "yes": "Oui",
    "no": "Non",
}
MARKET_LABELS = {
    "1x2": "1N2",
    "total_goals": "Buts",
    "asian_handicap": "Handicap",
    "btts": "Les 2 marquent",
    "draw_no_bet": "Rembourse si nul",
}


def _parse_at(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError("préciser le fuseau, ex. 2026-11-01T18:00:00Z")
    return dt.astimezone(UTC)


def _provider(settings: Settings) -> the_odds_api.TheOddsApi:
    return the_odds_api.TheOddsApi(
        settings.odds_api_key,
        regions=settings.regions,
        markets=settings.markets,
        bookmakers=settings.bookmakers,
        horizon=timedelta(days=settings.horizon_days),
    )


def cmd_migrate(settings: Settings, _args: argparse.Namespace) -> int:
    with db.connect(settings.database_url) as conn:
        applied = db.migrate(conn)
    print("migrations appliquées :", ", ".join(applied) if applied else "aucune (à jour)")
    return 0


def cmd_collect(settings: Settings, args: argparse.Namespace) -> int:
    roles = load_roles(settings.roles_file)
    provider = _provider(settings)
    scopes = tuple(args.scope) if args.scope else settings.sports
    with db.connect(settings.database_url) as conn:
        if args.once:
            code = 0
            for scope in scopes:
                out = poll_once(conn, provider, scope, roles, datetime.now(UTC))
                if out.stats is None:
                    print(f"{scope} : ÉCHEC {out.error}", file=sys.stderr)
                    code = 1
                    continue
                s, r = out.stats, out.response
                assert r is not None
                print(
                    f"{scope} : {s.events_seen} matchs, {s.quotes_inserted} cotes écrites, "
                    f"{s.quotes_removed} retraits, {r.unmapped_outcomes} issues non reconnues, "
                    f"quota restant {r.quota_remaining}"
                )
            return code
        run_forever(conn, provider, scopes, roles, quota_reserve=settings.quota_reserve)
    return 0


def cmd_ingest_file(settings: Settings, args: argparse.Namespace) -> int:
    """Ingère une réponse brute enregistrée (rejeu, démo, tests)."""
    data = json.loads(Path(args.path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "response" in data:
        body, scope = data["response"], data.get("scope", args.scope)
        observed = _parse_at(data.get("observed_at") or args.observed_at)
    else:
        body, scope = data, args.scope
        if args.observed_at is None:
            print("--observed-at requis pour une réponse brute", file=sys.stderr)
            return 2
        observed = _parse_at(args.observed_at)
    if not scope:
        print("--scope requis", file=sys.stderr)
        return 2
    markets = tuple(dict.fromkeys(the_odds_api.MARKET_MAP[m] for m in settings.markets))
    events, unmapped, warnings = the_odds_api.parse_odds_response(
        body, scope=scope, markets=markets, observed_at=observed
    )
    resp = ProviderResponse(
        provider=the_odds_api.NAME,
        scope=scope,
        markets=markets,
        observed_at=observed,
        events=events,
        body=body,
        request_url=f"file:{args.path}",
        unmapped_outcomes=unmapped,
        warnings=warnings,
    )
    roles = load_roles(settings.roles_file)
    with db.connect(settings.database_url) as conn:
        poll_id = start_poll(
            conn,
            provider=resp.provider,
            scope=scope,
            markets=markets,
            request_url=resp.request_url,
        )
        stats = ingest(conn, poll_id, resp, roles=roles)
    print(
        f"{args.path} @ {observed.isoformat()} : {stats.events_seen} matchs, "
        f"{stats.quotes_inserted} cotes écrites, {stats.quotes_removed} retraits"
    )
    return 0


def cmd_bookmakers(settings: Settings, _args: argparse.Namespace) -> int:
    with db.connect(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT b.key, b.name, b.role, count(c.*) FILTER (WHERE c.price IS NOT NULL)
            FROM bookmaker b LEFT JOIN odds_current c ON c.bookmaker = b.key
            GROUP BY b.key, b.name, b.role ORDER BY b.role, b.key
            """
        ).fetchall()
    print(f"{'clé':<24}{'rôle':<18}{'cotes actives':>14}  nom")
    for key, name, role, n in rows:
        print(f"{key!s:<24}{role!s:<18}{n!s:>14}  {name}")
    return 0


def cmd_set_role(settings: Settings, args: argparse.Namespace) -> int:
    with db.connect(settings.database_url) as conn, conn.transaction():
        cur = conn.execute(
            "UPDATE bookmaker SET role = %s WHERE key = %s", (args.role, args.bookmaker)
        )
        if cur.rowcount == 0:
            print(f"bookmaker inconnu : {args.bookmaker}", file=sys.stderr)
            return 1
    print(f"{args.bookmaker} -> {args.role}")
    return 0


def cmd_alias(settings: Settings, args: argparse.Namespace) -> int:
    norm = normalize_team_name(args.alias)
    with db.connect(settings.database_url) as conn, conn.transaction():
        team = conn.execute("SELECT name FROM team WHERE id = %s", (args.team_id,)).fetchone()
        if team is None:
            print(f"équipe inconnue : {args.team_id}", file=sys.stderr)
            return 1
        conn.execute(
            """
            INSERT INTO team_alias (alias_norm, team_id, origin) VALUES (%s, %s, 'manual')
            ON CONFLICT (alias_norm) DO UPDATE SET team_id = EXCLUDED.team_id, origin = 'manual'
            """,
            (norm, args.team_id),
        )
    print(f"« {args.alias} » ({norm}) -> {team[0]}")
    return 0


def _fmt_line(market: str, line: Decimal) -> str:
    if market == "total_goals":
        return f" {line}"
    if market == "asian_handicap":
        return f" {float(line):+g}"
    return ""


def cmd_scan(settings: Settings, args: argparse.Namespace) -> int:
    at = _parse_at(args.at)
    with db.connect(settings.database_url) as conn:
        signals = pit.value_scan(
            conn,
            at,
            min_edge=args.min_edge,
            horizon=timedelta(hours=args.horizon_hours),
            max_staleness=timedelta(minutes=args.max_staleness_min),
        )
    print(f"Écarts visibles à {at:%Y-%m-%d %H:%M} UTC (seuil {args.min_edge:.0%})\n")
    if not signals:
        print("Aucun écart au-dessus du seuil.")
        return 0
    for s in signals:
        pari = (
            f"{MARKET_LABELS.get(s.market, s.market)} "
            f"{SELECTION_LABELS.get(s.selection, s.selection)}{_fmt_line(s.market, s.line)}"
        )
        age = int((at - s.price_seen_at).total_seconds() // 60)
        flag = "  ⚠ trop beau : vérifier (erreur de cote ?)" if s.suspect else ""
        print(f"{s.home} – {s.away}   {s.kickoff:%a %d/%m %H:%M} UTC")
        print(
            f"  {pari:<22} {s.bookmaker} {s.price:.2f}   juste {s.fair_price:.2f}   "
            f"{s.edge:+.1%}   mise {s.kelly_quarter:.1%} bankroll   cote vue il y a {age} min"
            f"{flag}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="parisportif", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="crée / met à jour le schéma").set_defaults(fn=cmd_migrate)

    c = sub.add_parser("collect", help="collecte les cotes (boucle, ou --once)")
    c.add_argument("--once", action="store_true")
    c.add_argument("--scope", action="append", help="clé de championnat (répétable)")
    c.set_defaults(fn=cmd_collect)

    f = sub.add_parser("ingest-file", help="ingère une réponse enregistrée")
    f.add_argument("path")
    f.add_argument("--scope")
    f.add_argument("--observed-at")
    f.set_defaults(fn=cmd_ingest_file)

    sub.add_parser("bookmakers", help="bookmakers vus et leur rôle").set_defaults(fn=cmd_bookmakers)

    r = sub.add_parser("set-role", help="fixe le rôle d'un bookmaker")
    r.add_argument("bookmaker")
    r.add_argument("role", choices=sorted(ROLES))
    r.set_defaults(fn=cmd_set_role)

    a = sub.add_parser("alias", help="rattache un nom d'équipe à une équipe existante")
    a.add_argument("alias")
    a.add_argument("team_id", type=int)
    a.set_defaults(fn=cmd_alias)

    s = sub.add_parser("scan", help="écarts cote FR / cote juste à l'instant T")
    s.add_argument("--at", help="instant ISO avec fuseau (défaut : maintenant)")
    s.add_argument("--min-edge", type=float, default=0.02)
    s.add_argument("--horizon-hours", type=float, default=72)
    s.add_argument("--max-staleness-min", type=float, default=20)
    s.set_defaults(fn=cmd_scan)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s : %(message)s",
    )
    return int(args.fn(Settings.from_env(), args))


if __name__ == "__main__":
    raise SystemExit(main())
