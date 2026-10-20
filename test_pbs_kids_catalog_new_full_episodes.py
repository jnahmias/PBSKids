"""Tests for newly discovered full-episode reporting."""

import sqlite3
import sys
import types
import unittest

sys.modules.setdefault("pbs_kids_catalog_parser", types.ModuleType("pbs_kids_catalog_parser"))

from pbs_kids_catalog_db import count_new_full_episodes


class NewFullEpisodeCountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            CREATE TABLE catalog_run
            (
                run_id INTEGER PRIMARY KEY,
                show_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                retrieval_succeeded INTEGER NOT NULL,
                parse_succeeded INTEGER NOT NULL,
                catalog_complete INTEGER NOT NULL
            );

            CREATE TABLE asset_observation
            (
                run_id INTEGER NOT NULL,
                asset_id INTEGER NOT NULL,
                video_type TEXT NOT NULL,
                PRIMARY KEY (run_id, asset_id)
            );
            """
        )

    def tearDown(self) -> None:
        self.connection.close()

    def add_run(self, run_id, show_id, observed_at, complete, observations):
        self.connection.execute(
            """
            INSERT INTO catalog_run
                (run_id, show_id, observed_at, retrieval_succeeded,
                 parse_succeeded, catalog_complete)
            VALUES (?, ?, ?, 1, 1, ?)
            """,
            (run_id, show_id, observed_at, int(complete)),
        )
        self.connection.executemany(
            "INSERT INTO asset_observation VALUES (?, ?, ?)",
            [(run_id, asset_id, video_type) for asset_id, video_type in observations],
        )

    def test_first_complete_run_counts_all_full_episodes(self):
        self.add_run(1, 10, "2026-10-01T00:00:00Z", True,
                     [(100, "fullEpisode"), (101, "fullEpisode"), (102, "clip")])
        self.assertEqual(count_new_full_episodes(self.connection, 10, 1), 2)

    def test_only_never_before_observed_assets_are_new(self):
        self.add_run(1, 10, "2026-10-01T00:00:00Z", True,
                     [(100, "fullEpisode")])
        self.add_run(2, 10, "2026-10-02T00:00:00Z", True,
                     [(100, "fullEpisode"), (101, "fullEpisode")])
        self.assertEqual(count_new_full_episodes(self.connection, 10, 2), 1)

    def test_returning_rotated_episode_is_not_new(self):
        self.add_run(1, 10, "2026-10-01T00:00:00Z", True,
                     [(100, "fullEpisode")])
        self.add_run(2, 10, "2026-10-02T00:00:00Z", True, [])
        self.add_run(3, 10, "2026-10-03T00:00:00Z", True,
                     [(100, "fullEpisode")])
        self.assertEqual(count_new_full_episodes(self.connection, 10, 3), 0)

    def test_incomplete_current_run_reports_zero(self):
        self.add_run(1, 10, "2026-10-01T00:00:00Z", False,
                     [(100, "fullEpisode")])
        self.assertEqual(count_new_full_episodes(self.connection, 10, 1), 0)

    def test_prior_incomplete_run_does_not_establish_history(self):
        self.add_run(1, 10, "2026-10-01T00:00:00Z", False,
                     [(100, "fullEpisode")])
        self.add_run(2, 10, "2026-10-02T00:00:00Z", True,
                     [(100, "fullEpisode")])
        self.assertEqual(count_new_full_episodes(self.connection, 10, 2), 1)

    def test_show_history_is_isolated(self):
        self.add_run(1, 20, "2026-10-01T00:00:00Z", True,
                     [(100, "fullEpisode")])
        self.add_run(2, 10, "2026-10-02T00:00:00Z", True,
                     [(100, "fullEpisode")])
        self.assertEqual(count_new_full_episodes(self.connection, 10, 2), 1)


if __name__ == "__main__":
    unittest.main()
