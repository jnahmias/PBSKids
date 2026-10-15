# Odd Squad Catalog Database Design

**Document status:** Proposed baseline design  
**Database platform:** SQLite 3  
**Application phase:** Catalog retrieval and observation history  
**Future phase:** Watch-page enrichment, media profiles, captions, and download state  

## 1. Executive Summary

This document defines the SQLite database design for an application that periodically retrieves the PBS KIDS Odd Squad catalog, parses the embedded Next.js JSON, and records what assets and collection memberships were observed during each successful catalog run.

The design uses an **append-only observation model with stable internal identities**. It does not use a bitemporal model because the source generally establishes only that a fact was observed at a crawl time. It does not establish the exact real-world interval during which titles, descriptions, artwork, or collection membership were valid.

The principal design decisions are:

1. Governance rules define what constitutes a valid observation before data is interpreted as presence or absence.
2. Stable registry tables identify assets and collections independently of their observed attributes.
3. Observation tables preserve one normalized snapshot per successful run.
4. Collection membership is recorded independently from asset metadata.
5. Repeated appearances of the same collection or asset on a page are deduplicated. Page placement is not retained.
6. Absence is inferred only from a successful, complete catalog run.
7. Repeated rotations into and out of a collection are derived from the sequence of observations rather than stored as a single presence interval.
8. Source-provided timestamps, such as premiere or expiry dates, are stored under names that preserve their source meaning. They are not treated as generic validity boundaries.
9. The schema reserves extension points for watch-page metadata, media profiles, captions, and download verification.

This design favors traceability, normalization, deterministic ingestion, and simple historical reconstruction. Expected catalog size is small enough that storing complete observations for each run is operationally practical.

---

## 2. Purpose

### 2.1 Objectives

The database shall support the following capabilities:

- Record every catalog retrieval attempt and its outcome.
- Preserve the normalized state returned by each successful, complete catalog retrieval.
- Identify the same PBS video across multiple runs.
- Track changes to titles, descriptions, duration, classification, imagery, and collection membership.
- Determine what the application observed during any prior successful run.
- Detect assets added to or removed from the overall catalog.
- Detect assets rotating into or out of individual collections.
- Support idempotent reprocessing of a previously stored source snapshot.
- Provide a stable foundation for later watch-page, caption, media, and download features.

### 2.2 Non-objectives

The initial design does not attempt to:

- Establish exact real-world validity periods for catalog facts.
- Infer the precise time at which PBS added or removed an item between crawls.
- Preserve masthead, body-module, or other page-placement details.
- Treat the catalog as a complete historical inventory of all Odd Squad episodes.
- Match PBS assets to external episode databases.
- Circumvent DRM, authentication, geography, expiry, or other access controls.
- Store or manage media files in SQLite.

### 2.3 Governance precedence

Design and implementation decisions shall be evaluated in this order:

1. **Governance:** provenance, completeness, data interpretation, conflict handling, and retention.
2. **Architecture:** stable identities, append-only observations, separation of concerns, and extensibility.
3. **Implementation:** SQLite DDL, transactions, indexes, ingestion code, and operational commands.

---

## 3. Business Definitions

### 3.1 Catalog run

A **catalog run** is one attempt to retrieve and parse a defined catalog source URL. A run records retrieval status, parse status, completeness status, parser version, and response provenance.

A run may be:

- successful and complete;
- successful but incomplete;
- retrieved but not parsed;
- or not retrieved.

Only successful, complete runs may be used to infer absence.

### 3.2 Observation time

**Observation time** is the UTC timestamp assigned to a catalog run. It represents when the source response was obtained for interpretation.

Observation time does not mean that the observed catalog state began at that moment.

### 3.3 Asset

An **asset** is the application's stable internal representation of a logical PBS video item. The asset is assigned an SQLite integer key and may be associated with multiple PBS identifiers.

The initial source exposes three identifiers:

- **Media Manager GUID:** UUID associated with the underlying PBS media asset.
- **PBS KIDS CMS ID:** identifier used by the PBS KIDS content-management and watch-page routes.
- **Legacy TP media ID:** identifier retained from an older media platform.

The database's relational primary key is `asset_id`. No external identifier is used as a foreign key.

### 3.4 Asset observation

An **asset observation** states:

> During catalog run R, asset A was returned with the recorded attributes.

The presence of an asset-observation row establishes positive observation in the overall catalog for that run.

### 3.5 Collection

