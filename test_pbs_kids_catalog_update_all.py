import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pbs_kids_catalog_db as application


URL_TEMPLATE = "https://pbskids.org/videos/{key}"


def fixture_html(title: str, slug: str, collection_id: str, asset_id: str) -> str:
    payload = {
        "buildId": "test-build",
        "props": {
            "pageProps": {
                "pageData": {
                    "title": title,
                    "slug": slug,
                    "bodyContentModules": [
                        {
                            "heading": "Episodes",
                            "collection": [
                                {
                                    "id": collection_id,
                                    "slug": f"{slug}-episodes",
                                    "title": "Episodes",
                                    "entries": [
                                        {
                                            "__typename": "PbsKidsVideo",
                                            "id": asset_id,
                                            "guid": f"{int(asset_id):08d}-0000-0000-0000-000000000000",
                                            "slug": f"{slug}-episode",
                                            "title": f"{title} Episode",
                                            "videoType": "fullEpisode",
                                            "mediaManagerAsset": {
                                                "duration": 1500,
                                                "legacy_tp_media_id": f"9{asset_id}",
                                                "description_short": "Test episode.",
                                                "images": [],
                                            },
                                        }
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


class UpdateAllTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.database = directory / "catalog.sqlite3"
        self.snapshots = directory / "snapshots"
        config = directory / "test.ini"
        config.write_text(
            f"[application]\ndatabase={self.database}\nsnapshot_root={self.snapshots}\n",
            encoding="utf-8",
        )
        self.settings, _ = application.load_settings(
            ["--config", str(config), "init"]
        )
        application.initialize_database(self.database, self.settings)
        with application.connect(self.database, self.settings) as connection:
            application.configure_show(
                connection,
                "alpha",
                URL_TEMPLATE.format(key="alpha"),
                1,
                "daily",
                True,
            )
            application.configure_show(
                connection,
                "beta",
                URL_TEMPLATE.format(key="beta"),
                1,
                "daily",
                True,
            )
            application.configure_show(
                connection,
                "disabled",
                URL_TEMPLATE.format(key="disabled"),
                1,
                "daily",
                False,
            )

    def tearDown(self):
        self.temporary.cleanup()

    def fake_fetch(self, url, settings):
        key = url.rsplit("/", 1)[-1]
        if key == "beta":
            raise RuntimeError("simulated retrieval failure")
        return fixture_html("Alpha", "alpha", "101", "1"), {
            "status": 200,
            "etag": None,
            "last_modified": None,
            "final_url": url,
        }

    @patch("pbs_kids_catalog_db.fetch_catalog")
    def test_continues_after_failure(self, fetch_catalog):
        fetch_catalog.side_effect = self.fake_fetch
        results = application.update_all_shows(
            self.database,
            self.settings,
            self.snapshots,
        )

        self.assertEqual([item["show"] for item in results], ["alpha", "beta"])
        self.assertTrue(results[0]["success"])
        self.assertFalse(results[1]["success"])

        with application.connect(self.database, self.settings) as connection:
            runs = connection.execute(
                "SELECT COUNT(*) FROM catalog_run"
            ).fetchone()[0]
            complete = connection.execute(
                "SELECT COUNT(*) FROM catalog_run WHERE catalog_complete = 1"
            ).fetchone()[0]
        self.assertEqual(runs, 2)
        self.assertEqual(complete, 1)

    @patch("pbs_kids_catalog_db.fetch_catalog")
    def test_only_and_exclude_filters(self, fetch_catalog):
        fetch_catalog.side_effect = self.fake_fetch
        results = application.update_all_shows(
            self.database,
            self.settings,
            self.snapshots,
            only={"alpha", "beta"},
            exclude={"beta"},
        )
        self.assertEqual([item["show"] for item in results], ["alpha"])

    def test_unknown_only_show_fails(self):
        with self.assertRaises(ValueError):
            application.update_all_shows(
                self.database,
                self.settings,
                self.snapshots,
                only={"not-configured"},
            )


if __name__ == "__main__":
    unittest.main()
