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
    enabled                   INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    expected_minimum_assets   INTEGER CHECK (expected_minimum_assets IS NULL OR expected_minimum_assets >= 0),
    retrieval_frequency       TEXT,
    created_at                TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS show_required_collection
(
    show_id          INTEGER NOT NULL,
    collection_slug  TEXT NOT NULL,
    PRIMARY KEY (show_id, collection_slug),
    FOREIGN KEY (show_id) REFERENCES show(show_id)
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
    retrieval_succeeded      INTEGER NOT NULL CHECK (retrieval_succeeded IN (0, 1)),
    parse_succeeded          INTEGER NOT NULL CHECK (parse_succeeded IN (0, 1)),
    catalog_complete         INTEGER NOT NULL CHECK (catalog_complete IN (0, 1)),
    error_message            TEXT,
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (show_id) REFERENCES show(show_id),
    CHECK (catalog_complete = 0 OR (retrieval_succeeded = 1 AND parse_succeeded = 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_catalog_run_source_observation_response
ON catalog_run(show_id, source_url, observed_at, response_sha256)
WHERE response_sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_catalog_run_show_observed
ON catalog_run(show_id, observed_at, run_id);

CREATE TABLE IF NOT EXISTS asset
(
    asset_id             INTEGER PRIMARY KEY,
    media_manager_guid   TEXT,
    pbs_kids_cms_id      TEXT,
    legacy_tp_media_id   TEXT,
    CHECK (media_manager_guid IS NOT NULL OR pbs_kids_cms_id IS NOT NULL OR legacy_tp_media_id IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_asset_media_manager_guid
ON asset(media_manager_guid) WHERE media_manager_guid IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_asset_pbs_kids_cms_id
ON asset(pbs_kids_cms_id) WHERE pbs_kids_cms_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_asset_legacy_tp_media_id
ON asset(legacy_tp_media_id) WHERE legacy_tp_media_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS collection
(
    collection_id       INTEGER PRIMARY KEY,
    show_id             INTEGER NOT NULL,
    pbs_collection_id   TEXT NOT NULL UNIQUE,
    FOREIGN KEY (show_id) REFERENCES show(show_id),
    UNIQUE (show_id, pbs_collection_id)
);

CREATE TABLE IF NOT EXISTS show_observation
(
    run_id          INTEGER NOT NULL,
    show_id         INTEGER NOT NULL,
    source_slug     TEXT,
    source_title    TEXT NOT NULL,
    nola_root       TEXT,
    metadata_hash   TEXT NOT NULL,
    PRIMARY KEY (run_id, show_id),
    FOREIGN KEY (run_id) REFERENCES catalog_run(run_id),
    FOREIGN KEY (show_id) REFERENCES show(show_id)
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
    FOREIGN KEY (run_id) REFERENCES catalog_run(run_id),
    FOREIGN KEY (asset_id) REFERENCES asset(asset_id),
    CHECK (duration_seconds IS NULL OR duration_seconds >= 0)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_asset_observation_asset_run
ON asset_observation(asset_id, run_id);

CREATE INDEX IF NOT EXISTS ix_asset_observation_video_type_run
ON asset_observation(video_type, run_id);

CREATE TABLE IF NOT EXISTS asset_show_observation
(
    run_id      INTEGER NOT NULL,
    show_id     INTEGER NOT NULL,
    asset_id    INTEGER NOT NULL,
    PRIMARY KEY (run_id, show_id, asset_id),
    FOREIGN KEY (run_id) REFERENCES catalog_run(run_id),
    FOREIGN KEY (show_id) REFERENCES show(show_id),
    FOREIGN KEY (asset_id) REFERENCES asset(asset_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_asset_show_observation_asset_run
ON asset_show_observation(asset_id, run_id);

CREATE TABLE IF NOT EXISTS collection_observation
(
    run_id          INTEGER NOT NULL,
    collection_id   INTEGER NOT NULL,
    slug            TEXT NOT NULL,
    title           TEXT,
    heading         TEXT,
    metadata_hash   TEXT NOT NULL,
    PRIMARY KEY (run_id, collection_id),
    FOREIGN KEY (run_id) REFERENCES catalog_run(run_id),
    FOREIGN KEY (collection_id) REFERENCES collection(collection_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_collection_observation_slug_run
ON collection_observation(slug, run_id);

CREATE TABLE IF NOT EXISTS membership_observation
(
    run_id            INTEGER NOT NULL,
    collection_id     INTEGER NOT NULL,
    asset_id          INTEGER NOT NULL,
    ordinal_position  INTEGER,
    watch_url         TEXT NOT NULL,
    PRIMARY KEY (run_id, collection_id, asset_id),
    FOREIGN KEY (run_id) REFERENCES catalog_run(run_id),
    FOREIGN KEY (collection_id) REFERENCES collection(collection_id),
    FOREIGN KEY (asset_id) REFERENCES asset(asset_id),
    CHECK (ordinal_position IS NULL OR ordinal_position >= 0)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_membership_observation_asset_run
ON membership_observation(asset_id, run_id);

CREATE INDEX IF NOT EXISTS ix_membership_observation_collection_run
ON membership_observation(collection_id, run_id);

CREATE TABLE IF NOT EXISTS asset_image_observation
(
    run_id       INTEGER NOT NULL,
    asset_id     INTEGER NOT NULL,
    profile      TEXT NOT NULL,
    image_url    TEXT NOT NULL,
    image_sha256 TEXT,
    PRIMARY KEY (run_id, asset_id, profile, image_url),
    FOREIGN KEY (run_id) REFERENCES catalog_run(run_id),
    FOREIGN KEY (asset_id) REFERENCES asset(asset_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS identity_conflict
(
    conflict_id          INTEGER PRIMARY KEY,
    run_id               INTEGER NOT NULL,
    media_manager_guid   TEXT,
    pbs_kids_cms_id      TEXT,
    legacy_tp_media_id   TEXT,
    matched_asset_ids    TEXT NOT NULL,
    source_record_json   TEXT NOT NULL,
    detected_at          TEXT NOT NULL,
    resolution_status    TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (resolution_status IN ('unresolved', 'resolved_merge', 'resolved_separate', 'ignored')),
    resolution_notes     TEXT,
    FOREIGN KEY (run_id) REFERENCES catalog_run(run_id)
);

CREATE VIEW IF NOT EXISTS latest_complete_catalog_run_by_show AS
WITH ranked AS
(
    SELECT cr.*,
           ROW_NUMBER() OVER (PARTITION BY show_id ORDER BY observed_at DESC, run_id DESC) AS row_number
    FROM catalog_run AS cr
    WHERE retrieval_succeeded = 1 AND parse_succeeded = 1 AND catalog_complete = 1
)
SELECT * FROM ranked WHERE row_number = 1;

CREATE VIEW IF NOT EXISTS current_catalog_by_show AS
SELECT cr.show_id, cr.observed_at, ao.*, a.media_manager_guid,
       a.pbs_kids_cms_id, a.legacy_tp_media_id
FROM latest_complete_catalog_run_by_show AS cr
JOIN asset_observation AS ao ON ao.run_id = cr.run_id
JOIN asset AS a ON a.asset_id = ao.asset_id;

CREATE VIEW IF NOT EXISTS asset_observation_summary AS
SELECT aso.show_id, ao.asset_id,
       MIN(cr.observed_at) AS first_observed_at,
       MAX(cr.observed_at) AS last_observed_at,
       COUNT(*) AS observed_run_count
FROM asset_observation AS ao
JOIN asset_show_observation AS aso ON aso.run_id = ao.run_id AND aso.asset_id = ao.asset_id
JOIN catalog_run AS cr ON cr.run_id = ao.run_id
WHERE cr.retrieval_succeeded = 1 AND cr.parse_succeeded = 1 AND cr.catalog_complete = 1
GROUP BY aso.show_id, ao.asset_id;