A **collection** is a PBS catalog grouping such as `odd-squad-full-episodes`, `odd-squad-movies`, or `odd-squad-asl-collection`.

The stable registry uses the PBS collection ID. Mutable values such as slug, title, and heading are stored in collection observations.

### 3.6 Membership observation

A **membership observation** states:

> During catalog run R, asset A was returned as a member of collection C.

Repeated rendering of the same membership in masthead and body modules is collapsed into one row. Page placement is outside the project scope.

### 3.7 Presence and absence

Presence is explicit. Absence is inferred.

- An asset-observation row means the asset was observed in the overall catalog.
- A membership-observation row means the asset was observed in a specific collection.
- A missing row may be interpreted as absence only when the associated run is marked successful and complete.
- A failed or incomplete run provides no evidence of absence.

### 3.8 Observation period

An **observation period** is a derived sequence of consecutive complete runs in which an asset or collection membership was present.

Observation periods may recur. They are not exact source-validity periods.

Example:

```text
Run 1: absent
Run 2: present
Run 3: present
Run 4: absent
Run 5: present
```

This sequence contains two observed-presence periods: runs 2 through 3, and run 5 onward.

### 3.9 Source temporal attributes

Source-provided temporal values are retained with explicit names:

- `source_premiered_at`
- `source_created_at`
- `source_expires_at`

These fields describe source assertions. They do not define the validity of every attribute in an observation.

### 3.10 Complete catalog

A run is **catalog complete** only when the ingestion process has evidence that all expected catalog content was retrieved and parsed.

For the current HTML-based source, completeness requires at minimum:

- successful HTTP retrieval;
- exactly one parseable `script#__NEXT_DATA__` payload;
- expected page-data structure;
- at least one recognized video collection;
- no fatal parser or schema errors;
- completion of all pagination or dependent retrieval required by the configured run scope.

---

## 4. Assumptions

1. The PBS KIDS catalog HTML contains a Next.js `__NEXT_DATA__` payload that represents the catalog state returned for that request.
2. Asset GUIDs observed in catalog entries correspond to the Media Manager asset IDs presented on watch pages.
3. PBS KIDS CMS IDs are unique within the PBS KIDS source namespace.
4. Legacy TP media IDs are useful alternate identifiers, but uniqueness will not be enforced until empirical validation is complete.
5. The catalog is small enough to store a full normalized observation for every asset on every run.
6. Collection rotation is expected, so an asset may have multiple observed-presence periods and multiple observed-absence periods.
7. A single catalog run uses one UTC `observed_at` value for all rows inserted by that run.
8. Raw response files may be retained outside SQLite; SQLite stores their hashes and optional relative paths.
9. SQLite foreign-key enforcement will be enabled for every connection.
10. The initial process has one writer. Concurrent read access is permitted through WAL mode.
11. Phase 1 records catalog metadata only. Phase 2 may add watch-page, availability, media-profile, caption, and download observations.

---

## 5. Architecture

### 5.1 Logical data flow

```text
PBS KIDS catalog URL
        |
        v
HTTP retrieval
        |
        v
Raw HTML hash and optional snapshot
        |
        v
Extract Next.js __NEXT_DATA__
        |
        v
Normalize assets, collections, and memberships
        |
        v
Resolve stable identities and detect conflicts
        |
        v
Single SQLite transaction
        |
        +--> catalog_run
        +--> asset / collection registries
        +--> asset_observation
        +--> collection_observation
        +--> membership_observation
```

### 5.2 Architectural principles

#### Stable identity separated from mutable observations

Registry tables contain source identifiers. Mutable descriptive attributes are stored in run-scoped observation tables.

#### Append-only observations

Completed observation rows are not updated to represent a later source state. Each later run adds new rows.

Permitted updates are limited to operational completion of the current run record, conflict resolution records, and controlled maintenance metadata.

#### Scope-aware absence

Absence is interpreted only within the scope of a complete run. A catalog run does not establish caption or media-profile absence unless those scopes were explicitly checked.

#### Deterministic normalization

Hashes are calculated from canonical normalized fields, not raw JSON object order or unrelated presentation metadata.

#### Atomic run ingestion

All registry and observation rows for one successful parse are committed in one transaction. A partially committed catalog state is not allowed.

### 5.3 Identity strategy

SQLite assigns an internal integer `asset_id`. External identifiers are alternate identities used for reconciliation.

Resolution order:

