# PBS KIDS catalog database

## Discovery

`inspect-url` retrieves a catalog page, or reads saved HTML, and reports its structure without onboarding the show or writing observations.

```bash
python pbs_kids_catalog_db.py inspect-url \
  https://pbskids.org/videos/odd-squad
```

Use a saved fixture:

```bash
python pbs_kids_catalog_db.py inspect-url \
  https://pbskids.org/videos/odd-squad \
  --html odd-squad.html
```

Request structured output:

```bash
python pbs_kids_catalog_db.py inspect-url \
  https://pbskids.org/videos/odd-squad \
  --html odd-squad.html \
  --output-format json
```

The report includes:

- page title and slug;
- Next.js build ID;
- total catalog occurrences;
- unique assets after GUID deduplication;
- counts by video type;
- collections and their entry counts;
- missing identifier and duration counts;
- sample watch-page URLs;
- parser warnings;
- a conservative recommended minimum-asset threshold.

## Onboarding

`onboard-show` performs discovery first and saves the show configuration only if parsing succeeds.

Newly onboarded shows are **disabled by default**. This separates discovery from production scheduling.

```bash
python pbs_kids_catalog_db.py onboard-show \
  odd-squad \
  https://pbskids.org/videos/odd-squad \
  --html odd-squad.html \
  --require-collection odd-squad-full-episodes
```

Enable immediately only after reviewing the discovery report:

```bash
python pbs_kids_catalog_db.py onboard-show \
  odd-squad \
  https://pbskids.org/videos/odd-squad \
  --html odd-squad.html \
  --minimum-assets 80 \
  --frequency daily \
  --require-collection odd-squad-full-episodes \
  --enable
```

If no minimum is supplied, onboarding uses 80 percent of the observed unique-asset count, rounded down with a minimum of one. This is an initial safeguard, not a permanent business rule.

Existing show configuration is protected. Use `--replace` to revise it deliberately. When `--replace` is used, the required-collection list is replaced by the values supplied on that command.

```bash
python pbs_kids_catalog_db.py onboard-show \
  odd-squad \
  https://pbskids.org/videos/odd-squad \
  --html odd-squad.html \
  --replace \
  --enable
```

## Configuration

The default file is `pbs_kids_catalog.ini`. Override it with:

```bash
python pbs_kids_catalog_db.py \
  --config /etc/pbs-kids/catalog.ini \
  inspect-url https://pbskids.org/videos/odd-squad
```

Relative database and snapshot paths are resolved relative to the INI file.

## Tests

```bash
python -m unittest -v test_pbs_kids_catalog_onboarding.py
```

## Updating all enabled shows

`update-all` processes every enabled show in canonical-key order. Each show uses an independent database transaction. A retrieval or parser failure for one show is recorded as an incomplete run and does not prevent later shows from running.

```bash
python pbs_kids_catalog_db.py update-all
```

Limit execution to selected enabled shows by repeating `--only`:

```bash
python pbs_kids_catalog_db.py update-all \
  --only odd-squad \
  --only wild-kratts
```

Exclude one or more enabled shows:

```bash
python pbs_kids_catalog_db.py update-all \
  --exclude sesame-street
```

`--only` is evaluated first, followed by `--exclude`. Disabled shows are never selected. Requesting an unknown or disabled show with `--only` is an error.

Structured execution summaries are available for scheduled processing:

```bash
python pbs_kids_catalog_db.py update-all \
  --output-format json
```

The command exits with:

- `0` when every selected show was retrieved, parsed, and marked complete;
- `1` when one or more selected shows failed or were marked incomplete;
- `2` for configuration errors, invalid selections, or other command-level failures.

The existing `update` command applies the same ingestion and completeness logic to one enabled show:

```bash
python pbs_kids_catalog_db.py update odd-squad
```

Run the update-all tests with:

```bash
python -m unittest -v test_pbs_kids_catalog_update_all.py
```
