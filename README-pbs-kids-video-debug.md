# PBS KIDS video-format debugger

`pbs_kids_video_debug.py` reads the local SQLite catalog, finds one asset in a show's latest complete catalog run, retrieves its watch page, and displays every media profile exposed by that page.

The utility identifies DRM indicators but does not generate retrieval commands for DRM-protected profiles.

## Usage

```bash
python pbs_kids_video_debug.py SHOW_SLUG ASSET_ID
```

Example:

```bash
python pbs_kids_video_debug.py odd-squad 42
```

Use a non-default configuration file:

```bash
python pbs_kids_video_debug.py \
  --config /etc/pbs-kids/pbs_kids_catalog.ini \
  odd-squad 42
```

Override the database:

```bash
python pbs_kids_video_debug.py \
  odd-squad 42 \
  --db /var/lib/pbs-kids/catalog.sqlite3
```

## Generated commands

For each non-DRM profile, the utility prints:

- the PBS media profile name;
- its redirect URL;
- a shell-safe `yt-dlp` command;
- the proposed output filename.

The generated command uses the configured `yt_dlp_command`. Override it with:

```bash
python pbs_kids_video_debug.py \
  odd-squad 42 \
  --downloader-command /usr/local/bin/yt-dlp
```

Change the output directory represented in generated commands:

```bash
python pbs_kids_video_debug.py \
  odd-squad 42 \
  --output-directory /tmp/video-debug
```

## JSON output

```bash
python pbs_kids_video_debug.py \
  odd-squad 42 \
  --output-format json
```

JSON output includes asset identity, watch-page metadata, expiry, DRM status, profiles, URLs, license indicators, and retrieval commands.

## Asset-ID semantics

`ASSET_ID` is the application's internal SQLite `asset.asset_id`, not the PBS KIDS CMS ID or Media Manager GUID.

The asset must be present in the named show's latest successful, complete catalog run. The utility verifies that the Media Manager GUID returned by the watch page matches the catalog asset before presenting retrieval commands.
