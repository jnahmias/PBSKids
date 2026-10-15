# PBS KIDS catalog database

## Configuration

The program looks for `pbs_kids_catalog.ini` in the current working directory. If the default file is absent, built-in defaults are used. An explicitly requested missing file is an error.

Use a different file with the global `--config` option:

```bash
python pbs_kids_catalog_db.py --config /etc/pbs-kids/catalog.ini init
```

Example:

```ini
[application]
database = data/pbs-kids-catalog.sqlite3
snapshot_root = data/snapshots
log_level = INFO

[http]
timeout_seconds = 30
user_agent = PBSKidsCatalog/0.2 (+personal catalog synchronization)

[sqlite]
busy_timeout_ms = 5000
journal_mode = WAL
synchronous = NORMAL
```

Relative `database` and `snapshot_root` values are resolved relative to the INI file's directory, not the process working directory. An empty `snapshot_root` disables source snapshots.

Precedence is:

1. command-line option
2. INI setting
3. built-in default

The following stable operational settings can be overridden on the command line:

```bash
python pbs_kids_catalog_db.py \
  --config pbs_kids_catalog.ini \
  --db /tmp/catalog.sqlite3 \
  --snapshot-root /tmp/snapshots \
  --log-level DEBUG \
  init
```

## Initialize and configure

```bash
python pbs_kids_catalog_db.py init

python pbs_kids_catalog_db.py add-show \
  odd-squad \
  https://pbskids.org/videos/odd-squad \
  --minimum-assets 80 \
  --frequency daily

python pbs_kids_catalog_db.py require-collection \
  odd-squad odd-squad-full-episodes
```

## Update from saved HTML

```bash
python pbs_kids_catalog_db.py update odd-squad \
  --html odd-squad.html \
  --observed-at 2026-10-15T03:22:57.000Z
```

## Live update

```bash
python pbs_kids_catalog_db.py update odd-squad
```

## Inspection

```bash
python pbs_kids_catalog_db.py list-shows
python pbs_kids_catalog_db.py runs --key odd-squad
python pbs_kids_catalog_db.py current odd-squad
```

## Tests

```bash
python -m unittest -v test_pbs_kids_catalog_config.py
```

Phase 2 remains reserved for watch-page details, media profiles, captions, and downloads.
