#!/usr/bin/env python3
"""Download available PBS KIDS videos represented in the local catalog."""

from __future__ import annotations

import argparse
import configparser
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pbs_kids_catalog_db as catalog_database
import pbs_kids_watch_parser as watch_parser

try:
    import httpx
except ImportError:
    httpx = None


APP_VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
LOG = logging.getLogger("pbs_kids_video_download")


@dataclass(frozen=True)
class DownloadSettings:
    media_root: Path
    temporary_root: Path
    video_types: tuple[str, ...]
    preferred_profiles: tuple[str, ...]
    yt_dlp_command: str
    ffprobe_command: str
    minimum_free_bytes: int
    duration_tolerance_seconds: float
    overwrite: bool


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def resolve_path(raw: str, base: Path) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else base / path


def load_download_settings(config_path: Path) -> DownloadSettings:
    config = configparser.ConfigParser(interpolation=None)
    if config_path.exists():
        with config_path.open(encoding="utf-8") as handle:
            config.read_file(handle)

    base = config_path.parent
    media_root = resolve_path(
        config.get("downloads", "media_root", fallback="media"),
        base,
    )
    temporary_root = resolve_path(
        config.get("downloads", "temporary_root", fallback="data/download-staging"),
        base,
    )
    video_types = tuple(
        item.strip()
        for item in config.get(
            "downloads",
            "video_types",
            fallback="fullEpisode,movie",
        ).split(",")
        if item.strip()
    )
    preferred_profiles = tuple(
        item.strip()
        for item in config.get(
            "downloads",
            "preferred_profiles",
            fallback=(
                "hls-16x9-1080p,hls-16x9-720p,"
                "mp4-16x9-720p,mp4-16x9-baseline"
            ),
        ).split(",")
        if item.strip()
    )

    return DownloadSettings(
        media_root=media_root,
        temporary_root=temporary_root,
        video_types=video_types,
        preferred_profiles=preferred_profiles,
        yt_dlp_command=config.get("downloads", "yt_dlp_command", fallback="yt-dlp"),
        ffprobe_command=config.get("downloads", "ffprobe_command", fallback="ffprobe"),
        minimum_free_bytes=config.getint(
            "downloads",
            "minimum_free_bytes",
            fallback=1_073_741_824,
        ),
        duration_tolerance_seconds=config.getfloat(
            "downloads",
            "duration_tolerance_seconds",
            fallback=5.0,
        ),
        overwrite=config.getboolean("downloads", "overwrite", fallback=False),
    )


def ensure_download_schema(connection: sqlite3.Connection) -> None:
    schema = (HERE / "pbs_kids_video_schema.sql").read_text(encoding="utf-8")
    connection.executescript(schema)


def sanitize_component(value: str, maximum_length: int = 120) -> str:
    """Return a portable path component without source-controlled traversal."""
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " - ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = cleaned.replace("..", ".")
    return (cleaned or "Untitled")[:maximum_length].rstrip(" .")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()



