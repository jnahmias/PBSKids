"""Tests for persistent DRM availability-state transitions."""

from __future__ import annotations

import sqlite3
import sys
import types
import unittest
from dataclasses import dataclass

catalog_database = types.ModuleType("pbs_kids_catalog_db")
catalog_database.Settings = object
sys.modules.setdefault("pbs_kids_catalog_db", catalog_database)

watch_parser = types.ModuleType("pbs_kids_watch_parser")


@dataclass(frozen=True)
class FakeProfile:
    has_drm_license: bool


@dataclass(frozen=True)
class FakeWatchPage:
    media_profiles: tuple[FakeProfile, ...]
    drm_enabled: bool = False


watch_parser.WatchPage = FakeWatchPage
sys.modules.setdefault("pbs_kids_watch_parser", watch_parser)

from pbs_kids_video_download import (  # noqa: E402
    classify_media_availability,
    record_media_availability,
)


class DrmStateTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            CREATE TABLE asset_media_availability_observation
            (
                availability_observation_id INTEGER PRIMARY KEY,
                asset_id INTEGER NOT NULL,
                watch_observation_id INTEGER NOT NULL UNIQUE,
                observed_at TEXT NOT NULL,
                availability_state TEXT NOT NULL
            );
            """
        )
        self.asset_id = 42
        self.next_watch_id = 1

    def observe(self, page: FakeWatchPage) -> tuple[str, bool]:
        state = classify_media_availability(page)
        warning = record_media_availability(
            self.connection,
            self.asset_id,
            self.next_watch_id,
            state,
        )
        self.next_watch_id += 1
        return state, warning

    def test_non_available_to_drm_to_non_drm_to_drm_to_non_available(self):
        unavailable = FakeWatchPage(())
        drm = FakeWatchPage((FakeProfile(True), FakeProfile(True)))
        non_drm = FakeWatchPage((FakeProfile(False), FakeProfile(True)))

        observed = [
            self.observe(unavailable),
            self.observe(drm),
            self.observe(non_drm),
            self.observe(drm),
            self.observe(unavailable),
        ]

        self.assertEqual(
            observed,
            [
                ("unavailable", False),
                ("drm", True),
                ("non_drm", False),
                ("drm", True),
                ("unavailable", False),
            ],
        )
        states = [
            row[0]
            for row in self.connection.execute(
                """
                SELECT availability_state
                FROM asset_media_availability_observation
                ORDER BY availability_observation_id
                """
            )
        ]
        self.assertEqual(
            states,
            ["unavailable", "drm", "non_drm", "drm", "unavailable"],
        )

    def test_repeated_drm_warns_only_on_first_observation(self):
        drm = FakeWatchPage((FakeProfile(True),))
        self.assertEqual(self.observe(drm), ("drm", True))
        self.assertEqual(self.observe(drm), ("drm", False))

    def test_page_level_drm_overrides_unlicensed_profiles(self):
        page = FakeWatchPage((FakeProfile(False),), drm_enabled=True)
        self.assertEqual(classify_media_availability(page), "drm")

    def test_first_non_drm_observation_does_not_warn(self):
        page = FakeWatchPage((FakeProfile(False),))
        self.assertEqual(self.observe(page), ("non_drm", False))

    def test_first_unavailable_observation_does_not_warn(self):
        self.assertEqual(self.observe(FakeWatchPage(())), ("unavailable", False))


if __name__ == "__main__":
    unittest.main()
