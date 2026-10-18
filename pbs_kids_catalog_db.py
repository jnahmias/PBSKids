#!/usr/bin/env python3
"""Maintain a multi-show PBS KIDS catalog in SQLite."""

from __future__ import annotations

import argparse
import configparser
import gzip
import hashlib
import json
import logging
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pbs_kids_catalog_parser as catalog_parser

try:
    import httpx
except ImportError:
    httpx = None


APP_VERSION = "0.3.0"
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = "pbs_kids_catalog.ini"
LOG = logging.getLogger("pbs_kids_catalog")


@dataclass(frozen=True)
class Settings:
    config_path: Path
    database: Path
    snapshot_root: Path | None
    log_level: str
    timeout_seconds: float
    user_agent: str
    busy_timeout_ms: int
    journal_mode: str
    synchronous: str


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def canonical_hash(value: Any) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def resolve_path(raw: str | None, base: Path, default: Path | None) -> Path | None:
    if raw is None or not raw.strip():
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else base / path


def load_settings(argv: Sequence[str] | None = None) -> tuple[Settings, list[str]]:
    arguments = list(argv if argv is not None else sys.argv[1:])
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", default=DEFAULT_CONFIG)
    preliminary, remaining = pre_parser.parse_known_args(arguments)

    config_path = Path(preliminary.config).expanduser()
    explicit_config = "--config" in arguments
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path

    config = configparser.ConfigParser(interpolation=None)
    if config_path.exists():
        try:
            with config_path.open(encoding="utf-8") as handle:
                config.read_file(handle)
        except (configparser.Error, OSError) as exc:
            raise ValueError(f"Cannot read configuration {config_path}: {exc}") from exc
    elif explicit_config:
        raise ValueError(f"Configuration file not found: {config_path}")

    base = config_path.parent
    settings = Settings(
        config_path=config_path,
        database=resolve_path(
            config.get(
                "application",
                "database",
                fallback="pbs-kids-catalog.sqlite3",
            ),
            base,
            None,
        ),
        snapshot_root=resolve_path(
            config.get("application", "snapshot_root", fallback=""),
            base,
            None,
        ),
        log_level=config.get(
            "application",
            "log_level",
            fallback="INFO",
        ).upper(),
        timeout_seconds=config.getfloat("http", "timeout_seconds", fallback=30.0),
        user_agent=config.get(
            "http",
            "user_agent",
            fallback="PBSKidsCatalog/0.3 (+personal catalog synchronization)",
        ),
        busy_timeout_ms=config.getint("sqlite", "busy_timeout_ms", fallback=5000),
        journal_mode=config.get("sqlite", "journal_mode", fallback="WAL").upper(),
        synchronous=config.get("sqlite", "synchronous", fallback="NORMAL").upper(),
    )

    if settings.log_level not in logging._nameToLevel:
        raise ValueError(f"Invalid log_level: {settings.log_level}")
    if settings.journal_mode not in {
        "DELETE",
        "TRUNCATE",
        "PERSIST",
        "MEMORY",
        "WAL",
        "OFF",
    }:
        raise ValueError(f"Invalid journal_mode: {settings.journal_mode}")
    if settings.synchronous not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
        raise ValueError(f"Invalid synchronous setting: {settings.synchronous}")

    return settings, remaining


def connect(database: Path, settings: Settings) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        database,
        timeout=settings.busy_timeout_ms / 1000,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA journal_mode = {settings.journal_mode}")
    connection.execute(f"PRAGMA synchronous = {settings.synchronous}")
    connection.execute(f"PRAGMA busy_timeout = {settings.busy_timeout_ms}")
    return connection