def promote_download(temporary: Path, destination: Path) -> None:
    """Install a completed download, including across filesystems.

    An atomic rename is used when staging and destination are on the same
    filesystem. If they are on different filesystems, the file is copied to a
    temporary sibling of the destination, flushed, and atomically renamed
    there before the staging file is removed.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        os.replace(temporary, destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    destination_temporary = destination.with_name(
        f".{destination.name}.installing-{os.getpid()}"
    )
    destination_temporary.unlink(missing_ok=True)

    try:
        with temporary.open("rb") as source:
            with destination_temporary.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())

        shutil.copystat(
            temporary,
            destination_temporary,
            follow_symlinks=True,
        )
        os.replace(destination_temporary, destination)
        temporary.unlink()
    except Exception:
        destination_temporary.unlink(missing_ok=True)
        raise

def verify_file(
    path: Path,
    expected_duration: int | None,
    settings: DownloadSettings,
) -> dict[str, Any]:
    """Inspect the downloaded file with ffprobe when available."""
    result: dict[str, Any] = {
        "container_format": path.suffix.lstrip(".") or None,
        "duration_seconds": None,
        "video_width": None,
        "video_height": None,
        "verification_status": "unverified",
    }
    if shutil.which(settings.ffprobe_command) is None:
        return result

    completed = subprocess.run(
        [
            settings.ffprobe_command,
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return result

    data = json.loads(completed.stdout)
    format_data = data.get("format") or {}
    duration = format_data.get("duration")
    result["duration_seconds"] = float(duration) if duration is not None else None
    result["container_format"] = format_data.get("format_name") or result["container_format"]
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video":
            result["video_width"] = stream.get("width")
            result["video_height"] = stream.get("height")
            break

    result["verification_status"] = "verified"
    if (
        expected_duration is not None
        and result["duration_seconds"] is not None
        and abs(result["duration_seconds"] - expected_duration)
        > settings.duration_tolerance_seconds
    ):
        result["verification_status"] = "duration_warning"
    return result


def fetch_watch_page(
    url: str,
    catalog_settings: catalog_database.Settings,
) -> tuple[str, int]:
    if httpx is None:
        raise RuntimeError("Watch-page retrieval requires the httpx package.")

    with httpx.Client(
        headers={
            "User-Agent": catalog_settings.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        },
        follow_redirects=True,
        timeout=catalog_settings.timeout_seconds,
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text, response.status_code


def candidate_query(video_types: tuple[str, ...]) -> tuple[str, list[Any]]:
    """Return assets in each show's latest complete run without an artifact."""
    if not video_types:
        raise ValueError("At least one video type must be selected.")

    placeholders = ", ".join("?" for _ in video_types)
    query = f"""
        WITH ranked_runs AS
        (
            SELECT
                run_id,
                show_id,
                ROW_NUMBER() OVER
                (
                    PARTITION BY show_id
                    ORDER BY observed_at DESC, run_id DESC
                ) AS run_number
            FROM catalog_run
            WHERE retrieval_succeeded = 1
              AND parse_succeeded = 1
              AND catalog_complete = 1
        ),
        current_runs AS
        (
            SELECT run_id, show_id
            FROM ranked_runs
            WHERE run_number = 1
        ),
        preferred_membership AS
        (
            SELECT
                mo.run_id,
                mo.asset_id,
                mo.watch_url,
                ROW_NUMBER() OVER
                (
                    PARTITION BY mo.run_id, mo.asset_id
                    ORDER BY co.slug, mo.ordinal_position
                ) AS preferred_number
            FROM membership_observation AS mo
            JOIN collection_observation AS co
              ON co.run_id = mo.run_id
             AND co.collection_id = mo.collection_id
        )
        SELECT
            current.show_id,
            configured_show.canonical_key AS show_key,
            observation.run_id,
            observation.asset_id,
            asset.media_manager_guid,
            observation.title,
            observation.slug,
            observation.video_type,
            observation.duration_seconds,
            membership.watch_url
        FROM current_runs AS current
        JOIN show AS configured_show
          ON configured_show.show_id = current.show_id
        JOIN asset_observation AS observation
          ON observation.run_id = current.run_id
        JOIN asset
          ON asset.asset_id = observation.asset_id
        JOIN preferred_membership AS membership
          ON membership.run_id = observation.run_id
         AND membership.asset_id = observation.asset_id
         AND membership.preferred_number = 1
        LEFT JOIN download_artifact AS artifact
          ON artifact.asset_id = observation.asset_id
        WHERE artifact.asset_id IS NULL
          AND observation.video_type IN ({placeholders})
        ORDER BY configured_show.canonical_key, observation.title
    """
    return query, list(video_types)


def target_path(
    row: sqlite3.Row,
    settings: DownloadSettings,
) -> Path:
    show_name = sanitize_component(row["show_key"])
    category = sanitize_component(row["video_type"])
    title = sanitize_component(row["title"])
    return settings.media_root / show_name / category / f"{title} [{row['asset_id']}].mp4"


def record_watch_observation(
    connection: sqlite3.Connection,
    asset_id: int,
    watch_url: str,
    http_status: int | None,
    html: str | None,
    parsed: watch_parser.WatchPage | None,
    error: str | None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO watch_page_observation
            (asset_id, observed_at, watch_url, http_status,
             response_sha256, source_expires_at, drm_enabled,
             title, description_long, duration_seconds,
             retrieval_succeeded, parse_succeeded, error_message)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            utc_now(),
            watch_url,
            http_status,
            hashlib.sha256(html.encode("utf-8")).hexdigest() if html else None,
            parsed.source_expires_at if parsed else None,
            int(parsed.drm_enabled) if parsed else None,
            parsed.title if parsed else None,
            parsed.description_long if parsed else None,
            parsed.duration_seconds if parsed else None,
            int(html is not None),
            int(parsed is not None),
            error,
        ),
    )
    observation_id = int(cursor.lastrowid)
    if parsed:
        connection.executemany(
            """
            INSERT INTO media_profile_observation
                (watch_observation_id, profile, redirect_url,
                 fairplay_license_url, widevine_license_url)
            VALUES
                (?, ?, ?, ?, ?)
            """,
            [
                (
                    observation_id,
                    item.profile,
                    item.redirect_url,
                    item.fairplay_license_url,
                    item.widevine_license_url,
                )
                for item in parsed.media_profiles
            ],
        )
    return observation_id


