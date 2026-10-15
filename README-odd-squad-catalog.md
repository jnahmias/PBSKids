# Odd Squad catalog parser, phase 1

This program retrieves the PBS KIDS Odd Squad catalog page, extracts its embedded Next.js `__NEXT_DATA__` JSON, normalizes the video entries, deduplicates repeated appearances, and writes a local JSON catalog. It can optionally write CSV.

It does not download videos yet. Caption discovery and watch-page enrichment are reserved for phase 2. The normalized schema already contains an empty `captions` list, and the source defines a `CaptionTrack` model so the schema can be extended without redesigning the catalog layer.

## Requirements

- Python 3.11 or newer recommended
- `httpx` for live HTTP retrieval
- No browser or Playwright installation is required

On Debian:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-odd-squad.txt
```

## Retrieve the current catalog

By default, the command selects ordinary `fullEpisode` entries and excludes the separate ASL collection:

```bash
python odd_squad_catalog.py \
  --output odd-squad-catalog.json \
  --csv odd-squad-catalog.csv
```

Save the source HTML for diagnostics:

```bash
python odd_squad_catalog.py \
  --save-html odd-squad-latest.html \
  --output odd-squad-catalog.json
```

## Parse the supplied saved page

```bash
python odd_squad_catalog.py \
  --html odd-squad.html \
  --output odd-squad-catalog.json \
  --csv odd-squad-catalog.csv
```

## Include ASL full episodes

```bash
python odd_squad_catalog.py \
  --html odd-squad.html \
  --include-asl \
  --output odd-squad-full-with-asl.json
```

## Include movies

Repeat `--type` for multiple categories:

```bash
python odd_squad_catalog.py \
  --type fullEpisode \
  --type movie \
  --output odd-squad-episodes-and-movies.json
```

## Inspect every catalog type

```bash
python odd_squad_catalog.py \
  --all-types \
  --include-asl \
  --output odd-squad-all.json
```

## Limit by collection

```bash
python odd_squad_catalog.py \
  --all-types \
  --collection odd-squad-movies \
  --output odd-squad-movies.json
```

## Run tests

Keep `test_odd_squad_catalog.py`, `odd_squad_catalog.py`, and the supplied `odd-squad.html` in the same directory, then run:

```bash
python -m unittest -v test_odd_squad_catalog.py
```

The fixture tests verify the observed catalog counts, deduplication, default ASL exclusion, watch-page URL construction, and the phase-2 caption extension field.

## Output identity and deduplication

The primary identity is the PBS Media Manager GUID. The output also preserves:

- PBS KIDS CMS video ID
- legacy media ID
- every collection membership
- preferred watch-page URL
- short description
- duration
- PBS premiere timestamp
- available image profiles

Repeated appearances in masthead and body modules are merged by GUID. Collection membership is retained instead of discarded.

## Phase 2 design

The next phase can add a watch-page enrichment component that:

1. Retrieves each `preferred_watch_url`.
2. Extracts its own `__NEXT_DATA__` payload.
3. Reads `pageProps.videoData.mediaManagerAsset`.
4. Adds long description, expiry, season/episode numbers, DRM status, and media profiles.
5. Discovers caption tracks from page data, player requests, or HLS subtitle groups.
6. Populates `captions` with `CaptionTrack` records.

Keeping that enrichment separate means changes to playback or caption discovery will not destabilize catalog enumeration.
