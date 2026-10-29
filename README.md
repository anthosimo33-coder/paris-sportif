# paris-sportif

Collecteur de cotes foot et base de données **à l'instant T** (point-in-time).
Première brique d'un outil qui compare les cotes des bookmakers agréés en France à une
cote « juste » (référence sharp sans marge), et qui mesure la valeur à la clôture (CLV).

## Le principe : ne jamais réécrire le passé

1. **Tout est ajouté, rien n'est modifié.** Les tables d'observation (`odds_quote`,
   `event_schedule`, `poll_event`, `raw_payload`) refusent `UPDATE`, `DELETE` et `TRUNCATE`
   (triggers).
2. **Une seule horloge : `observed_at`**, l'instant où le collecteur a reçu la réponse.
   Les dates annoncées par la source (`source_updated_at`) sont gardées pour information,
   jamais utilisées pour répondre à « que savait-on à T ? ».
3. **Le journal ne stocke que les changements.** Une cote = une ligne quand elle apparaît
   ou change, une ligne `price = NULL` quand elle disparaît. L'état à T = la dernière ligne
   `<= T`.
4. **La fraîcheur est explicite.** `poll_event` dit quand chaque match a été vu en entier.
   Une cote n'est utilisée à T que si elle a été confirmée récemment (`max_staleness`) :
   collecteur en panne = pas de signal, plutôt qu'un signal sur une cote périmée.
5. **Le même code sert au direct et au backtest.** `value_scan(at=maintenant)` alimente
   l'écran ; `value_scan(at=n'importe quand)` rejoue exactement ce qui était visible.

## Schéma

| Table | Rôle |
|---|---|
| `competition`, `team`, `bookmaker`, `event` | référentiel canonique |
| `*_source`, `team_alias` | correspondance source → référentiel (noms normalisés, alias manuels) |
| `poll_run` | un passage du collecteur : statut, quota, compteurs, empreinte de la réponse |
| `raw_payload` | réponse brute (jsonb), dédupliquée par sha256 : permet de tout rejouer |
| `poll_event` | couverture : quel match a été vu à quel instant, sur quels marchés |
| `event_schedule` | horaire de coup d'envoi versionné (reports, décalages) |
| `odds_quote` | journal des changements de cotes (clé : match, book, marché, ligne, issue, instant) |
| `odds_current` | projection de l'état courant, maintenue dans la même transaction, reconstructible (`rebuild_odds_current()`) |

Fonctions SQL à l'instant T : `odds_as_of(event, T)`, `kickoff_as_of(event, T)`,
`last_confirmed(event, market, T)`, `events_as_of(T, horizon)`.

Marchés canoniques : `1x2`, `total_goals`, `asian_handicap` (ligne exprimée côté
domicile), `btts`, `draw_no_bet`.

## Démarrer

```bash
docker compose up -d
```

```bash
uv sync
```

```bash
cp .env.example .env   # puis renseigner ODDS_API_KEY, et exporter les variables
```

```bash
uv run parisportif migrate
```

```bash
uv run parisportif collect --once
```

```bash
uv run parisportif bookmakers
```

Les **clés des bookmakers français ne sont pas encore vérifiées** : la première collecte dit
lesquelles le fournisseur renvoie vraiment. Ensuite, attribuer les rôles :

```bash
uv run parisportif set-role <clé> fr_licensed
```

Puis la collecte continue (cadence 3 h → 2 min à l'approche du coup d'envoi, ralentie si le
quota passe sous `ODDS_API_QUOTA_RESERVE`) :

```bash
uv run parisportif collect
```

## Démo hors ligne (données fictives)

Une journée inventée (dimanche 1er novembre 2026, Ligue 1 + Premier League), quatre passages :

```bash
uv run python examples/demo/make_demo.py
```

```bash
export BOOKMAKER_ROLES_FILE=examples/demo/bookmakers.toml
for f in examples/demo/[1-4]_*.json; do uv run parisportif ingest-file "$f"; done
```

```bash
uv run parisportif scan --at 2026-11-01T15:05:00Z
```

```
Écarts visibles à 2026-11-01 15:05 UTC (seuil 2%)

