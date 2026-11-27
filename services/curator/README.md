# Curator

Status: implemented and tested against a fake Radarr/Sonarr that speaks the v3 API
(`tests/test_curator_arr.py`) and against Postgres (`tests/integration/test_curator.py`). Not yet
run against a live Radarr/Sonarr: check the first cycle with `./muse curator` and
`./muse logs curator`, and pause with `./muse curator pause` if anything looks off.

Autonomous storage-budget manager. What it does for people is in
[`docs/installation.md`](../../docs/installation.md#the-curator-muse-looks-after-your-storage);
this is how:

- `main.py` — two loops. Frequent (30 min): re-checks Soon Gone against the protection rules,
  saves what now qualifies, removes what expired. Slow (daily, or when the admin asks): sync
  real sizes and adopt tagged titles, next seasons, add, and (only over budget) Soon Gone.
- `budget.py` — the budget (env default, overridden from the admin), real-size accounting,
  per-person daily cap, the grace window and `protection()` / `protection_reason()`: the rules
  that keep a title, one place, unit-tested.
- `scoring.py` — averaged-across-profiles similarity for ADD decisions, max-across-profiles
  for REMOVE decisions (an item is only removal-eligible if *every* active profile scores it low).
- `discovery.py` — TMDB + Trakt candidates; size estimates until the sync has real ones.
- `arr_client.py` — the only code path that can add or remove a file: tags, quality profile,
  root folder, season-1 adds, next-season monitoring, sizes, free space.

Everything it does goes to `curator_activity` (shown by `GET /v1/admin/curator`) and to the
admin's feed (`muse_schema.notify.admin`, `GET /v1/admin/notifications?domain=video`).

## Settings (`.env`)

| | default | |
|---|---|---|
| `MUSE_CURATOR_BUDGET_GB` | 300 | the admin can change it from Vesper or `./muse curator budget` |
| `MUSE_CURATOR_MIN_FREE_GB` | 50 | never fill the disk past this |
| `MUSE_CURATOR_GRACE_HOURS` | 48 | Soon Gone |
| `MUSE_CURATOR_RECENT_DAYS` | 30 | watched within = never removed |
| `MUSE_CURATOR_DAILY_CAP` | 10 | adds per person per day, scaled down for quiet profiles |
| `MUSE_CURATOR_TAG` | muse | tag in Radarr/Sonarr |
| `MUSE_CURATOR_SERIES_START` | first_season | or `all` |
| `MUSE_CURATOR_ADD_SCORE` / `_REMOVE_SCORE` | 0.55 / 0.10 | thresholds |

## Known gaps

- Sizes are estimated until the first sync after a download (6 GB a film, 12 GB a season).
- Series adopted from Sonarr v3 without a TMDB id can't be adopted (Sonarr v4 has the id).