def initialize_database(database: Path, settings: Settings) -> None:
    schema = (HERE / "pbs_kids_catalog_schema.sql").read_text(encoding="utf-8")
    checksum = hashlib.sha256(schema.encode("utf-8")).hexdigest()

    with connect(database, settings) as connection:
        connection.executescript(schema)
        existing = connection.execute(
            "SELECT checksum FROM schema_migration WHERE version = 1"
        ).fetchone()
        if existing and existing["checksum"] != checksum:
            raise RuntimeError("Schema migration checksum mismatch.")
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_migration
                (version, applied_at, description, checksum)
            VALUES
                (1, ?, 'Initial schema', ?)
            """,
            (utc_now(), checksum),
        )


def fetch_catalog(url: str, settings: Settings) -> tuple[str, dict[str, Any]]:
    if httpx is None:
        raise RuntimeError("Live retrieval requires the httpx package.")

    with httpx.Client(
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        },
        follow_redirects=True,
        timeout=settings.timeout_seconds,
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text, {
            "status": response.status_code,
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "final_url": str(response.url),
        }


def get_html(
    url: str,
    html_path: Path | None,
    settings: Settings,
) -> tuple[str, dict[str, Any]]:
    if html_path is not None:
        return html_path.read_text(encoding="utf-8"), {
            "status": None,
            "etag": None,
            "last_modified": None,
            "final_url": url,
        }
    return fetch_catalog(url, settings)


def build_discovery_report(
    parsed: catalog_parser.ParsedCatalog,
    source_url: str,
) -> dict[str, Any]:
    type_counts: dict[str, int] = {}
    missing = {
        "media_manager_guid": 0,
        "pbs_kids_cms_id": 0,
        "legacy_tp_media_id": 0,
        "duration_seconds": 0,
    }

    for video in parsed.videos:
        type_counts[video.video_type] = type_counts.get(video.video_type, 0) + 1
        if not video.media_manager_guid:
            missing["media_manager_guid"] += 1
        if not video.cms_id:
            missing["pbs_kids_cms_id"] += 1
        if not video.legacy_tp_media_id:
            missing["legacy_tp_media_id"] += 1
        if video.duration_seconds is None:
            missing["duration_seconds"] += 1

    warnings: list[str] = []
    if parsed.metadata.occurrence_count > parsed.metadata.unique_asset_count:
        warnings.append(
            "The page contains duplicate renderings; they will be deduplicated by GUID."
        )
    if missing["legacy_tp_media_id"]:
        warnings.append("Some assets do not contain a legacy TP media ID.")

    return {
        "source_url": source_url,
        "page_title": parsed.metadata.page_title,
        "page_slug": parsed.metadata.page_slug,
        "next_build_id": parsed.metadata.build_id,
        "occurrence_count": parsed.metadata.occurrence_count,
        "unique_asset_count": parsed.metadata.unique_asset_count,
        "recommended_minimum_assets": max(
            1,
            int(parsed.metadata.unique_asset_count * 0.8),
        ),
        "video_types": type_counts,
        "missing_fields": missing,
        "collections": [asdict(item) for item in parsed.collections],
        "sample_watch_urls": [
            membership.watch_url
            for video in parsed.videos[:3]
            for membership in video.memberships[:1]
        ],
        "warnings": warnings,
    }


def print_discovery_report(report: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return

    print(f"Page title:             {report['page_title']}")
    print(f"Page slug:              {report['page_slug']}")
    print(f"Unique assets:          {report['unique_asset_count']}")
    print(f"Catalog occurrences:    {report['occurrence_count']}")
    print(f"Recommended minimum:    {report['recommended_minimum_assets']}")
    print("Video types:")
    for video_type, count in sorted(report["video_types"].items()):
        print(f"  {video_type}: {count}")
    print("Collections:")
    for collection in report["collections"]:
        print(
            f"  {collection['slug']} "
            f"(id={collection['pbs_collection_id']}, "
            f"entries={collection['entry_count']})"
        )
    if report["warnings"]:
        print("Warnings:")
        for warning in report["warnings"]:
            print(f"  - {warning}")


def find_show(connection: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM show WHERE canonical_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown show {key!r}.")
    return row


def configure_show(
    connection: sqlite3.Connection,
    key: str,
    url: str,
    minimum_assets: int | None,
    frequency: str | None,
    enabled: bool,
) -> int:
    connection.execute(
        """
        INSERT INTO show
            (canonical_key, catalog_url, enabled,
             expected_minimum_assets, retrieval_frequency)
        VALUES
            (?, ?, ?, ?, ?)
        ON CONFLICT(canonical_key) DO UPDATE SET
            catalog_url = excluded.catalog_url,
            enabled = excluded.enabled,
            expected_minimum_assets = excluded.expected_minimum_assets,
            retrieval_frequency = excluded.retrieval_frequency
        """,
        (key, url, int(enabled), minimum_assets, frequency),
    )
    return int(
        connection.execute(
            "SELECT show_id FROM show WHERE canonical_key = ?",
            (key,),
        ).fetchone()[0]
    )


def onboard_show(
    connection: sqlite3.Connection,
    key: str,
    url: str,
    report: dict[str, Any],
    minimum_assets: int | None,
    frequency: str | None,
    required_collections: list[str],
    enable: bool,
    replace: bool,
) -> int:
    existing = connection.execute(
        "SELECT show_id FROM show WHERE canonical_key = ?",
        (key,),
    ).fetchone()
    if existing and not replace:
        raise ValueError(
            f"Show {key!r} already exists. Use --replace to update its configuration."
        )

    minimum = (
        minimum_assets
        if minimum_assets is not None
        else int(report["recommended_minimum_assets"])
    )
    with connection:
        show_id = configure_show(
            connection,
            key,
            url,
            minimum,
            frequency,
            enable,
        )
        if replace:
            connection.execute(
                "DELETE FROM show_required_collection WHERE show_id = ?",
                (show_id,),
            )
        for slug in required_collections:
            connection.execute(
                """
                INSERT OR IGNORE INTO show_required_collection
                    (show_id, collection_slug)
                VALUES
                    (?, ?)
                """,
                (show_id, slug),
            )
    return show_id


def build_cli(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Maintain and discover PBS KIDS catalog pages."
    )
    parser.add_argument("--db", type=Path)
    parser.add_argument("--snapshot-root", type=Path)
    parser.add_argument("--log-level", choices=sorted(logging._nameToLevel))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create the database schema.")
    subparsers.add_parser("list-shows", help="List configured shows.")

    add_show = subparsers.add_parser("add-show", help="Add or update a show.")
    add_show.add_argument("key")
    add_show.add_argument("url")
    add_show.add_argument("--minimum-assets", type=int)
    add_show.add_argument("--frequency")
    add_show.add_argument("--disabled", action="store_true")

    inspect_url = subparsers.add_parser(
        "inspect-url",
        help="Retrieve or read a catalog and report its structure without writing data.",
    )
    inspect_url.add_argument("url")
    inspect_url.add_argument("--html", type=Path)
    inspect_url.add_argument("--output-format", choices=("text", "json"), default="text")

    onboard = subparsers.add_parser(
        "onboard-show",
        help="Inspect a catalog and save a disabled show configuration by default.",
    )
    onboard.add_argument("key")
    onboard.add_argument("url")
    onboard.add_argument("--html", type=Path)
    onboard.add_argument("--minimum-assets", type=int)
    onboard.add_argument("--frequency", default="daily")
    onboard.add_argument("--require-collection", action="append", default=[])
    onboard.add_argument("--enable", action="store_true")
    onboard.add_argument("--replace", action="store_true")
    onboard.add_argument("--output-format", choices=("text", "json"), default="text")

    require_collection = subparsers.add_parser("require-collection")
    require_collection.add_argument("key")
    require_collection.add_argument("collection_slug")

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        settings, remaining = load_settings(argv)
        args = build_cli(remaining)
        logging.basicConfig(
            level=getattr(logging, args.log_level or settings.log_level),
            format="%(levelname)s: %(message)s",
        )
        database = args.db.expanduser() if args.db else settings.database
        initialize_database(database, settings)

        if args.command == "init":
            print(f"Initialized {database}")
            return 0

        if args.command in {"inspect-url", "onboard-show"}:
            html, response = get_html(args.url, args.html, settings)
            parsed = catalog_parser.parse_catalog_html(html, response["final_url"])
            report = build_discovery_report(parsed, response["final_url"])
            print_discovery_report(report, args.output_format)

            if args.command == "inspect-url":
                return 0

            with connect(database, settings) as connection:
                show_id = onboard_show(
                    connection=connection,
                    key=args.key,
                    url=args.url,
                    report=report,
                    minimum_assets=args.minimum_assets,
                    frequency=args.frequency,
                    required_collections=args.require_collection,
                    enable=args.enable,
                    replace=args.replace,
                )
            if args.output_format == "text":
                state = "enabled" if args.enable else "disabled"
                print(f"Saved show_id={show_id}; state={state}.")
            return 0

        if args.command == "add-show":
            with connect(database, settings) as connection:
                show_id = configure_show(
                    connection,
                    args.key,
                    args.url,
                    args.minimum_assets,
                    args.frequency,
                    not args.disabled,
                )
            print(f"Configured show_id={show_id}.")
            return 0

        if args.command == "require-collection":
            with connect(database, settings) as connection:
                show = find_show(connection, args.key)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO show_required_collection
                        (show_id, collection_slug)
                    VALUES
                        (?, ?)
                    """,
                    (show["show_id"], args.collection_slug),
                )
            print("Requirement saved.")
            return 0

        if args.command == "list-shows":
            with connect(database, settings) as connection:
                rows = connection.execute(
                    "SELECT * FROM show ORDER BY canonical_key"
                ).fetchall()
            for row in rows:
                print(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
            return 0

        raise RuntimeError(f"Unsupported command: {args.command}")
    except Exception as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