1. Match Media Manager GUID when present.
2. Match PBS KIDS CMS ID when present.
3. Match legacy TP media ID when present.
4. Confirm that all matches resolve to the same internal asset.
5. Create a new asset when no identifier matches.
6. Attach a previously unknown alternate identifier only after uniqueness checks.
7. Record a conflict and reject automatic merging when identifiers point to different assets.

The Media Manager GUID is the strongest current external identity because it is associated with the underlying Media Manager asset. It is not the SQLite primary key.

### 5.4 Temporal model

The model records observation time, not generic valid time.

Authoritative temporal evidence consists of:

- run `observed_at`;
- the presence or absence of run-scoped observation rows;
- separately named source timestamps.

Repeated periods are derived from ordered complete runs. No `valid_from` or `valid_to` columns are required in phase 1.

### 5.5 Provenance

Each run records:

- source URL;
- observation timestamp;
- HTTP status;
- response hash;
- optional ETag and Last-Modified values;
- parser version;
- retrieval, parsing, and completeness results;
- optional snapshot path;
- error details.

This evidence supports replay, debugging, and parser validation.

---

## 6. Data Model

### 6.1 Relationship summary

```text
catalog_run 1 --- * asset_observation * --- 1 asset
catalog_run 1 --- * collection_observation * --- 1 collection
catalog_run 1 --- * membership_observation * --- 1 asset
collection  1 --- * membership_observation
catalog_run 1 --- * identity_conflict
```

### 6.2 `catalog_run`

Stores retrieval and parsing provenance.

```sql
CREATE TABLE catalog_run
(
    run_id                   INTEGER PRIMARY KEY,
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

    CHECK
    (
        catalog_complete = 0
        OR (retrieval_succeeded = 1 AND parse_succeeded = 1)
    )
);
```

Recommended additional rule in ingestion code: only one successful run may be loaded for a given `source_url`, `observed_at`, and `response_sha256` combination.

```sql
CREATE UNIQUE INDEX ux_catalog_run_source_observation_response
ON catalog_run(source_url, observed_at, response_sha256)
WHERE response_sha256 IS NOT NULL;
```

### 6.3 `asset`

Stores stable reconciliation identities only.

```sql
CREATE TABLE asset
(
    asset_id             INTEGER PRIMARY KEY,
    media_manager_guid   TEXT,
    pbs_kids_cms_id      TEXT,
    legacy_tp_media_id   TEXT,

    CHECK
    (
        media_manager_guid IS NOT NULL
        OR pbs_kids_cms_id IS NOT NULL
        OR legacy_tp_media_id IS NOT NULL
    )
);

CREATE UNIQUE INDEX ux_asset_media_manager_guid
ON asset(media_manager_guid)
WHERE media_manager_guid IS NOT NULL;

CREATE UNIQUE INDEX ux_asset_pbs_kids_cms_id
ON asset(pbs_kids_cms_id)
WHERE pbs_kids_cms_id IS NOT NULL;

CREATE INDEX ix_asset_legacy_tp_media_id
ON asset(legacy_tp_media_id)
WHERE legacy_tp_media_id IS NOT NULL;
```

The legacy ID remains non-unique until observed data supports a stronger constraint.

### 6.4 `asset_observation`

Stores normalized asset state for one run.

```sql
CREATE TABLE asset_observation
(
    run_id                 INTEGER NOT NULL,
    asset_id               INTEGER NOT NULL,
    slug                   TEXT NOT NULL,
    title                  TEXT NOT NULL,
    video_type             TEXT NOT NULL,
    show_slug              TEXT,
    show_title             TEXT,
    description_short      TEXT,
    duration_seconds       INTEGER,
    source_premiered_at    TEXT,
    metadata_hash          TEXT NOT NULL,

    PRIMARY KEY (run_id, asset_id),

    FOREIGN KEY (run_id)
        REFERENCES catalog_run(run_id),

    FOREIGN KEY (asset_id)
        REFERENCES asset(asset_id),

    CHECK (duration_seconds IS NULL OR duration_seconds >= 0)
) WITHOUT ROWID;

CREATE INDEX ix_asset_observation_asset_run
ON asset_observation(asset_id, run_id);

CREATE INDEX ix_asset_observation_video_type_run
ON asset_observation(video_type, run_id);
```

Recommended canonical metadata-hash input:

```json
{
  "slug": "best-seats-in-the-house-agent-obfusco",
  "title": "Best Seats in the House/Agent Obfusco",
  "video_type": "fullEpisode",
  "show_slug": "odd-squad",
  "show_title": "Odd Squad",
  "description_short": "...",
  "duration_seconds": 1571,
  "source_premiered_at": "2014-12-09T00:00:00Z"
}
```

