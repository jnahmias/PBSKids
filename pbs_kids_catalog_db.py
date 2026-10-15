#!/usr/bin/env python3
"""PBS KIDS multi-show catalog updater backed by SQLite.

Phase 1 records catalog observations. Watch-page enrichment, media profiles,
captions, and downloads are intentionally reserved for later phases.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import pbs_kids_catalog_parser as parser

APP_VERSION = "0.1.0"
SCHEMA_VERSION = 1
LOG = logging.getLogger("pbs_kids_catalog_db")
HERE = Path(__file__).resolve().parent
DEFAULT_SCHEMA = HERE / "pbs_kids_catalog_schema.sql"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def initialize_database(db_path: Path, schema_path: Path = DEFAULT_SCHEMA) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = schema_path.read_text(encoding="utf-8")
    checksum = hashlib.sha256(schema.encode("utf-8")).hexdigest()
    with connect(db_path) as conn:
        conn.executescript(schema)
        row = conn.execute("SELECT checksum FROM schema_migration WHERE version = ?", (SCHEMA_VERSION,)).fetchone()
        if row and row["checksum"] != checksum:
            raise RuntimeError("Schema migration 1 checksum differs from the applied migration.")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migration(version, applied_at, description, checksum) VALUES (?, ?, ?, ?)",
            (SCHEMA_VERSION, utc_now(), "Initial multi-show catalog schema", checksum),
        )


def add_show(conn: sqlite3.Connection, key: str, url: str, minimum_assets: int | None,
             frequency: str | None, enabled: bool = True) -> int:
    conn.execute(
        """INSERT INTO show(canonical_key, catalog_url, enabled, expected_minimum_assets, retrieval_frequency)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(canonical_key) DO UPDATE SET
             catalog_url=excluded.catalog_url,
             enabled=excluded.enabled,
             expected_minimum_assets=excluded.expected_minimum_assets,
             retrieval_frequency=excluded.retrieval_frequency""",
        (key, url, int(enabled), minimum_assets, frequency),
    )
    return int(conn.execute("SELECT show_id FROM show WHERE canonical_key = ?", (key,)).fetchone()[0])


def get_show(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM show WHERE canonical_key = ?", (key,)).fetchone()
    if not row:
        raise ValueError(f"Unknown show {key!r}. Add it with the add-show command.")
    return row


def resolve_asset(conn: sqlite3.Connection, run_id: int, video: parser.CatalogVideo) -> tuple[int | None, bool]:
    identifiers = {
        "media_manager_guid": video.guid or None,
        "pbs_kids_cms_id": video.cms_id or None,
        "legacy_tp_media_id": video.legacy_tp_media_id or None,
    }
    matches: set[int] = set()
    for column, value in identifiers.items():
        if value is None:
            continue
        rows = conn.execute(f"SELECT asset_id FROM asset WHERE {column} = ?", (value,)).fetchall()
        matches.update(int(r[0]) for r in rows)

    if len(matches) > 1:
        conn.execute(
            """INSERT INTO identity_conflict
               (run_id, media_manager_guid, pbs_kids_cms_id, legacy_tp_media_id,
                matched_asset_ids, source_record_json, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, identifiers["media_manager_guid"], identifiers["pbs_kids_cms_id"],
             identifiers["legacy_tp_media_id"], json.dumps(sorted(matches)),
             json.dumps(asdict(video), sort_keys=True, ensure_ascii=False), utc_now()),
        )
        return None, True

    if matches:
        asset_id = next(iter(matches))
        current = conn.execute("SELECT * FROM asset WHERE asset_id = ?", (asset_id,)).fetchone()
        updates: dict[str, str] = {}
        for column, value in identifiers.items():
            if value and current[column] is None:
                conflict = conn.execute(
                    f"SELECT asset_id FROM asset WHERE {column} = ? AND asset_id <> ?", (value, asset_id)
                ).fetchone()
                if conflict:
                    conn.execute(
                        """INSERT INTO identity_conflict
                           (run_id, media_manager_guid, pbs_kids_cms_id, legacy_tp_media_id,
                            matched_asset_ids, source_record_json, detected_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (run_id, identifiers["media_manager_guid"], identifiers["pbs_kids_cms_id"],
                         identifiers["legacy_tp_media_id"], json.dumps([asset_id, int(conflict[0])]),
                         json.dumps(asdict(video), sort_keys=True, ensure_ascii=False), utc_now()),
                    )
                    return None, True
                updates[column] = value
            elif value and current[column] not in (None, value):
                conn.execute(
                    """INSERT INTO identity_conflict
                       (run_id, media_manager_guid, pbs_kids_cms_id, legacy_tp_media_id,
                        matched_asset_ids, source_record_json, detected_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, identifiers["media_manager_guid"], identifiers["pbs_kids_cms_id"],
                     identifiers["legacy_tp_media_id"], json.dumps([asset_id]),
                     json.dumps(asdict(video), sort_keys=True, ensure_ascii=False), utc_now()),
                )
                return None, True
        if updates:
            clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE asset SET {clause} WHERE asset_id = ?", (*updates.values(), asset_id))
        return asset_id, False

    cur = conn.execute(
        "INSERT INTO asset(media_manager_guid, pbs_kids_cms_id, legacy_tp_media_id) VALUES (?, ?, ?)",
        (identifiers["media_manager_guid"], identifiers["pbs_kids_cms_id"], identifiers["legacy_tp_media_id"]),
    )
    return int(cur.lastrowid), False


