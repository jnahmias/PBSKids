import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import pbs_kids_catalog_db as catalog_database
import pbs_kids_video_download as downloader
import pbs_kids_watch_parser as watch_parser


GUID = "11111111-1111-1111-1111-111111111111"


def watch_html(drm_enabled: bool = False) -> str:
    payload = {
        "buildId": "watch-build",
        "props": {
            "pageProps": {
                "videoData": {
                    "expiryDate": "1893456000",
                    "mediaManagerAsset": {
                        "id": GUID,
                        "title": "Episode One",
                        "description_long": "Long description.",
                        "duration": 1500,
                        "drm_enabled": drm_enabled,
                        "videos": [
                            {
                                "profile": "hls-16x9-720p",
                                "url": "https://urs.example/video.m3u8",
                            },
                            {
                                "profile": "mp4-16x9-720p",
                                "url": "https://urs.example/video.mp4",
                            },
                        ],
                    },
                }
            }
        },
    }
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></html>"
    )


class WatchParserTests(unittest.TestCase):
    def test_parse_and_select_profile(self):
        parsed = watch_parser.parse_watch_page(watch_html())
        selected = watch_parser.choose_media_profile(
            parsed,
            ["mp4-16x9-720p", "hls-16x9-720p"],
        )
        self.assertEqual(parsed.media_manager_guid, GUID)
        self.assertEqual(selected.profile, "mp4-16x9-720p")

    def test_drm_page_is_not_selected(self):
        parsed = watch_parser.parse_watch_page(watch_html(drm_enabled=True))
        self.assertIsNone(
            watch_parser.choose_media_profile(parsed, ["hls-16x9-720p"])
        )


class DownloadCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = root / "catalog.sqlite3"
        self.config = root / "test.ini"
        self.config.write_text(
            "\n".join(
                [
                    "[application]",
                    f"database = {self.database}",
                    "",
                    "[downloads]",
                    f"media_root = {root / 'media'}",
                    f"temporary_root = {root / 'staging'}",
                    "video_types = fullEpisode, movie",
                ]
            ),
            encoding="utf-8",
        )
        self.settings, _ = catalog_database.load_settings(
            ["--config", str(self.config), "init"]
        )
        catalog_database.initialize_database(self.database, self.settings)
        self.connection = catalog_database.connect(self.database, self.settings)
        downloader.ensure_download_schema(self.connection)
        self.seed_catalog()

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def seed_catalog(self):
        show_id = self.connection.execute(
            """
            INSERT INTO show
                (canonical_key, catalog_url, enabled,
                 expected_minimum_assets, retrieval_frequency)
            VALUES
                ('example-show', 'https://pbskids.org/videos/example-show', 1, 1, 'daily')
            """
        ).lastrowid
        run_id = self.connection.execute(
            """
            INSERT INTO catalog_run
                (show_id, observed_at, source_url, parser_version,
                 retrieval_succeeded, parse_succeeded, catalog_complete)
            VALUES
                (?, '2026-10-18T00:00:00Z',
                 'https://pbskids.org/videos/example-show',
                 'test', 1, 1, 1)
            """,
            (show_id,),
        ).lastrowid
        asset_id = self.connection.execute(
            """
            INSERT INTO asset
                (media_manager_guid, pbs_kids_cms_id, legacy_tp_media_id)
            VALUES
                (?, '1', '9001')
            """,
            (GUID,),
        ).lastrowid
        collection_id = self.connection.execute(
            """
            INSERT INTO collection
                (show_id, pbs_collection_id)
            VALUES
                (?, '100')
            """,
            (show_id,),
        ).lastrowid
        self.connection.execute(
            """
            INSERT INTO asset_observation
                (run_id, asset_id, slug, title, video_type,
                 description_short, duration_seconds,
                 source_premiered_at, metadata_hash)
            VALUES
                (?, ?, 'episode-one', 'Episode One', 'fullEpisode',
                 'Test.', 1500, NULL, 'hash')
            """,
            (run_id, asset_id),
        )
        self.connection.execute(
            """
            INSERT INTO collection_observation
                (run_id, collection_id, slug, title, heading, metadata_hash)
            VALUES
                (?, ?, 'example-episodes', 'Episodes', 'Episodes', 'hash')
            """,
            (run_id, collection_id),
        )
        self.connection.execute(
            """
            INSERT INTO membership_observation
                (run_id, collection_id, asset_id, ordinal_position, watch_url)
            VALUES
                (?, ?, ?, 0, 'https://pbskids.org/videos/watch/example')
            """,
            (run_id, collection_id, asset_id),
        )
        self.connection.commit()
        self.asset_id = asset_id

    def test_candidate_query_and_dry_run(self):
        query, parameters = downloader.candidate_query(("fullEpisode", "movie"))
        row = self.connection.execute(query, parameters).fetchone()
        self.assertIsNotNone(row)

        settings = downloader.load_download_settings(self.config)
        result = downloader.download_candidate(
            self.connection,
            row,
            self.settings,
            settings,
            dry_run=True,
        )
        self.assertEqual(result["status"], "would-download")
        self.assertIn("Episode One [1].mp4", result["path"])

    def test_recorded_artifact_removes_candidate(self):
        watch_id = self.connection.execute(
            """
            INSERT INTO watch_page_observation
                (asset_id, observed_at, watch_url,
                 retrieval_succeeded, parse_succeeded)
            VALUES
                (?, '2026-10-18T00:00:00Z', 'https://example/watch', 1, 1)
            """,
            (self.asset_id,),
        ).lastrowid
        attempt_id = self.connection.execute(
            """
            INSERT INTO download_attempt
                (asset_id, watch_observation_id, started_at,
                 target_path, status)
            VALUES
                (?, ?, '2026-10-18T00:00:00Z', '/media/file.mp4', 'complete')
            """,
            (self.asset_id, watch_id),
        ).lastrowid
        self.connection.execute(
            """
            INSERT INTO download_artifact
                (asset_id, download_attempt_id, relative_path,
                 file_size_bytes, sha256, verified_at,
                 verification_status)
            VALUES
                (?, ?, 'example/file.mp4', 100, 'abc',
                 '2026-10-18T00:00:00Z', 'verified')
            """,
            (self.asset_id, attempt_id),
        )
        self.connection.commit()

        query, parameters = downloader.candidate_query(("fullEpisode",))
        rows = self.connection.execute(query, parameters).fetchall()
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