### 6.5 `collection`

Stores stable PBS collection identity.

```sql
CREATE TABLE collection
(
    collection_id       INTEGER PRIMARY KEY,
    pbs_collection_id   TEXT NOT NULL UNIQUE
);
```

### 6.6 `collection_observation`

Stores collection values observed during one run.

```sql
CREATE TABLE collection_observation
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

CREATE INDEX ix_collection_observation_slug_run
ON collection_observation(slug, run_id);
```

### 6.7 `membership_observation`

Stores one positive collection-membership observation per run.

```sql
CREATE TABLE membership_observation
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
        REFERENCES asset(asset_id),

    CHECK (ordinal_position IS NULL OR ordinal_position >= 0)
) WITHOUT ROWID;

CREATE INDEX ix_membership_observation_asset_run
ON membership_observation(asset_id, run_id);

CREATE INDEX ix_membership_observation_collection_run
ON membership_observation(collection_id, run_id);
```

### 6.8 `asset_image_observation`

Images may change independently and may have multiple profiles. They should not be stored as repeated columns on the asset observation.

```sql
CREATE TABLE asset_image_observation
(
    run_id       INTEGER NOT NULL,
    asset_id     INTEGER NOT NULL,
    profile      TEXT NOT NULL,
    image_url    TEXT NOT NULL,
    image_sha256 TEXT,

    PRIMARY KEY (run_id, asset_id, profile, image_url),

    FOREIGN KEY (run_id)
        REFERENCES catalog_run(run_id),

    FOREIGN KEY (asset_id)
        REFERENCES asset(asset_id)
) WITHOUT ROWID;
```

`image_sha256` is optional until the application downloads images.

### 6.9 `identity_conflict`

Records contradictory identifier mappings requiring review.

```sql
CREATE TABLE identity_conflict
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
        CHECK (resolution_status IN
            ('unresolved', 'resolved_merge', 'resolved_separate', 'ignored')),
    resolution_notes     TEXT,

    FOREIGN KEY (run_id)
        REFERENCES catalog_run(run_id)
);
```

Automatic ingestion shall not merge assets when populated identifiers resolve to different asset rows.

---

## 7. Derived Views and Queries

### 7.1 Latest complete run

```sql
CREATE VIEW latest_complete_catalog_run AS
SELECT *
FROM catalog_run
WHERE run_id =
(
    SELECT run_id
    FROM catalog_run
    WHERE retrieval_succeeded = 1
      AND parse_succeeded = 1
      AND catalog_complete = 1
    ORDER BY observed_at DESC, run_id DESC
    LIMIT 1
);
```

### 7.2 Current observed catalog

```sql
CREATE VIEW current_catalog AS
SELECT
    ao.*,
    a.media_manager_guid,
    a.pbs_kids_cms_id,
    a.legacy_tp_media_id,
    cr.observed_at
FROM latest_complete_catalog_run AS cr
JOIN asset_observation AS ao
  ON ao.run_id = cr.run_id
JOIN asset AS a
  ON a.asset_id = ao.asset_id;
```

### 7.3 Asset observation summary

This view derives, rather than stores, first and last observation times.

```sql
CREATE VIEW asset_observation_summary AS
SELECT
    ao.asset_id,
    MIN(cr.observed_at) AS first_observed_at,
    MAX(cr.observed_at) AS last_observed_at,
    COUNT(*) AS observed_run_count
FROM asset_observation AS ao
JOIN catalog_run AS cr
  ON cr.run_id = ao.run_id
WHERE cr.retrieval_succeeded = 1
  AND cr.parse_succeeded = 1
  AND cr.catalog_complete = 1
GROUP BY ao.asset_id;
```

### 7.4 Metadata changes

```sql
WITH history AS
(
    SELECT
        ao.asset_id,
        ao.run_id,
        cr.observed_at,
        ao.metadata_hash,
        LAG(ao.metadata_hash) OVER
        (
            PARTITION BY ao.asset_id
            ORDER BY cr.observed_at, ao.run_id
        ) AS previous_metadata_hash
    FROM asset_observation AS ao
    JOIN catalog_run AS cr
      ON cr.run_id = ao.run_id
    WHERE cr.catalog_complete = 1
)
SELECT *
FROM history
WHERE previous_metadata_hash IS NULL
   OR metadata_hash <> previous_metadata_hash;
```

### 7.5 Repeated collection-presence periods

