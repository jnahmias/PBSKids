#!/usr/bin/env python3
from __future__ import annotations
import argparse, configparser, gzip, hashlib, json, logging, sqlite3, sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
import pbs_kids_catalog_parser as parser

APP_VERSION = "0.2.0"
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


def now():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def sha(v):
    return hashlib.sha256(
        json.dumps(
            v, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def path_value(raw: str | None, base: Path, default: Path | None) -> Path | None:
    if raw is None or not raw.strip():
        return default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else base / p


def load_settings(argv: Sequence[str] | None = None) -> tuple[Settings, list[str]]:
    args = list(argv if argv is not None else sys.argv[1:])
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=DEFAULT_CONFIG)
    ns, remaining = pre.parse_known_args(args)
    cp = Path(ns.config).expanduser()
    explicit = "--config" in args
    if not cp.is_absolute():
        cp = Path.cwd() / cp
    cfg = configparser.ConfigParser(interpolation=None)
    if cp.exists():
        try:
            with cp.open(encoding="utf-8") as f:
                cfg.read_file(f)
        except (configparser.Error, OSError) as e:
            raise ValueError(f"Cannot read configuration {cp}: {e}") from e
    elif explicit:
        raise ValueError(f"Configuration file not found: {cp}")
    base = cp.parent

    def get(sec, key, default):
        return cfg.get(sec, key, fallback=default)

    try:
        s = Settings(
            cp,
            path_value(
                get("application", "database", "pbs-kids-catalog.sqlite3"), base, None
            ),
            path_value(get("application", "snapshot_root", ""), base, None),
            get("application", "log_level", "INFO").upper(),
            float(get("http", "timeout_seconds", "30")),
            get("http", "user_agent", "PBSKidsCatalog/0.2"),
            int(get("sqlite", "busy_timeout_ms", "5000")),
            get("sqlite", "journal_mode", "WAL").upper(),
            get("sqlite", "synchronous", "NORMAL").upper(),
        )
    except ValueError as e:
        raise ValueError(f"Invalid value in {cp}: {e}") from e
    if s.log_level not in logging._nameToLevel:
        raise ValueError(f"Invalid log_level: {s.log_level}")
    if s.journal_mode not in {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}:
        raise ValueError(f"Invalid journal_mode: {s.journal_mode}")
    if s.synchronous not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
        raise ValueError(f"Invalid synchronous: {s.synchronous}")
    return s, remaining


def connect(path: Path, s: Settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=s.busy_timeout_ms / 1000)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(f"PRAGMA journal_mode={s.journal_mode}")
    c.execute(f"PRAGMA synchronous={s.synchronous}")
    c.execute(f"PRAGMA busy_timeout={s.busy_timeout_ms}")
    return c


def init_db(path: Path, s: Settings):
    sql = (HERE / "pbs_kids_catalog_schema.sql").read_text()
    h = hashlib.sha256(sql.encode()).hexdigest()
    with connect(path, s) as c:
        c.executescript(sql)
        r = c.execute(
            "SELECT checksum FROM schema_migration WHERE version=1"
        ).fetchone()
        if r and r[0] != h:
            raise RuntimeError("Schema migration checksum mismatch")
        c.execute(
            "INSERT OR IGNORE INTO schema_migration VALUES(1,?,?,?)",
            (now(), "Initial schema", h),
        )


def show(c, key):
    r = c.execute("SELECT * FROM show WHERE canonical_key=?", (key,)).fetchone()
    if not r:
        raise ValueError(f"Unknown show {key!r}")
    return r


def asset(c, v):
    ids = (v.guid, v.cms_id, v.legacy_tp_media_id)
    matches = set()
    for col, val in zip(
        ("media_manager_guid", "pbs_kids_cms_id", "legacy_tp_media_id"), ids
    ):
        if val:
            matches |= {
                r[0]
                for r in c.execute(f"SELECT asset_id FROM asset WHERE {col}=?", (val,))
            }
    if len(matches) > 1:
        return None, matches
    if matches:
        return next(iter(matches)), set()
    return (
        c.execute(
            "INSERT INTO asset(media_manager_guid,pbs_kids_cms_id,legacy_tp_media_id) VALUES(?,?,?)",
            ids,
        ).lastrowid,
        set(),
    )


def snapshot(root: Path | None, key: str, stamp: str, html: str):
    if not root:
        return None
    d = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    rel = Path(f"{d:%Y/%m}/{d:%Y%m%dT%H%M%S}-{key}.html.gz")
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(html)
    return rel.as_posix()


def ingest(c, sh, html, stamp, snap, http_status=None, etag=None, last_modified=None):
    cat = parser.parse_catalog_html(html, str(sh["catalog_url"]))
    rh = hashlib.sha256(html.encode()).hexdigest()
    with c:
        run = c.execute(
            """INSERT INTO catalog_run(show_id,observed_at,source_url,http_status,response_sha256,response_etag,response_last_modified,snapshot_relative_path,next_build_id,parser_version,retrieval_succeeded,parse_succeeded,catalog_complete) VALUES(?,?,?,?,?,?,?,?,?,?,1,1,0)""",
            (
                sh["show_id"],
                stamp,
                sh["catalog_url"],
                http_status,
                rh,
                etag,
                last_modified,
                snap,
                cat.metadata.next_build_id,
                APP_VERSION,
            ),
        ).lastrowid
        sv = {"slug": cat.metadata.page_slug, "title": cat.metadata.page_title}
        c.execute(
            "INSERT INTO show_observation VALUES(?,?,?,?,?)",
            (
                run,
                sh["show_id"],
                cat.metadata.page_slug,
                cat.metadata.page_title or sh["canonical_key"],
                sha(sv),
            ),
        )
        collections = {}
        positions = {}
        conflicts = 0
        for v in cat.videos:
            aid, bad = asset(c, v)
            if bad:
                conflicts += 1
                c.execute(
                    "INSERT INTO identity_conflict(run_id,media_manager_guid,pbs_kids_cms_id,legacy_tp_media_id,matched_asset_ids,source_record_json,detected_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        run,
                        v.guid,
                        v.cms_id,
                        v.legacy_tp_media_id,
                        json.dumps(sorted(bad)),
                        json.dumps(asdict(v)),
                        now(),
                    ),
                )
                continue
            md = {
                "slug": v.slug,
                "title": v.title,
                "video_type": v.video_type,
                "description_short": v.description_short,
                "duration_seconds": v.duration_seconds,
                "source_premiered_at": v.premiered_on_utc,
            }
            c.execute(
                "INSERT INTO asset_observation VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run,
                    aid,
                    v.slug,
                    v.title,
                    v.video_type,
                    v.description_short,
                    v.duration_seconds,
                    v.premiered_on_utc,
                    sha(md),
                ),
            )
            c.execute(
                "INSERT INTO asset_show_observation VALUES(?,?,?)",
                (run, sh["show_id"], aid),
            )
            for im in v.images:
                c.execute(
                    "INSERT OR IGNORE INTO asset_image_observation VALUES(?,?,?,?)",
                    (run, aid, im.profile, im.url),
                )
            for m in v.memberships:
                if m.id not in collections:
                    r = c.execute(
                        "SELECT collection_id,show_id FROM collection WHERE pbs_collection_id=?",
                        (m.id,),
                    ).fetchone()
                    if r and r["show_id"] != sh["show_id"]:
                        raise RuntimeError(f"Collection {m.id} belongs to another show")
                    cid = (
                        r["collection_id"]
                        if r
                        else c.execute(
                            "INSERT INTO collection(show_id,pbs_collection_id) VALUES(?,?)",
                            (sh["show_id"], m.id),
                        ).lastrowid
                    )
                    collections[m.id] = cid
                    positions[m.id] = {}
                    q = {"slug": m.slug, "title": m.title, "heading": m.heading}
                    c.execute(
                        "INSERT INTO collection_observation VALUES(?,?,?,?,?,?)",
                        (run, cid, m.slug, m.title, m.heading, sha(q)),
                    )
                if aid not in positions[m.id]:
                    pos = len(positions[m.id])
                    positions[m.id][aid] = pos
                    c.execute(
                        "INSERT INTO membership_observation VALUES(?,?,?,?,?)",
                        (run, collections[m.id], aid, pos, m.watch_url),
                    )
        n = c.execute(
            "SELECT count(*) FROM asset_observation WHERE run_id=?", (run,)
        ).fetchone()[0]
        required = {
            r[0]
            for r in c.execute(
                "SELECT collection_slug FROM show_required_collection WHERE show_id=?",
                (sh["show_id"],),
            )
        }
        got = {
            r[0]
            for r in c.execute(
                "SELECT slug FROM collection_observation WHERE run_id=?", (run,)
            )
        }
        minimum = sh["expected_minimum_assets"]
        ok = (
            not conflicts
            and n > 0
            and (minimum is None or n >= minimum)
            and required <= got
        )
        errs = []
        if conflicts:
            errs.append(f"{conflicts} identity conflict(s)")
        if minimum is not None and n < minimum:
            errs.append(f"asset count {n} below minimum {minimum}")
        if required - got:
            errs.append(
                "missing required collections: " + ",".join(sorted(required - got))
            )
        c.execute(
            "UPDATE catalog_run SET catalog_complete=?,error_message=? WHERE run_id=?",
            (int(ok), "; ".join(errs) or None, run),
        )
        return run