def snapshot_html(snapshot_root: Path | None, key: str, observed_at: str, html: str) -> str | None:
    if snapshot_root is None:
        return None
    dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    relative = Path(f"{dt:%Y/%m}") / f"{dt:%Y%m%dT%H%M%S}-{key}-catalog.html.gz"
    target = snapshot_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    with gzip.open(temp, "wt", encoding="utf-8") as handle:
        handle.write(html)
    temp.replace(target)
    return relative.as_posix()


def ingest_catalog(conn: sqlite3.Connection, show: sqlite3.Row, html: str, observed_at: str,
                   snapshot_path: str | None, http_status: int | None = None,
                   etag: str | None = None, last_modified: str | None = None) -> int:
    response_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    source_url = str(show["catalog_url"])
    try:
        catalog = parser.parse_catalog_html(html, source_url=source_url)
        parse_error = None
    except Exception as exc:
        catalog = None
        parse_error = str(exc)

    with conn:
        cur = conn.execute(
            """INSERT INTO catalog_run
               (show_id, observed_at, source_url, http_status, response_sha256,
                response_etag, response_last_modified, snapshot_relative_path,
                next_build_id, parser_version, retrieval_succeeded, parse_succeeded,
                catalog_complete, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, ?)""",
            (show["show_id"], observed_at, source_url, http_status, response_hash, etag,
             last_modified, snapshot_path,
             catalog.metadata.next_build_id if catalog else None,
             APP_VERSION, int(catalog is not None), parse_error),
        )
        run_id = int(cur.lastrowid)
        if catalog is None:
            return run_id

        show_value = {"source_slug": catalog.metadata.page_slug, "source_title": catalog.metadata.page_title}
        conn.execute(
            "INSERT INTO show_observation(run_id, show_id, source_slug, source_title, nola_root, metadata_hash) VALUES (?, ?, ?, ?, NULL, ?)",
            (run_id, show["show_id"], catalog.metadata.page_slug,
             catalog.metadata.page_title or show["canonical_key"], canonical_hash(show_value)),
        )

        collection_map: dict[tuple[str, str], int] = {}
        collection_positions: dict[tuple[str, str], dict[str, int]] = {}
        conflicts = 0

        for video in catalog.videos:
            asset_id, conflicted = resolve_asset(conn, run_id, video)
            if conflicted or asset_id is None:
                conflicts += 1
                continue
            metadata = {
                "slug": video.slug, "title": video.title, "video_type": video.video_type,
                "description_short": video.description_short,
                "duration_seconds": video.duration_seconds,
                "source_premiered_at": video.premiered_on_utc,
            }
            conn.execute(
                """INSERT INTO asset_observation
                   (run_id, asset_id, slug, title, video_type, description_short,
                    duration_seconds, source_premiered_at, metadata_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, asset_id, video.slug, video.title, video.video_type,
                 video.description_short, video.duration_seconds, video.premiered_on_utc,
                 canonical_hash(metadata)),
            )
            conn.execute(
                "INSERT INTO asset_show_observation(run_id, show_id, asset_id) VALUES (?, ?, ?)",
                (run_id, show["show_id"], asset_id),
            )
            for image in video.images:
                conn.execute(
                    "INSERT INTO asset_image_observation(run_id, asset_id, profile, image_url) VALUES (?, ?, ?, ?)",
                    (run_id, asset_id, image.profile, image.url),
                )

            for membership in video.memberships:
                key = (membership.id, membership.slug)
                if key not in collection_map:
                    existing = conn.execute(
                        "SELECT collection_id, show_id FROM collection WHERE pbs_collection_id = ?", (membership.id,)
                    ).fetchone()
                    if existing and int(existing["show_id"]) != int(show["show_id"]):
                        raise RuntimeError(f"PBS collection {membership.id} is already owned by another show")
                    if existing:
                        collection_id = int(existing["collection_id"])
                    else:
                        collection_id = int(conn.execute(
                            "INSERT INTO collection(show_id, pbs_collection_id) VALUES (?, ?)",
                            (show["show_id"], membership.id),
                        ).lastrowid)
                    collection_map[key] = collection_id
                    value = {"slug": membership.slug, "title": membership.title, "heading": membership.heading}
                    conn.execute(
                        """INSERT INTO collection_observation
                           (run_id, collection_id, slug, title, heading, metadata_hash)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (run_id, collection_id, membership.slug, membership.title,
                         membership.heading, canonical_hash(value)),
                    )
                    collection_positions[key] = {}
                collection_id = collection_map[key]
                # Deduplicate body/masthead repetitions. First occurrence determines order.
                if video.guid not in collection_positions[key]:
                    position = len(collection_positions[key])
                    collection_positions[key][video.guid] = position
                    conn.execute(
                        """INSERT INTO membership_observation
                           (run_id, collection_id, asset_id, ordinal_position, watch_url)
                           VALUES (?, ?, ?, ?, ?)""",
                        (run_id, collection_id, asset_id, position, membership.watch_url),
                    )

        observed_assets = conn.execute(
            "SELECT COUNT(*) FROM asset_observation WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        required = {r[0] for r in conn.execute(
            "SELECT collection_slug FROM show_required_collection WHERE show_id = ?", (show["show_id"],)
        )}
        observed_collections = {r[0] for r in conn.execute(
            """SELECT co.slug FROM collection_observation co WHERE co.run_id = ?""", (run_id,)
        )}
        minimum = show["expected_minimum_assets"]
        complete = conflicts == 0 and observed_assets > 0
        if minimum is not None:
            complete = complete and observed_assets >= int(minimum)
        complete = complete and required.issubset(observed_collections)
        message_parts = []
        if conflicts:
            message_parts.append(f"{conflicts} identity conflict(s)")
        if minimum is not None and observed_assets < int(minimum):
            message_parts.append(f"asset count {observed_assets} below minimum {minimum}")
        missing = sorted(required - observed_collections)
        if missing:
            message_parts.append("missing required collections: " + ", ".join(missing))
        conn.execute(
            "UPDATE catalog_run SET catalog_complete = ?, error_message = ? WHERE run_id = ?",
            (int(complete), "; ".join(message_parts) or None, run_id),
        )
        return run_id