def download_candidate(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    catalog_settings: catalog_database.Settings,
    download_settings: DownloadSettings,
    dry_run: bool,
) -> dict[str, Any]:
    destination = target_path(row, download_settings)
    if destination.exists() and not download_settings.overwrite:
        return {
            "asset_id": row["asset_id"],
            "title": row["title"],
            "status": "existing-untracked",
            "path": str(destination),
            "error": "Target file already exists but is not recorded in the database.",
        }

    if dry_run:
        return {
            "asset_id": row["asset_id"],
            "title": row["title"],
            "status": "would-download",
            "path": str(destination),
            "watch_url": row["watch_url"],
        }

    html: str | None = None
    parsed: watch_parser.WatchPage | None = None
    try:
        html, status = fetch_watch_page(row["watch_url"], catalog_settings)
        parsed = watch_parser.parse_watch_page(html)
        if parsed.media_manager_guid != row["media_manager_guid"]:
            raise RuntimeError(
                "Watch-page Media Manager GUID does not match the catalog asset."
            )
        with connection:
            watch_observation_id = record_watch_observation(
                connection,
                row["asset_id"],
                row["watch_url"],
                status,
                html,
                parsed,
                None,
            )
    except Exception as exc:
        with connection:
            record_watch_observation(
                connection,
                row["asset_id"],
                row["watch_url"],
                None,
                html,
                parsed,
                str(exc),
            )
        return {
            "asset_id": row["asset_id"],
            "title": row["title"],
            "status": "failed",
            "path": str(destination),
            "error": str(exc),
        }

    chosen = watch_parser.choose_media_profile(
        parsed,
        list(download_settings.preferred_profiles),
    )
    if chosen is None:
        reason = "DRM-protected or no non-DRM media profile is available."
        with connection:
            connection.execute(
                """
                INSERT INTO download_attempt
                    (asset_id, watch_observation_id, started_at,
                     selected_profile, source_url, target_path,
                     status, error_message)
                VALUES
                    (?, ?, ?, NULL, NULL, ?, 'skipped', ?)
                """,
                (
                    row["asset_id"],
                    watch_observation_id,
                    utc_now(),
                    str(destination),
                    reason,
                ),
            )
        return {
            "asset_id": row["asset_id"],
            "title": row["title"],
            "status": "skipped",
            "path": str(destination),
            "error": reason,
        }

    if shutil.which(download_settings.yt_dlp_command) is None:
        return {
            "asset_id": row["asset_id"],
            "title": row["title"],
            "status": "failed",
            "path": str(destination),
            "error": f"Downloader not found: {download_settings.yt_dlp_command}",
        }

    destination.parent.mkdir(parents=True, exist_ok=True)
    download_settings.temporary_root.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(destination.parent).free
    if free_bytes < download_settings.minimum_free_bytes:
        return {
            "asset_id": row["asset_id"],
            "title": row["title"],
            "status": "failed",
            "path": str(destination),
            "error": "Insufficient free disk space.",
        }

    temporary = download_settings.temporary_root / f"asset-{row['asset_id']}.mp4"
    temporary.unlink(missing_ok=True)
    started_at = utc_now()
    with connection:
        attempt_id = int(
            connection.execute(
                """
                INSERT INTO download_attempt
                    (asset_id, watch_observation_id, started_at,
                     selected_profile, source_url, target_path,
                     temporary_path, status)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, 'started')
                """,
                (
                    row["asset_id"],
                    watch_observation_id,
                    started_at,
                    chosen.profile,
                    chosen.redirect_url,
                    str(destination),
                    str(temporary),
                ),
            ).lastrowid
        )

    command = [
        download_settings.yt_dlp_command,
        "--no-playlist",
        "--no-overwrites",
        "--no-part",
        "--merge-output-format",
        "mp4",
        "--output",
        str(temporary),
        chosen.redirect_url,
    ]
    completed = subprocess.run(command, check=False)

    if completed.returncode != 0 or not temporary.exists():
        error = f"yt-dlp exited with status {completed.returncode}."
        with connection:
            connection.execute(
                """
                UPDATE download_attempt
                SET completed_at = ?, status = 'failed',
                    downloader_exit_code = ?, error_message = ?
                WHERE download_attempt_id = ?
                """,
                (utc_now(), completed.returncode, error, attempt_id),
            )
        return {
            "asset_id": row["asset_id"],
            "title": row["title"],
            "status": "failed",
            "path": str(destination),
            "error": error,
        }

    promote_download(temporary, destination)
    verification = verify_file(
        destination,
        parsed.duration_seconds or row["duration_seconds"],
        download_settings,
    )
    relative_path = destination.relative_to(download_settings.media_root).as_posix()
    with connection:
        connection.execute(
            """
            UPDATE download_attempt
            SET completed_at = ?, status = 'complete', downloader_exit_code = 0
            WHERE download_attempt_id = ?
            """,
            (utc_now(), attempt_id),
        )
        connection.execute(
            """
            INSERT INTO download_artifact
                (asset_id, download_attempt_id, relative_path,
                 file_size_bytes, sha256, container_format,
                 duration_seconds, video_width, video_height,
                 verified_at, verification_status)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["asset_id"],
                attempt_id,
                relative_path,
                destination.stat().st_size,
                sha256_file(destination),
                verification["container_format"],
                verification["duration_seconds"],
                verification["video_width"],
                verification["video_height"],
                utc_now(),
                verification["verification_status"],
            ),
        )

    return {
        "asset_id": row["asset_id"],
        "title": row["title"],
        "status": "complete",
        "path": str(destination),
        "profile": chosen.profile,
        "verification_status": verification["verification_status"],
        "error": None,
    }


def build_cli(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download available PBS KIDS videos missing from the local library."
    )
    parser.add_argument("--db", type=Path)
    parser.add_argument("--media-root", type=Path)
    parser.add_argument("--temporary-root", type=Path)
    parser.add_argument("--show", action="append", default=[])
    parser.add_argument("--video-type", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        catalog_settings, remaining = catalog_database.load_settings(argv)
        args = build_cli(remaining)
        logging.basicConfig(
            level=getattr(logging, catalog_settings.log_level),
            format="%(levelname)s: %(message)s",
        )
        database = args.db.expanduser() if args.db else catalog_settings.database
        download_settings = load_download_settings(catalog_settings.config_path)
        if args.media_root:
            download_settings = DownloadSettings(
                **{
                    **download_settings.__dict__,
                    "media_root": args.media_root.expanduser(),
                }
            )
        if args.temporary_root:
            download_settings = DownloadSettings(
                **{
                    **download_settings.__dict__,
                    "temporary_root": args.temporary_root.expanduser(),
                }
            )

        catalog_database.initialize_database(database, catalog_settings)
        with catalog_database.connect(database, catalog_settings) as connection:
            ensure_download_schema(connection)
            video_types = tuple(args.video_type) or download_settings.video_types
            query, parameters = candidate_query(video_types)
            rows = connection.execute(query, parameters).fetchall()
            if args.show:
                selected = set(args.show)
                rows = [row for row in rows if row["show_key"] in selected]
            if args.limit is not None:
                rows = rows[: args.limit]

            results = [
                download_candidate(
                    connection,
                    row,
                    catalog_settings,
                    download_settings,
                    args.dry_run,
                )
                for row in rows
            ]

        summary = {
            "candidates": len(rows),
            "complete": sum(item["status"] == "complete" for item in results),
            "skipped": sum(item["status"] == "skipped" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
            "would_download": sum(
                item["status"] == "would-download" for item in results
            ),
            "results": results,
        }
        if args.output_format == "json":
            print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print(
                f"Candidates={summary['candidates']} "
                f"complete={summary['complete']} "
                f"skipped={summary['skipped']} "
                f"failed={summary['failed']} "
                f"would-download={summary['would_download']}"
            )
            for item in results:
                print(f"  {item['status']}: {item['title']} -> {item['path']}")
                if item.get("error"):
                    print(f"    {item['error']}")

        return 1 if summary["failed"] else 0
    except Exception as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
