"""Tests for watch-page-aware dry-run availability reporting."""
from __future__ import annotations
import sqlite3
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

catalog_database = types.ModuleType("pbs_kids_catalog_db")
catalog_database.Settings = object
sys.modules.setdefault("pbs_kids_catalog_db", catalog_database)
watch_parser = types.ModuleType("pbs_kids_watch_parser")

@dataclass(frozen=True)
class FakeProfile:
    profile: str
    redirect_url: str
    has_drm_license: bool = False

@dataclass(frozen=True)
class FakeWatchPage:
    media_manager_guid: str
    media_profiles: tuple[FakeProfile, ...]
    drm_enabled: bool = False

def choose_media_profile(page, preferred_profiles):
    if page.drm_enabled:
        return None
    for preferred in preferred_profiles:
        for profile in page.media_profiles:
            if profile.profile == preferred and not profile.has_drm_license:
                return profile
    return next((p for p in page.media_profiles if not p.has_drm_license), None)

watch_parser.WatchPage = FakeWatchPage
watch_parser.choose_media_profile = choose_media_profile
watch_parser.parse_watch_page = lambda html: html
sys.modules.setdefault("pbs_kids_watch_parser", watch_parser)
from pbs_kids_video_download import DownloadSettings, download_candidate  # noqa: E402

class DryRunAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = DownloadSettings(root / "media", root / "staging", ("fullEpisode",), ("mp4", "hls"), "yt-dlp", "ffprobe", 0, 5, False)
        self.connection = sqlite3.connect(":memory:")
        self.row = {"asset_id": 42, "title": "Episode One", "show_key": "example-show", "video_type": "fullEpisode", "watch_url": "https://example.invalid/watch", "media_manager_guid": "guid-42"}

    def tearDown(self):
        self.connection.close(); self.temporary.cleanup()

    def run_dry(self, page, drm_is_new=False, expect_availability=True):
        with patch(
            "pbs_kids_video_download.fetch_watch_page",
            return_value=("<html></html>", 200),
        ), patch(
            "pbs_kids_video_download.watch_parser.parse_watch_page",
            return_value=page,
        ), patch(
            "pbs_kids_video_download.record_watch_observation",
            return_value=10,
        ) as watch, patch(
            "pbs_kids_video_download.record_media_availability",
            return_value=drm_is_new,
        ) as availability:
            result = download_candidate(self.connection, self.row, object(), self.settings, dry_run=True)
        watch.assert_called_once()
        if expect_availability:
            availability.assert_called_once()
        else:
            availability.assert_not_called()
        return result

    def test_non_drm_stream_reports_would_download(self):
        result = self.run_dry(FakeWatchPage("guid-42", (FakeProfile("mp4", "https://example.invalid/video.mp4"),)))
        self.assertEqual(result["status"], "would-download"); self.assertEqual(result["availability_state"], "non_drm"); self.assertEqual(result["profile"], "mp4")

    def test_new_drm_condition_reports_would_skip_with_warning(self):
        result = self.run_dry(FakeWatchPage("guid-42", (FakeProfile("hls", "https://example.invalid/video.m3u8", True),)), True)
        self.assertEqual(result["status"], "would-skip"); self.assertTrue(result["drm_warning"]); self.assertIn("DRM-protected", result["error"])

    def test_repeated_drm_reports_would_skip_without_warning(self):
        result = self.run_dry(FakeWatchPage("guid-42", (FakeProfile("hls", "https://example.invalid/video.m3u8", True),)))
        self.assertEqual(result["status"], "would-skip"); self.assertFalse(result["drm_warning"]); self.assertIsNone(result["error"])

    def test_no_streams_report_would_skip(self):
        result = self.run_dry(FakeWatchPage("guid-42", ()))
        self.assertEqual(result["status"], "would-skip"); self.assertEqual(result["availability_state"], "unavailable")

    def test_guid_mismatch_still_fails(self):
        result = self.run_dry(
            FakeWatchPage("different-guid", ()),
            expect_availability=False,
        )
        self.assertEqual(result["status"], "failed"); self.assertIn("does not match", result["error"])

if __name__ == "__main__":
    unittest.main()
