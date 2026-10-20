from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

import pbs_kids_catalog_db as application
import pbs_kids_catalog_parser as catalog_parser


URL = "https://pbskids.org/videos/example-show"


def fixture_html() -> str:
    payload = {
        "buildId": "test-build",
        "props": {
            "pageProps": {
                "pageData": {
                    "title": "Example Show",
                    "slug": "example-show",
                    "bodyContentModules": [
                        {
                            "heading": "Episodes",
                            "collection": [
                                {
                                    "id": "100",
                                    "slug": "example-full-episodes",
                                    "title": "Full Episodes",
                                    "entries": [
                                        {
                                            "__typename": "PbsKidsVideo",
                                            "id": "1",
                                            "guid": "11111111-1111-1111-1111-111111111111",
                                            "slug": "episode-one",
                                            "title": "Episode One",
                                            "videoType": "fullEpisode",
                                            "mediaManagerAsset": {
                                                "duration": 1500,
                                                "legacy_tp_media_id": "9001",
                                                "description_short": "First episode.",
                                                "premiered_on": "1700000000",
                                                "images": [],
                                            },
                                        },
                                        {
                                            "__typename": "PbsKidsVideo",
                                            "id": "2",
                                            "guid": "22222222-2222-2222-2222-222222222222",
                                            "slug": "episode-two",
                                            "title": "Episode Two",
                                            "videoType": "clip",
                                            "mediaManagerAsset": {
                                                "duration": 300,
                                                "legacy_tp_media_id": "9002",
                                                "description_short": "Second item.",
                                                "images": [],
                                            },
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                    "mastheadContentModules": [],
                }
            }
        },
    }
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></html>"
    )


class OnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = catalog_parser.parse_catalog_html(
            fixture_html(),
            URL,
        )
        cls.report = application.build_discovery_report(cls.parsed, URL)

    def test_discovery_report(self):
        self.assertEqual(self.report["unique_asset_count"], 2)
        self.assertEqual(self.report["occurrence_count"], 2)
        self.assertEqual(self.report["video_types"]["fullEpisode"], 1)
        self.assertIn(
            "example-full-episodes",
            {item["slug"] for item in self.report["collections"]},
        )
        self.assertEqual(self.report["recommended_minimum_assets"], 1)

    def test_onboarding_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "test.ini"
            database = Path(directory) / "catalog.sqlite3"
            config_path.write_text(
                f"[application]\ndatabase={database}\n",
                encoding="utf-8",
            )
            settings, _ = application.load_settings(
                ["--config", str(config_path), "init"]
            )
            application.initialize_database(database, settings)

            with application.connect(database, settings) as connection:
                show_id = application.onboard_show(
                    connection=connection,
                    key="example-show",
                    url=URL,
                    report=self.report,
                    minimum_assets=None,
                    frequency="daily",
                    required_collections=["example-full-episodes"],
                    enable=False,
                    replace=False,
                )
                row = connection.execute(
                    "SELECT * FROM show WHERE show_id = ?",
                    (show_id,),
                ).fetchone()
                required = connection.execute(
                    "SELECT collection_slug FROM show_required_collection"
                ).fetchall()

            self.assertEqual(row["enabled"], 0)
            self.assertEqual(row["expected_minimum_assets"], 1)
            self.assertEqual(required[0][0], "example-full-episodes")

    def test_existing_show_requires_replace(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE show
            (
                show_id INTEGER PRIMARY KEY,
                canonical_key TEXT UNIQUE,
                catalog_url TEXT UNIQUE,
                enabled INTEGER,
                expected_minimum_assets INTEGER,
                retrieval_frequency TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE show_required_collection
            (
                show_id INTEGER,
                collection_slug TEXT,
                PRIMARY KEY (show_id, collection_slug)
            )
            """
        )
        application.configure_show(
            connection,
            "example-show",
            URL,
            50,
            "daily",
            False,
        )
        with self.assertRaises(ValueError):
            application.onboard_show(
                connection,
                "example-show",
                URL,
                self.report,
                None,
                "daily",
                [],
                False,
                False,
            )


if __name__ == "__main__":
    unittest.main()
