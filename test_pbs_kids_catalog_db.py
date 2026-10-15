from pathlib import Path
import sqlite3
import tempfile
import unittest

import pbs_kids_catalog_db as db

FIXTURE = Path(__file__).with_name("odd-squad.html")
URL = "https://pbskids.org/videos/odd-squad"

class CatalogDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "catalog.sqlite3"
        db.initialize_database(self.db_path)
        with db.connect(self.db_path) as conn:
            db.add_show(conn, "odd-squad", URL, 80, "daily")
            show = db.get_show(conn, "odd-squad")
            conn.execute(
                "INSERT INTO show_required_collection(show_id, collection_slug) VALUES (?, ?)",
                (show["show_id"], "odd-squad-full-episodes"),
            )

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, observed_at):
        return db.update_show(self.db_path, "odd-squad", FIXTURE, None, observed_at)

    def test_first_complete_run(self):
        self.ingest("2026-10-15T03:22:57.000Z")
        with db.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 86)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM asset_observation").fetchone()[0], 86)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0], 6)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM membership_observation").fetchone()[0], 86)
            self.assertEqual(conn.execute("SELECT catalog_complete FROM catalog_run").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pragma_foreign_key_check").fetchone()[0], 0)

    def test_second_run_reuses_identities_and_appends_observations(self):
        self.ingest("2026-10-15T03:22:57.000Z")
        self.ingest("2026-10-16T03:22:57.000Z")
        with db.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 86)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM asset_observation").fetchone()[0], 172)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM catalog_run").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM current_catalog_by_show").fetchone()[0], 86)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM identity_conflict").fetchone()[0], 0)

    def test_minimum_asset_threshold_blocks_completeness(self):
        with db.connect(self.db_path) as conn:
            conn.execute("UPDATE show SET expected_minimum_assets = 100 WHERE canonical_key = 'odd-squad'")
        self.ingest("2026-10-15T03:22:57.000Z")
        with db.connect(self.db_path) as conn:
            row = conn.execute("SELECT catalog_complete, error_message FROM catalog_run").fetchone()
            self.assertEqual(row[0], 0)
            self.assertIn("below minimum", row[1])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM current_catalog_by_show").fetchone()[0], 0)

if __name__ == "__main__":
    unittest.main()