The following query derives islands of consecutive complete runs in which an asset was observed in a collection.

```sql
WITH complete_runs AS
(
    SELECT
        run_id,
        observed_at,
        ROW_NUMBER() OVER
        (
            ORDER BY observed_at, run_id
        ) AS run_sequence
    FROM catalog_run
    WHERE retrieval_succeeded = 1
      AND parse_succeeded = 1
      AND catalog_complete = 1
),
present_memberships AS
(
    SELECT
        mo.collection_id,
        mo.asset_id,
        cr.run_id,
        cr.observed_at,
        cr.run_sequence,
        ROW_NUMBER() OVER
        (
            PARTITION BY mo.collection_id, mo.asset_id
            ORDER BY cr.run_sequence
        ) AS present_sequence
    FROM membership_observation AS mo
    JOIN complete_runs AS cr
      ON cr.run_id = mo.run_id
),
islands AS
(
    SELECT
        collection_id,
        asset_id,
        run_id,
        observed_at,
        run_sequence - present_sequence AS island_id
    FROM present_memberships
)
SELECT
    collection_id,
    asset_id,
    MIN(observed_at) AS first_observed_present_at,
    MAX(observed_at) AS last_observed_present_at,
    COUNT(*) AS present_observation_count
FROM islands
GROUP BY collection_id, asset_id, island_id;
```

These rows are observation periods, not exact PBS availability intervals.

---

## 8. Implementation

### 8.1 SQLite connection configuration

Every application connection shall execute:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
```

Operational notes:

- `foreign_keys = ON` is connection-scoped and must not be assumed from database creation.
- WAL mode supports concurrent readers with one writer.
- `synchronous = NORMAL` is appropriate for WAL-backed application data where the source can be replayed. Use `FULL` if stricter durability is required.
- `busy_timeout` reduces avoidable lock failures but does not replace transaction discipline.

### 8.2 Timestamp format

Application timestamps shall use UTC ISO 8601 text with a `Z` suffix:

```text
2026-10-15T03:22:57.123Z
```

Rules:

- Normalize all application timestamps to UTC before insertion.
- Preserve raw source epoch values in saved source snapshots if required for audit.
- Convert source timestamps once in the normalization layer.
- Do not mix local offsets and UTC strings in indexed timestamp columns.

### 8.3 Ingestion transaction

A successful catalog ingestion shall use one database transaction:

```text
BEGIN IMMEDIATE
  insert provisional catalog_run
  resolve or insert assets
  resolve or insert collections
  insert asset observations
  insert collection observations
  insert membership observations
  insert image observations
  mark run parse_succeeded and catalog_complete
COMMIT
```

If normalization or insertion fails, roll back all observation rows for that run. A failed retrieval may still be recorded as a standalone failed `catalog_run` transaction.

### 8.4 Idempotency

Reprocessing the same source snapshot shall not create duplicate observations.

Controls:

- Unique run index on source URL, observation time, and response hash.
- Primary keys include `run_id` and stable entity key.
- Canonical hashes use deterministic JSON serialization.
- Replay tools either reuse the original run or create a new explicitly labeled replay run, according to governance policy.

Recommended canonical JSON settings:

```python
json.dumps(
    normalized_object,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
)
```

Compute SHA-256 over UTF-8 bytes.

### 8.5 Completeness decision

The parser shall determine completeness before absence is inferred. Store `catalog_complete = 0` when:

- expected Next.js page data is missing;
- expected collections are absent without an approved source change;
- a schema-breaking parser warning occurs;
- pagination or dependent resources are known to be incomplete;
- retrieved asset count falls below a configurable anomaly threshold pending review.

An unexpectedly low count should normally block completeness rather than delete historical membership through inference.

### 8.6 Identifier reconciliation

For each incoming source record:

1. Query all populated external identifiers.
2. Collect distinct matched `asset_id` values.
3. If no matches exist, insert one asset row.
4. If exactly one match exists, associate the observation with that asset.
5. Add newly observed alternate identifiers only when uniqueness constraints are satisfied.
6. If more than one internal asset matches, insert `identity_conflict`, omit that observation from automatic processing, and mark the run incomplete unless policy explicitly allows partial completion.

### 8.7 Ordinal position

`ordinal_position` shall use a defined convention. Recommended convention:

- zero-based integer matching source entry order;
- unique within a run and collection when the source provides a deterministic order;
- nullable when order is unavailable.

Do not use ordinal position in asset identity or membership identity.

### 8.8 Source snapshot storage

Recommended directory structure:

```text
data/
  catalog.sqlite3
  snapshots/
    2026/
      10/
        20261015T032257Z-catalog.html.gz
        20261015T032257Z-next-data.json.gz
