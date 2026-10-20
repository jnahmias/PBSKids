from pathlib import Path
import tempfile
import unittest

import pbs_kids_catalog_db as db
from test_pbs_kids_catalog_onboarding import fixture_html

URL = "https://pbskids.org/videos/example-show"

class CatalogDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "catalog.sqlite3"
        config_path = Path(self.temp.name) / "test.ini"
        config_path.write_text(
            f"[application]\ndatabase={self.db_path}\n",
            encoding="utf-8",
        )
        self.settings, _ = db.load_settings(
            ["--config", str(config_path), "init"]
        )
        db.initialize_database(self.db_path, self.settings)
        with db.connect(self.db_path, self.settings) as conn:
            show_id = db.configure_show(
                conn, "example-show", URL, 1, "daily", True
            )
            show = conn.execute(
                "SELECT * FROM show WHERE show_id = ?", (show_id,)
            ).fetchone()
            conn.execute(
                "INSERT INTO show_required_collection(show_id, collection_slug) VALUES (?, ?)",
                (show["show_id"], "example-full-episodes"),
            )

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, observed_at):
        html = fixture_html()
        parsed = db.catalog_parser.parse_catalog_html(html, URL)
        with db.connect(self.db_path, self.settings) as connection:
            show = db.find_show(connection, "example-show")
            return db.ingest_catalog_run(
                connection,
                show,
                parsed,
                {
                    "status": 200,
                    "etag": None,
                    "last_modified": None,
                    "final_url": URL,
                },
                html,
                observed_at,
                None,
            )["run_id"]

    def test_first_complete_run(self):
        self.ingest("2026-10-15T03:22:57.000Z")
        with db.connect(self.db_path, self.settings) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM asset_observation").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM membership_observation").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT catalog_complete FROM catalog_run").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pragma_foreign_key_check").fetchone()[0], 0)

    def test_second_run_reuses_identities_and_appends_observations(self):
        self.ingest("2026-10-15T03:22:57.000Z")
        self.ingest("2026-10-16T03:22:57.000Z")
        with db.connect(self.db_path, self.settings) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM asset_observation").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM catalog_run").fetchone()[0], 2)
            self.assertEqual(conn.execute(
                """
                SELECT COUNT(*)
                FROM asset_observation
                WHERE run_id =
                (
                    SELECT run_id
                    FROM catalog_run
                    WHERE catalog_complete = 1
                    ORDER BY observed_at DESC, run_id DESC
                    LIMIT 1
                )
                """
            ).fetchone()[0], 2)

    def test_minimum_asset_threshold_blocks_completeness(self):
        with db.connect(self.db_path, self.settings) as conn:
            conn.execute("UPDATE show SET expected_minimum_assets = 3 WHERE canonical_key = 'example-show'")
        self.ingest("2026-10-15T03:22:57.000Z")
        with db.connect(self.db_path, self.settings) as conn:
            row = conn.execute("SELECT catalog_complete, error_message FROM catalog_run").fetchone()
            self.assertEqual(row[0], 0)
            self.assertIn("below minimum", row[1])
            self.assertEqual(conn.execute(
                """
                SELECT COUNT(*)
                FROM asset_observation
                WHERE run_id =
                (
                    SELECT run_id
                    FROM catalog_run
                    WHERE catalog_complete = 1
                    ORDER BY observed_at DESC, run_id DESC
                    LIMIT 1
                )
                """
            ).fetchone()[0], 0)

if __name__ == "__main__":
    unittest.main()
