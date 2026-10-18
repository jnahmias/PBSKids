#!/usr/bin/env python3
"""Inspect the media formats currently exposed for one catalog asset.

This is a diagnostic utility. It reads the configured SQLite catalog, resolves
an asset from a show slug and internal asset ID, retrieves its PBS KIDS watch
page, and prints the media profiles reported by that page. DRM-protected
profiles are identified but no retrieval command is produced for them.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

import pbs_kids_catalog_db as catalog_database
import pbs_kids_video_download as video_download
import pbs_kids_watch_parser as watch_parser


DEFAULT_OUTPUT_FORMAT = "text"


def find_catalog_asset(
    connection: sqlite3.Connection,
    show_slug: str,
    asset_id: int,
) -> sqlite3.Row:
    """Return the asset from the show's latest complete catalog run."""
    row = connection.execute(
        """
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
        preferred_membership AS
        (
            SELECT
                membership.run_id,
                membership.asset_id,
                membership.watch_url,
                collection.slug AS collection_slug,
                ROW_NUMBER() OVER
                (
                    PARTITION BY membership.run_id, membership.asset_id
                    ORDER BY
                        collection.slug,
                        membership.ordinal_position
                ) AS preferred_number
            FROM membership_observation AS membership
            JOIN collection_observation AS collection
              ON collection.run_id = membership.run_id
             AND collection.collection_id = membership.collection_id
        )
        SELECT
            configured_show.canonical_key AS show_slug,
            observation.run_id,
            observation.asset_id,
            asset.media_manager_guid,
            asset.pbs_kids_cms_id,
            asset.legacy_tp_media_id,
            observation.title,
            observation.slug AS asset_slug,
            observation.video_type,
            observation.duration_seconds,
            membership.collection_slug,
            membership.watch_url
        FROM ranked_runs AS current
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
        WHERE current.run_number = 1
          AND configured_show.canonical_key = ?
          AND observation.asset_id = ?
        """,
        (show_slug, asset_id),
    ).fetchone()

    if row is None:
        raise ValueError(
            f"Asset {asset_id} was not found in the latest complete catalog "
            f"run for show {show_slug!r}."
        )
    return row


def shell_command(arguments: list[str]) -> str:
    """Render a command that is safe to paste into a POSIX shell."""
    return shlex.join(arguments)


def build_format_report(
    asset: sqlite3.Row,
    page: watch_parser.WatchPage,
    output_directory: Path,
    downloader_command: str,
) -> dict[str, Any]:
    """Build a serializable report for all watch-page media profiles."""
    if page.media_manager_guid != asset["media_manager_guid"]:
        raise ValueError(
            "Watch-page Media Manager GUID does not match the catalog asset: "
            f"catalog={asset['media_manager_guid']!r}, "
            f"watch_page={page.media_manager_guid!r}."
        )

    base_name = video_download.sanitize_component(asset["title"])
    output_path = output_directory / f"{base_name} [{asset['asset_id']}].mp4"
    formats: list[dict[str, Any]] = []

    for media in page.media_profiles:
        drm_protected = page.drm_enabled or media.has_drm_license
        command = None
        if not drm_protected:
            command = shell_command(
                [
                    downloader_command,
                    "--no-playlist",
                    "--merge-output-format",
                    "mp4",
                    "--output",
                    str(output_path),
                    media.redirect_url,
                ]
            )

        formats.append(
            {
                "profile": media.profile,
                "url": media.redirect_url,
                "drm_protected": drm_protected,
                "fairplay_license_url": media.fairplay_license_url,
                "widevine_license_url": media.widevine_license_url,
                "retrieval_command": command,
            }
        )

    return {
        "show_slug": asset["show_slug"],
        "asset_id": asset["asset_id"],
        "title": asset["title"],
        "video_type": asset["video_type"],
        "media_manager_guid": asset["media_manager_guid"],
        "pbs_kids_cms_id": asset["pbs_kids_cms_id"],
        "watch_url": asset["watch_url"],
        "watch_page_title": page.title,
        "duration_seconds": page.duration_seconds,
        "source_expires_at": page.source_expires_at,
        "drm_enabled": page.drm_enabled,
        "format_count": len(formats),
        "formats": formats,
    }


def print_text_report(report: dict[str, Any]) -> None:
    """Print a readable format report."""
    print(f"Show:               {report['show_slug']}")
    print(f"Asset ID:            {report['asset_id']}")
    print(f"Title:               {report['title']}")
    print(f"Video type:          {report['video_type']}")
    print(f"Media Manager GUID:  {report['media_manager_guid']}")
    print(f"PBS KIDS CMS ID:     {report['pbs_kids_cms_id']}")
    print(f"Watch URL:           {report['watch_url']}")
    print(f"Duration:            {report['duration_seconds']}")
    print(f"Source expiry:       {report['source_expires_at']}")
    print(f"DRM enabled:         {report['drm_enabled']}")
    print(f"Formats:             {report['format_count']}")

    if not report["formats"]:
        print("\nNo media profiles were reported by the watch page.")
        return

    for number, media in enumerate(report["formats"], start=1):
        print(f"\nFormat {number}")
        print(f"  Profile:            {media['profile']}")
        print(f"  URL:                {media['url']}")
        print(f"  DRM protected:      {media['drm_protected']}")
        if media["fairplay_license_url"]:
            print(f"  FairPlay license:   {media['fairplay_license_url']}")
        if media["widevine_license_url"]:
            print(f"  Widevine license:   {media['widevine_license_url']}")
        if media["retrieval_command"]:
            print("  Retrieval command:")
            print(f"    {media['retrieval_command']}")
        else:
            print("  Retrieval command:  omitted because the profile is DRM-protected")


def build_cli(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Display the media formats exposed for one PBS KIDS catalog asset."
        )
    )
    parser.add_argument("show_slug", help="Configured show canonical key.")
    parser.add_argument("asset_id", type=int, help="Internal SQLite asset ID.")
    parser.add_argument("--db", type=Path, help="Override the configured database.")
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path.cwd(),
        help="Directory used in generated retrieval commands.",
    )
    parser.add_argument(
        "--downloader-command",
        default=None,
        help="Downloader executable used in generated commands.",
    )
    parser.add_argument(
        "--output-format",
        choices=("text", "json"),
        default=DEFAULT_OUTPUT_FORMAT,
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        catalog_settings, remaining = catalog_database.load_settings(argv)
        args = build_cli(remaining)
        database = args.db.expanduser() if args.db else catalog_settings.database
        download_settings = video_download.load_download_settings(
            catalog_settings.config_path
        )
        downloader_command = (
            args.downloader_command or download_settings.yt_dlp_command
        )

        catalog_database.initialize_database(database, catalog_settings)
        with catalog_database.connect(database, catalog_settings) as connection:
            video_download.ensure_download_schema(connection)
            asset = find_catalog_asset(
                connection,
                args.show_slug,
                args.asset_id,
            )

        html, _ = video_download.fetch_watch_page(
            asset["watch_url"],
            catalog_settings,
        )
        page = watch_parser.parse_watch_page(html)
        report = build_format_report(
            asset,
            page,
            args.output_directory.expanduser(),
            downloader_command,
        )

        if args.output_format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print_text_report(report)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