```

Store the relative path and response SHA-256 in `catalog_run`. Use atomic file writes followed by rename.

Retention options:

- retain all extracted JSON snapshots;
- retain HTML snapshots for a shorter troubleshooting window;
- retain hashes indefinitely;
- document and automate retention rather than deleting files manually.

### 8.9 Schema migrations

Maintain a migration table:

```sql
CREATE TABLE schema_migration
(
    version       INTEGER PRIMARY KEY,
    applied_at    TEXT NOT NULL,
    description   TEXT NOT NULL,
    checksum      TEXT NOT NULL
);
```

Migration requirements:

- migrations are ordered and immutable after release;
- database backup precedes migration;
- migration runs inside a transaction where SQLite permits;
- application startup verifies expected schema version;
- downgrade behavior is documented per migration;
- production schema is never changed ad hoc.

---

## 9. Operational Procedures

### 9.1 Scheduled catalog run

Recommended initial frequency: daily. Increase only if rotation analysis demonstrates a business need.

Procedure:

1. Open SQLite with required PRAGMAs.
2. Begin a run and establish one UTC observation timestamp.
3. Retrieve the catalog with timeouts and bounded retries.
4. Save and hash the response.
5. Parse and normalize the catalog.
6. Apply completeness checks.
7. Ingest the run atomically.
8. Execute post-load validation.
9. Emit structured run metrics.
10. Back up according to policy.

### 9.2 Failure handling

#### Retrieval failure

Record:

```text
retrieval_succeeded = 0
parse_succeeded = 0
catalog_complete = 0
```

Do not insert observations and do not infer absence.

#### Parse failure

Record the response hash and snapshot path, with:

```text
retrieval_succeeded = 1
parse_succeeded = 0
catalog_complete = 0
```

Do not infer absence.

#### Completeness anomaly

If parsing succeeds but expected structure or counts are questionable:

```text
retrieval_succeeded = 1
parse_succeeded = 1
catalog_complete = 0
```

Retain diagnostics and require review or a corrected parser.

#### Identity conflict

Record the conflict. Do not merge automatically. Mark the run incomplete if the unresolved record affects full-catalog interpretation.

### 9.3 Backup

Use the SQLite backup API or `VACUUM INTO`, not an uncoordinated filesystem copy of an active WAL database.

Example maintenance command:

```sql
VACUUM INTO 'catalog-backup-20261015.sqlite3';
```

Operational requirements:

- verify backups by opening them and running integrity checks;
- keep backups outside the working database directory;
- define retention by daily, weekly, and monthly tiers;
- test restore procedures at least quarterly;
- include snapshot files in backup policy if they are part of required provenance.

### 9.4 Integrity checks

Run routinely:

```sql
PRAGMA quick_check;
PRAGMA foreign_key_check;
```

Run `PRAGMA integrity_check;` during scheduled maintenance or backup validation.

### 9.5 WAL checkpointing

Monitor WAL growth. Checkpoint after large loads or before controlled backups when appropriate:

```sql
PRAGMA wal_checkpoint(TRUNCATE);
```

Do not checkpoint aggressively during active readers without measuring impact.

### 9.6 Statistics and optimization

After meaningful data growth or index changes:

```sql
PRAGMA optimize;
```

Use `EXPLAIN QUERY PLAN` for slow history, change-detection, or rotation queries. Add indexes only for measured query patterns.

### 9.7 Retention

Recommended initial policy:

- retain normalized observation rows indefinitely;
- retain source hashes indefinitely;
- retain extracted JSON snapshots indefinitely unless volume becomes material;
- retain raw HTML for at least 90 days;
- retain failed-run snapshots until the parser issue is resolved plus the normal retention period.

Any deletion policy shall preserve the ability to explain historical observations.

### 9.8 Security

- Run the process as an unprivileged operating-system account.
- Restrict database and snapshot directories to the application account and authorized operators.
- Do not store cookies, authorization headers, or transient signed URLs in logs.
- Validate all source-derived filenames and paths.
- Use parameterized SQL exclusively.
- Do not load SQLite extensions unless explicitly approved.
- Treat source HTML and JSON as untrusted input.

---

## 10. Validation

### 10.1 Schema validation

Validate after database creation and every migration:

```sql
PRAGMA foreign_key_check;
PRAGMA integrity_check;
```

Expected result for `integrity_check`:

```text
ok
```

### 10.2 Fixture validation

The supplied catalog fixture currently yields:

- 94 video occurrences before deduplication;
- 86 unique assets;
- 24 `fullEpisode` assets;
- 14 ordinary full episodes;
- 10 ASL full episodes.

Automated fixture tests shall verify these counts until the fixture is intentionally replaced.

### 10.3 Ingestion invariants

For each successful complete run:

1. Every asset observation references one asset.
2. Every membership references an observed asset and observed collection from the same run.
3. No duplicate asset observation exists for the same run and asset.
4. No duplicate membership exists for the same run, collection, and asset.
5. Every watch URL is syntactically valid and derived from observed route components.
6. Every metadata hash matches canonical serialization of the stored fields.
7. No unresolved identity conflict is silently merged.

Example same-run referential validation:

```sql
SELECT mo.*
FROM membership_observation AS mo
LEFT JOIN asset_observation AS ao
  ON ao.run_id = mo.run_id
 AND ao.asset_id = mo.asset_id
