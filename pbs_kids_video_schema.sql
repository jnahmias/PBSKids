PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS watch_page_observation
(
    watch_observation_id    INTEGER PRIMARY KEY,
    asset_id               INTEGER NOT NULL,
    observed_at            TEXT NOT NULL,
    watch_url              TEXT NOT NULL,
    http_status            INTEGER,
    response_sha256        TEXT,
    source_expires_at      TEXT,
    drm_enabled            INTEGER
                           CHECK (drm_enabled IN (0, 1)),
    title                  TEXT,
    description_long       TEXT,
    duration_seconds       INTEGER,
    retrieval_succeeded    INTEGER NOT NULL
                           CHECK (retrieval_succeeded IN (0, 1)),
    parse_succeeded        INTEGER NOT NULL
                           CHECK (parse_succeeded IN (0, 1)),
    error_message          TEXT,

    FOREIGN KEY (asset_id)
        REFERENCES asset(asset_id)
);

CREATE INDEX IF NOT EXISTS ix_watch_page_observation_asset_time
ON watch_page_observation(asset_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS media_profile_observation
(
    watch_observation_id   INTEGER NOT NULL,
    profile                TEXT NOT NULL,
    redirect_url           TEXT NOT NULL,
    fairplay_license_url   TEXT,
    widevine_license_url   TEXT,

    PRIMARY KEY (watch_observation_id, profile, redirect_url),

    FOREIGN KEY (watch_observation_id)
        REFERENCES watch_page_observation(watch_observation_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS download_attempt
(
    download_attempt_id      INTEGER PRIMARY KEY,
    asset_id                 INTEGER NOT NULL,
    watch_observation_id     INTEGER,
    started_at               TEXT NOT NULL,
    completed_at             TEXT,
    selected_profile         TEXT,
    source_url               TEXT,
    target_path              TEXT NOT NULL,
    temporary_path           TEXT,
    status                   TEXT NOT NULL
                             CHECK
                             (
                                 status IN
                                 (
                                     'started',
                                     'complete',
                                     'failed',
                                     'skipped'
                                 )
                             ),
    downloader_exit_code     INTEGER,
    error_message            TEXT,

    FOREIGN KEY (asset_id)
        REFERENCES asset(asset_id),

    FOREIGN KEY (watch_observation_id)
        REFERENCES watch_page_observation(watch_observation_id)
);

CREATE INDEX IF NOT EXISTS ix_download_attempt_asset_started
ON download_attempt(asset_id, started_at DESC);

CREATE TABLE IF NOT EXISTS download_artifact
(
    download_artifact_id    INTEGER PRIMARY KEY,
    asset_id                INTEGER NOT NULL,
    download_attempt_id     INTEGER NOT NULL UNIQUE,
    relative_path           TEXT NOT NULL UNIQUE,
    file_size_bytes         INTEGER NOT NULL,
    sha256                  TEXT NOT NULL,
    container_format        TEXT,
    duration_seconds        REAL,
    video_width             INTEGER,
    video_height            INTEGER,
    verified_at             TEXT NOT NULL,
    verification_status     TEXT NOT NULL
                            CHECK
                            (
                                verification_status IN
                                (
                                    'verified',
                                    'duration_warning',
                                    'unverified'
                                )
                            ),

    FOREIGN KEY (asset_id)
        REFERENCES asset(asset_id),

    FOREIGN KEY (download_attempt_id)
        REFERENCES download_attempt(download_attempt_id)
);

CREATE INDEX IF NOT EXISTS ix_download_artifact_asset
ON download_artifact(asset_id);
