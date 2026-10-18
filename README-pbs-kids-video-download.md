# PBS KIDS video downloader

This application reads the latest complete catalog run for each configured show and downloads selected videos that do not already have a recorded local artifact.

It downloads only media profiles exposed by the PBS KIDS watch page. DRM-enabled assets and profiles carrying FairPlay or Widevine license URLs are skipped. It does not attempt to bypass access controls.

Use it only for content you are authorized to save and retain.

## Files

- `pbs_kids_video_download.py`: download CLI and inventory workflow
- `pbs_kids_watch_parser.py`: watch-page and media-profile parser
- `pbs_kids_video_schema.sql`: watch, profile, attempt, and artifact tables
- `test_pbs_kids_video_download.py`: parser and candidate-selection tests

The downloader uses the same:

- `pbs_kids_catalog.ini`
- SQLite database
- show and catalog observations
- logging and HTTP settings

as the catalog application.

## Dependencies

Install:

```bash
python -m pip install 'httpx>=0.27,<1' yt-dlp
```

Install `ffmpeg` and `ffprobe` from the operating-system package manager. `yt-dlp` uses `ffmpeg` when HLS streams must be merged or remuxed. File verification uses `ffprobe` when available.

## Configuration

Add or retain this section in `pbs_kids_catalog.ini`:

```ini
[downloads]
media_root = media
temporary_root = data/download-staging
video_types = fullEpisode, movie
preferred_profiles = hls-16x9-1080p, hls-16x9-720p, mp4-16x9-720p, mp4-16x9-baseline
yt_dlp_command = yt-dlp
ffprobe_command = ffprobe
minimum_free_bytes = 1073741824
duration_tolerance_seconds = 5
overwrite = false
```

Relative paths are resolved relative to the INI file.

## Preview the queue

Always start with a dry run:

```bash
python pbs_kids_video_download.py \
  --dry-run
```

Restrict by show:

```bash
python pbs_kids_video_download.py \
  --show odd-squad \
  --dry-run
```

Restrict content types:

```bash
python pbs_kids_video_download.py \
  --video-type fullEpisode \
  --limit 5 \
  --dry-run
```

## Download missing videos

```bash
python pbs_kids_video_download.py
```

The default query includes assets from each show's latest complete catalog run whose video type is configured and whose asset does not already have a `download_artifact` row.

## Output layout

Default layout:

```text
media/
  odd-squad/
    fullEpisode/
      Episode Title [asset_id].mp4
```

The internal asset ID prevents different assets with the same title from colliding.

## Download lifecycle

For each candidate the program:

1. retrieves its watch page;
2. validates the Media Manager GUID against the catalog;
3. records watch-page and media-profile observations;
4. skips DRM-enabled assets;
5. selects the first configured non-DRM profile;
6. checks free disk space;
7. downloads to the staging directory with `yt-dlp`;
8. atomically moves the completed file into the media library;
9. calculates SHA-256;
10. uses `ffprobe` to record duration, dimensions, and container when available;
11. records the completed artifact in SQLite.

A recorded artifact removes the asset from later download queues. A failed attempt does not, so the asset is eligible for retry on the next run.

## Existing untracked files

If the target filename already exists but no artifact row exists, the program does not overwrite or silently trust it. It reports `existing-untracked` for manual reconciliation.

## Tests

```bash
python -m unittest -v test_pbs_kids_video_download.py
```
