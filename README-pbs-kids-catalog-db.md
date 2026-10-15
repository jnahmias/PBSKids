# PBS KIDS multi-show catalog database

This phase implements the SQLite catalog defined in `pbs-kids-catalog-database-design.md`.
It records append-only observations for configured PBS KIDS shows. It does not yet retrieve watch-page media profiles, captions, or video files.

## Files

- `pbs_kids_catalog_db.py`: database CLI and ingestion service
- `pbs_kids_catalog_parser.py`: generic Next.js catalog parser
- `pbs_kids_catalog_schema.sql`: SQLite schema, indexes, and current-state views
- `test_pbs_kids_catalog_db.py`: database and ingestion tests
- `pbs-kids-catalog.sqlite3`: sample database populated from the supplied Odd Squad fixture

## Requirements

- Python 3.11 or later recommended
- `httpx` for live retrieval
- SQLite supplied by Python's standard library

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'httpx>=0.27,<1'
```

Keep the Python and SQL files in the same directory.

## Initialize

```bash
python pbs_kids_catalog_db.py \
  --db pbs-kids-catalog.sqlite3 \
  init
```

## Configure a show

The URL must be verified during onboarding.

```bash
python pbs_kids_catalog_db.py \
  --db pbs-kids-catalog.sqlite3 \
  add-show odd-squad \
  'https://pbskids.org/videos/odd-squad' \
  --minimum-assets 80 \
  --frequency daily
```

Optionally require a structurally expected collection before a run can be marked complete:

```bash
python pbs_kids_catalog_db.py \
  --db pbs-kids-catalog.sqlite3 \
  require-collection odd-squad odd-squad-full-episodes
```

Other shows use the same commands, with independently verified keys, URLs, minimum counts, and required collections.

## Import a saved HTML fixture

```bash
python pbs_kids_catalog_db.py \
  --db pbs-kids-catalog.sqlite3 \
  update odd-squad \
  --html odd-squad.html \
  --observed-at '2026-10-15T03:22:57.000Z' \
  --snapshot-root snapshots
```

## Retrieve the current page

Omit `--html`:

```bash
python pbs_kids_catalog_db.py \
  --db pbs-kids-catalog.sqlite3 \
  update odd-squad \
  --snapshot-root snapshots
```

## Inspect configuration and runs

```bash
python pbs_kids_catalog_db.py --db pbs-kids-catalog.sqlite3 list-shows
python pbs_kids_catalog_db.py --db pbs-kids-catalog.sqlite3 runs --key odd-squad
```

## Inspect current observations

```bash
python pbs_kids_catalog_db.py \
  --db pbs-kids-catalog.sqlite3 \
  current odd-squad
```

The `current_catalog_by_show` view uses the latest successful, complete run for each show. Incomplete runs remain in history but do not replace current state.

## Run tests

Keep `odd-squad.html` beside the test file:

```bash
python -m unittest -v test_pbs_kids_catalog_db.py
```

The tests verify:

- initial identity and observation counts
- foreign-key and database integrity
- append-only behavior across two runs
- stable identity reuse
- show completeness thresholds
- current-state exclusion of incomplete runs

## Important operating rules

- A missing row means absence only for a successful, complete run of the same show.
- Each show is updated in its own transaction and has independent completeness rules.
- Do not manually update observation rows.
- Do not reuse another show's thresholds or required collections without onboarding evidence.
- Back up active databases with the SQLite backup API or `VACUUM INTO`.
- Preserve failed source snapshots until parser problems are resolved.

## Phase 2 extension points

The next phase can add separate run and observation tables for:

- watch-page enrichment
- source expiry and DRM status
- media profiles and PBS URS redirects
- caption tracks
- download attempts and verified artifacts

Catalog-run absence will not be interpreted as caption or media-profile absence.