def record_retrieval_failure(conn: sqlite3.Connection, show: sqlite3.Row, observed_at: str, error: str) -> int:
    with conn:
        cur = conn.execute(
            """INSERT INTO catalog_run
               (show_id, observed_at, source_url, parser_version, retrieval_succeeded,
                parse_succeeded, catalog_complete, error_message)
               VALUES (?, ?, ?, ?, 0, 0, 0, ?)""",
            (show["show_id"], observed_at, show["catalog_url"], APP_VERSION, error),
        )
        return int(cur.lastrowid)


def update_show(db_path: Path, key: str, html_path: Path | None,
                snapshot_root: Path | None, observed_at: str | None) -> int:
    initialize_database(db_path)
    observed_at = observed_at or utc_now()
    with connect(db_path) as conn:
        show = get_show(conn, key)
        if not show["enabled"]:
            raise ValueError(f"Show {key!r} is disabled.")
        try:
            if html_path:
                html = html_path.read_text(encoding="utf-8")
                http_status = None
                etag = last_modified = None
            else:
                if parser.httpx is None:
                    raise RuntimeError("Live retrieval requires httpx")
                headers = {"User-Agent": parser.USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
                with parser.httpx.Client(headers=headers, follow_redirects=True, timeout=30.0) as client:
                    response = client.get(show["catalog_url"])
                    response.raise_for_status()
                    html = response.text
                    http_status = response.status_code
                    etag = response.headers.get("etag")
                    last_modified = response.headers.get("last-modified")
            relative = snapshot_html(snapshot_root, key, observed_at, html)
            return ingest_catalog(conn, show, html, observed_at, relative, http_status, etag, last_modified)
        except Exception as exc:
            run_id = record_retrieval_failure(conn, show, observed_at, str(exc))
            raise RuntimeError(f"Run {run_id} failed: {exc}") from exc


def print_rows(rows: Iterable[sqlite3.Row]) -> None:
    for row in rows:
        print(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))