LEFT JOIN collection_observation AS co
  ON co.run_id = mo.run_id
 AND co.collection_id = mo.collection_id
WHERE ao.asset_id IS NULL
   OR co.collection_id IS NULL;
```

Expected result: zero rows.

### 10.4 Absence validation

Before reporting an asset or membership as absent, verify that the comparison run is complete.

Example assets present in the prior complete run but absent in the current complete run:

```sql
WITH complete_runs AS
(
    SELECT
        run_id,
        observed_at,
        LAG(run_id) OVER
        (
            ORDER BY observed_at, run_id
        ) AS prior_run_id
    FROM catalog_run
    WHERE retrieval_succeeded = 1
      AND parse_succeeded = 1
      AND catalog_complete = 1
),
selected AS
(
    SELECT *
    FROM complete_runs
    WHERE run_id = :current_run_id
)
SELECT previous.asset_id
FROM selected AS s
JOIN asset_observation AS previous
  ON previous.run_id = s.prior_run_id
LEFT JOIN asset_observation AS current
  ON current.run_id = s.run_id
 AND current.asset_id = previous.asset_id
WHERE current.asset_id IS NULL;
```

### 10.5 Rotation validation

Create synthetic fixture runs with at least two presence periods for one collection membership:

```text
Run 1: absent
Run 2: present
Run 3: present
Run 4: absent
Run 5: present
Run 6: absent
```

The presence-period query must return two periods, not one combined interval.

### 10.6 Idempotency validation

Load the same fixture twice using the same observation identity and response hash.

Expected outcome:

- no duplicate run;
- no duplicate observations;
- no changed row counts;
- same canonical hashes.

### 10.7 Identity-conflict validation

Construct a fixture in which Media Manager GUID and CMS ID resolve to different assets.

Expected outcome:

- one `identity_conflict` row;
- no automatic merge;
- affected observation rejected;
- run marked incomplete unless an approved partial-load policy applies.

### 10.8 Performance validation

Test at projected scale, including at least:

- three years of daily runs;
- 200 assets per run;
- multiple collection memberships;
- image observations;
- change and rotation queries.

Measure:

- load duration;
- database size;
- latest-catalog query latency;
- asset-history query latency;
- rotation-query latency;
- backup and restore duration.

SQLite is acceptable only while these measures remain within documented operational thresholds.

---

## 11. Future Extensions

### 11.1 Watch-page observations

Phase 2 may add:

```text
watch_page_run
watch_page_observation
```

Candidate fields include:

- long description;
- source expiry date;
- DRM status;
- season and episode numbers;
- watch-page response hash;
- parse status;
- availability status.

A catalog run shall not imply that every watch page was checked.

### 11.2 Media profiles

Add stable media references and run-scoped observations separately:

```text
media_profile
media_profile_observation
```

Store PBS URS redirect URLs where appropriate. Avoid persisting temporary resolved CDN URLs as durable identities.

### 11.3 Captions

Add:

```text
caption_track
caption_observation
```

Candidate caption attributes:

- language code;
- label;
- caption kind;
- format;
- source URL;
- content hash;
- availability result.

A caption-check run defines caption observation scope. Absence from a catalog run has no caption meaning.

### 11.4 Download state

Download state is an operational subsystem and should not modify source observations.

Candidate tables:

```text
download_attempt
download_artifact
download_verification
```

Store:

- asset ID;
- selected media profile;
- start and completion times;
- output path;
- file size;
- SHA-256;
- `ffprobe` duration;
- verification result;
- failure category.

### 11.5 External episode reconciliation

If external episode numbering is added, keep it in a separately governed mapping table. Do not overwrite PBS source metadata with inferred third-party values.

### 11.6 Schema evolution

Future maintenance shall preserve these boundaries:

- identity registry versus observations;
- catalog scope versus watch-page scope;
- source facts versus application-derived facts;
- observations versus downloaded artifacts.

Do not add cached first/last-observed columns to registry tables unless measured performance justifies a controlled, rebuildable summary layer.

---

## 12. Operational Maintenance Guidance

### 12.1 Parser changes

When PBS changes its JSON structure:

1. Preserve the failing raw response.
2. Mark the run incomplete.
3. Add or update a fixture.
4. update the parser and increment `parser_version`.
5. rerun validation against old and new fixtures.
6. replay the saved response as an explicitly governed replay if historical normalization is required.

Do not reinterpret prior observations silently.

### 12.2 Constraint changes

Before promoting a lookup index to a unique constraint, query all historical data for duplicates and document the evidence.

This applies especially to `legacy_tp_media_id`.

### 12.3 Expected-count changes

Catalog counts are validation baselines, not permanent business rules. When content rotates:

- variation in membership is expected;
- a total collapse or unexpected structural loss is not;
- completeness checks should use configurable thresholds and schema expectations rather than fixed episode IDs.

### 12.4 Database growth

Review quarterly:

- row counts by table;
- database and WAL size;
- index usage;
- snapshot volume;
- backup duration;
- query plans;
- retention effectiveness.

If full observations become materially expensive, optimize snapshots or introduce derived summaries before replacing the append-only model.

### 12.5 Recovery

Recovery procedure:

1. Stop the writer process.
2. Preserve the damaged database and WAL files for analysis.
3. Restore the latest verified database backup.
4. Restore required source snapshots.
5. Replay catalog runs after the backup point in observation order.
6. run integrity and foreign-key checks.
7. compare run counts, asset counts, and latest complete catalog state.
8. resume scheduled ingestion.

---

## 13. References

### 13.1 Project evidence

- PBS KIDS Odd Squad catalog page: `https://pbskids.org/videos/odd-squad`
- Uploaded catalog fixture: `odd-squad.html`
- Embedded Next.js payload: `script#__NEXT_DATA__`
- Phase 1 parser: `odd_squad_catalog.py`
- Phase 1 parser tests: `test_odd_squad_catalog.py`

