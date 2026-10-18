PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migration
(
    version       INTEGER PRIMARY KEY,
    applied_at    TEXT NOT NULL,
    description   TEXT NOT NULL,
    checksum      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS show
(
    show_id                   INTEGER PRIMARY KEY,
    canonical_key             TEXT NOT NULL UNIQUE,
    catalog_url               TEXT NOT NULL UNIQUE,
    enabled                   INTEGER NOT NULL DEFAULT 1
                              CHECK (enabled IN (0, 1)),
    expected_minimum_assets   INTEGER
                              CHECK
                              (
                                  expected_minimum_assets IS NULL
                                  OR expected_minimum_assets >= 0
                              ),
    retrieval_frequency       TEXT,
    created_at                TEXT NOT NULL DEFAULT
                              (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS show_required_collection
(
    show_id           INTEGER NOT NULL,
    collection_slug   TEXT NOT NULL,

    PRIMARY KEY (show_id, collection_slug),

    FOREIGN KEY (show_id)
        REFERENCES show(show_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS catalog_run
(
    run_id                   INTEGER PRIMARY KEY,
    show_id                  INTEGER NOT NULL,
    observed_at              TEXT NOT NULL,
    source_url               TEXT NOT NULL,
    http_status              INTEGER,
    response_sha256          TEXT,
    response_etag            TEXT,
    response_last_modified   TEXT,
    snapshot_relative_path   TEXT,
    next_build_id            TEXT,
    parser_version           TEXT NOT NULL,
    retrieval_succeeded      INTEGER NOT NULL
                             CHECK (retrieval_succeeded IN (0, 1)),
    parse_succeeded          INTEGER NOT NULL
                             CHECK (parse_succeeded IN (0, 1)),
    catalog_complete         INTEGER NOT NULL
                             CHECK (catalog_complete IN (0, 1)),
    error_message            TEXT,
    created_at               TEXT NOT NULL DEFAULT
                             (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    FOREIGN KEY (show_id)
        REFERENCES show(show_id)
);

CREATE TABLE IF NOT EXISTS asset
(
    asset_id             INTEGER PRIMARY KEY,
    media_manager_guid   TEXT UNIQUE,
    pbs_kids_cms_id      TEXT UNIQUE,
    legacy_tp_media_id   TEXT
);

CREATE TABLE IF NOT EXISTS collection
(
    collection_id       INTEGER PRIMARY KEY,
    show_id             INTEGER NOT NULL,
    pbs_collection_id   TEXT NOT NULL UNIQUE,

    FOREIGN KEY (show_id)
        REFERENCES show(show_id)
);

CREATE TABLE IF NOT EXISTS show_observation
(
    run_id          INTEGER NOT NULL,
    show_id         INTEGER NOT NULL,
    source_slug     TEXT,
    source_title    TEXT NOT NULL,
    metadata_hash   TEXT NOT NULL,

    PRIMARY KEY (run_id, show_id),

    FOREIGN KEY (run_id)
        REFERENCES catalog_run(run_id),

    FOREIGN KEY (show_id)
        REFERENCES show(show_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS asset_observation
(
    run_id                 INTEGER NOT NULL,
    asset_id               INTEGER NOT NULL,
    slug                   TEXT NOT NULL,
    title                  TEXT NOT NULL,
    video_type             TEXT NOT NULL,
    description_short      TEXT,
    duration_seconds       INTEGER,
    source_premiered_at    TEXT,
    metadata_hash          TEXT NOT NULL,

    PRIMARY KEY (run_id, asset_id),

    FOREIGN KEY (run_id)
        REFERENCES catalog_run(run_id),

    FOREIGN KEY (asset_id)
        REFERENCES asset(asset_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS collection_observation
(
    run_id          INTEGER NOT NULL,
    collection_id   INTEGER NOT NULL,
    slug            TEXT NOT NULL,
    title           TEXT,
    heading         TEXT,
    metadata_hash   TEXT NOT NULL,

    PRIMARY KEY (run_id, collection_id),

    FOREIGN KEY (run_id)
        REFERENCES catalog_run(run_id),

    FOREIGN KEY (collection_id)
        REFERENCES collection(collection_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS membership_observation
(
    run_id            INTEGER NOT NULL,
    collection_id     INTEGER NOT NULL,
    asset_id          INTEGER NOT NULL,
    ordinal_position  INTEGER,
    watch_url         TEXT NOT NULL,

    PRIMARY KEY (run_id, collection_id, asset_id),

    FOREIGN KEY (run_id)
        REFERENCES catalog_run(run_id),

    FOREIGN KEY (collection_id)
        REFERENCES collection(collection_id),

    FOREIGN KEY (asset_id)
        REFERENCES asset(asset_id)
) WITHOUT ROWID;