def build_cli() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Maintain a multi-show PBS KIDS catalog in SQLite.")
    ap.add_argument("--db", type=Path, default=Path("pbs-kids-catalog.sqlite3"))
    ap.add_argument("--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create or upgrade the database schema.")

    p = sub.add_parser("add-show", help="Add or update a show configuration.")
    p.add_argument("key")
    p.add_argument("url")
    p.add_argument("--minimum-assets", type=int)
    p.add_argument("--frequency")
    p.add_argument("--disabled", action="store_true")

    p = sub.add_parser("require-collection", help="Require a collection for completeness.")
    p.add_argument("key")
    p.add_argument("collection_slug")

    p = sub.add_parser("update", help="Retrieve or import one show catalog.")
    p.add_argument("key")
    p.add_argument("--html", type=Path)
    p.add_argument("--snapshot-root", type=Path)
    p.add_argument("--observed-at", help="UTC ISO-8601 timestamp; defaults to now.")

    sub.add_parser("list-shows")
    p = sub.add_parser("runs")
    p.add_argument("--key")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("current")
    p.add_argument("key")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_cli().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")
    try:
        if args.command == "init":
            initialize_database(args.db)
            print(f"Initialized {args.db}")
            return 0
        initialize_database(args.db)
        if args.command == "add-show":
            with connect(args.db) as conn:
                show_id = add_show(conn, args.key, args.url, args.minimum_assets,
                                   args.frequency, not args.disabled)
            print(f"show_id={show_id}")
        elif args.command == "require-collection":
            with connect(args.db) as conn:
                show = get_show(conn, args.key)
                conn.execute("INSERT OR IGNORE INTO show_required_collection(show_id, collection_slug) VALUES (?, ?)",
                             (show["show_id"], args.collection_slug))
            print("Requirement saved")
        elif args.command == "update":
            run_id = update_show(args.db, args.key, args.html, args.snapshot_root, args.observed_at)
            with connect(args.db) as conn:
                row = conn.execute("SELECT * FROM catalog_run WHERE run_id = ?", (run_id,)).fetchone()
            print(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
        elif args.command == "list-shows":
            with connect(args.db) as conn:
                print_rows(conn.execute("SELECT * FROM show ORDER BY canonical_key"))
        elif args.command == "runs":
            with connect(args.db) as conn:
                if args.key:
                    rows = conn.execute(
                        """SELECT cr.*, s.canonical_key FROM catalog_run cr JOIN show s USING(show_id)
                           WHERE s.canonical_key = ? ORDER BY cr.observed_at DESC, cr.run_id DESC LIMIT ?""",
                        (args.key, args.limit),
                    )
                else:
                    rows = conn.execute(
                        """SELECT cr.*, s.canonical_key FROM catalog_run cr JOIN show s USING(show_id)
                           ORDER BY cr.observed_at DESC, cr.run_id DESC LIMIT ?""", (args.limit,)
                    )
                print_rows(rows)
        elif args.command == "current":
            with connect(args.db) as conn:
                rows = conn.execute(
                    """SELECT c.* FROM current_catalog_by_show c JOIN show s USING(show_id)
                       WHERE s.canonical_key = ? ORDER BY c.video_type, c.title""", (args.key,)
                )
                print_rows(rows)
        return 0
    except Exception as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