def retrieve(sh, s):
    if parser.httpx is None:
        raise RuntimeError("Live retrieval requires httpx")
    with parser.httpx.Client(
        headers={"User-Agent": s.user_agent, "Accept": "text/html"},
        follow_redirects=True,
        timeout=s.timeout_seconds,
    ) as client:
        r = client.get(sh["catalog_url"])
        r.raise_for_status()
        return (
            r.text,
            r.status_code,
            r.headers.get("etag"),
            r.headers.get("last-modified"),
        )


def cli(settings, argv):
    ap = argparse.ArgumentParser(description="Maintain the PBS KIDS SQLite catalog.")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--snapshot-root", type=Path, default=None)
    ap.add_argument("--log-level", choices=sorted(logging._nameToLevel), default=None)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    p = sub.add_parser("add-show")
    p.add_argument("key")
    p.add_argument("url")
    p.add_argument("--minimum-assets", type=int)
    p.add_argument("--frequency")
    p.add_argument("--disabled", action="store_true")
    p = sub.add_parser("require-collection")
    p.add_argument("key")
    p.add_argument("collection_slug")
    p = sub.add_parser("update")
    p.add_argument("key")
    p.add_argument("--html", type=Path)
    p.add_argument("--observed-at")
    sub.add_parser("list-shows")
    p = sub.add_parser("runs")
    p.add_argument("--key")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("current")
    p.add_argument("key")
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None):
    try:
        s, rest = load_settings(argv)
        a = cli(s, rest)
        logging.basicConfig(
            level=getattr(logging, a.log_level or s.log_level),
            format="%(levelname)s: %(message)s",
        )
        db = a.db.expanduser() if a.db else s.database
        sr = a.snapshot_root.expanduser() if a.snapshot_root else s.snapshot_root
        init_db(db, s)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    try:
        if a.command == "init":
            print(f"Initialized {db}")
        elif a.command == "add-show":
            with connect(db, s) as c:
                c.execute(
                    """INSERT INTO show(canonical_key,catalog_url,enabled,expected_minimum_assets,retrieval_frequency) VALUES(?,?,?,?,?) ON CONFLICT(canonical_key) DO UPDATE SET catalog_url=excluded.catalog_url,enabled=excluded.enabled,expected_minimum_assets=excluded.expected_minimum_assets,retrieval_frequency=excluded.retrieval_frequency""",
                    (a.key, a.url, int(not a.disabled), a.minimum_assets, a.frequency),
                )
                print(f"Configured {a.key}")
        elif a.command == "require-collection":
            with connect(db, s) as c:
                sh = show(c, a.key)
                c.execute(
                    "INSERT OR IGNORE INTO show_required_collection VALUES(?,?)",
                    (sh["show_id"], a.collection_slug),
                )
                print("Requirement saved")
        elif a.command == "update":
            stamp = a.observed_at or now()
            with connect(db, s) as c:
                sh = show(c, a.key)
                if not sh["enabled"]:
                    raise ValueError(f"Show {a.key} is disabled")
                if a.html:
                    html = a.html.read_text()
                    status = etag = modified = None
                else:
                    html, status, etag, modified = retrieve(sh, s)
                run = ingest(
                    c,
                    sh,
                    html,
                    stamp,
                    snapshot(sr, a.key, stamp, html),
                    status,
                    etag,
                    modified,
                )
                print(
                    json.dumps(
                        dict(
                            c.execute(
                                "SELECT * FROM catalog_run WHERE run_id=?", (run,)
                            ).fetchone()
                        ),
                        sort_keys=True,
                    )
                )
        elif a.command == "list-shows":
            with connect(db, s) as c:
                for r in c.execute("SELECT * FROM show ORDER BY canonical_key"):
                    print(json.dumps(dict(r), sort_keys=True))
        elif a.command == "runs":
            with connect(db, s) as c:
                q = "SELECT cr.*,s.canonical_key FROM catalog_run cr JOIN show s USING(show_id)"
                params = []
                if a.key:
                    q += " WHERE s.canonical_key=?"
                    params.append(a.key)
                q += " ORDER BY observed_at DESC,run_id DESC LIMIT ?"
                params.append(a.limit)
                for r in c.execute(q, params):
                    print(json.dumps(dict(r), sort_keys=True))
        elif a.command == "current":
            with connect(db, s) as c:
                for r in c.execute(
                    "SELECT v.* FROM current_catalog_by_show v JOIN show s USING(show_id) WHERE s.canonical_key=? ORDER BY title",
                    (a.key,),
                ):
                    print(json.dumps(dict(r), sort_keys=True))
        return 0
    except Exception as e:
        LOG.error("%s", e)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