### 13.2 Technology references

- SQLite documentation: `https://sqlite.org/docs.html`
- SQLite foreign keys: `https://sqlite.org/foreignkeys.html`
- SQLite WAL mode: `https://sqlite.org/wal.html`
- SQLite backup API: `https://sqlite.org/backup.html`
- SQLite window functions: `https://sqlite.org/windowfunctions.html`
- SQLite `WITHOUT ROWID`: `https://sqlite.org/withoutrowid.html`

---

## Appendix A. Decision Record

### A.1 Observation model versus bitemporal model

**Decision:** Use append-only observations.

**Reason:** The source reliably establishes what was returned at crawl time but does not generally establish exact real-world validity intervals for catalog facts.

### A.2 Internal primary key

**Decision:** Use SQLite `INTEGER PRIMARY KEY` for assets and collections.

**Reason:** External identifiers belong to PBS source systems and should not be propagated as relational foreign keys.

### A.3 External identifier constraints

**Decision:** Enforce uniqueness for Media Manager GUID and PBS KIDS CMS ID; initially index but do not enforce uniqueness for legacy TP media ID.

**Reason:** The first two identifiers have stronger observed identity semantics. The legacy identifier requires empirical validation before a stronger constraint is justified.

### A.4 First and last observation columns

**Decision:** Do not store them in `asset` or `collection`.

**Reason:** They are derivable from observation tables and would duplicate normalized facts.

### A.5 Presence periods

**Decision:** Derive all repeated presence periods from observation sequences.

**Reason:** PBS is expected to rotate episodes. A single first-present or first-absent pair cannot represent recurring membership.

### A.6 Page placement

**Decision:** Do not store page placement.

**Reason:** Masthead versus body rendering is not required for catalog, analytics, or download decisions. Duplicate renderings collapse to one membership observation.

### A.7 Full observation storage

**Decision:** Store one normalized observation per asset per complete run.

**Reason:** The catalog is small, and complete observations simplify provenance, historical reconstruction, and validation.