Paris Saint Germain – RC Lens   dim 01/11 19:45 UTC
  Buts Plus 2.5        winamax_fr    2.10   juste  1.94    +8.4%   mise 1.9%   inchangée depuis 5 min, confirmée il y a 5 min
Marseille – Lyon   dim 01/11 16:15 UTC
  1N2 2                unibet_fr     3.95   juste  3.76    +5.0%   mise 0.4%   inchangée depuis 65 min, confirmée il y a 5 min
Arsenal – Chelsea   dim 01/11 16:30 UTC
  Handicap 2 +0.5      betclic_fr    2.18   juste  2.10    +3.8%   mise 0.8%   inchangée depuis 65 min, confirmée il y a 5 min
```

`--at 2026-11-01T14:05:00Z` montre l'écart « trop beau » (Lens à 9,50, signalé ⚠) ;
`--at 2026-11-01T15:55:00Z` montre que l'écart Winamax a disparu après correction.

## Tests

Les tests touchent un vrai Postgres (une base neuve par test, clonée d'un gabarit migré) :

```bash
TEST_DATABASE_URL=postgresql://parisportif:parisportif@localhost:5432/postgres uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## Plusieurs sources

- Une clé de bookmaker appartient à **une seule source**. Si une 2e source livre aussi
  `winamax_fr`, elle écrit sous `autre_source:winamax_fr` : deux flux ne s'écrasent jamais.
- La fraîcheur se contrôle **par source** : une source arrêtée ne devient pas « confirmée »
  parce qu'une autre continue de voir le match. Un écart n'est retenu que si la source de la
  cote FR et celle de chaque référence ont confirmé récemment. Le retard toléré suit la
  cadence attendue (1,5 x l'intervalle de passage à cette distance du coup d'envoi).
- L'horaire est versionné par source ; le coup d'envoi retenu est le **plus tôt** annoncé
  (prudent : pas de cote de live prise pour de l'avant-match).
- Les noms d'équipes sont normalisés **prudemment** (casse, accents, ponctuation ; les sigles
  restent : « Barcelona » et « Barcelona SC » sont deux clubs). Un nom inconnu crée une équipe,
  marquée `needs_review` si une autre source a déjà rempli le référentiel ; on la rattache avec
  `parisportif alias "<nom>" <team_id>` pour les passages suivants.

## Archive brute : purge

`raw_payload` est immuable mais purgeable (aucune clé étrangère n'y pointe, `DELETE`
autorisé) : environ 45 ko stockés par passage pour 10 matchs x 15 books, du même ordre que le
journal des cotes (1 à 2 fois sa taille par passage, mesuré sur données synthétiques). Rétention :

```sql
DELETE FROM raw_payload WHERE first_seen < now() - interval '90 days';
```

## Limites connues (à traiter, pas cachées)

- **Couverture FR de The Odds API non vérifiée** (régions, clés `*_fr`). L'interface
  `OddsProvider` permet de brancher un autre fournisseur (OddsPapi, odds-api.io, flux maison).
- **Pinnacle** : API publique fermée depuis juillet 2025 ; via un agrégateur, ses cotes peuvent
  être retardées. Une référence en retard fabrique de faux écarts : à mesurer.
- **Flux figé chez l'agrégateur** : un passage réussi confirme une cote même si la source n'a
  pas rafraîchi ce bookmaker (`last_update` ancien). `source_updated_at` est stocké mais pas
  encore exploité pour marquer ces cas.
- **Doublons d'événements** : un alias posé après coup ne déplace pas l'historique déjà écrit
  sous le doublon (journal en ajout seul) ; pas encore de commande de fusion.
- **Marchés** : seuls 1N2, buts et handicap (appel groupé). Les deux équipes marquent /
  remboursé si nul demandent l'appel par match, non implémenté.
- **Handicaps entiers ou quarts** : l'avantage affiché est l'EV hors remboursement ; le signe
  est juste, l'ampleur légèrement surestimée.
- **Avant-match uniquement** : les matchs commencés sont ignorés (la clôture = dernier état
  avant le coup d'envoi).
