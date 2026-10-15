from pathlib import Path
import unittest

import odd_squad_catalog as osc


FIXTURE = Path(__file__).with_name("odd-squad.html")


class CatalogParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = osc.parse_catalog_html(FIXTURE.read_text(encoding="utf-8"))

    def test_fixture_counts(self):
        self.assertEqual(self.catalog.metadata.total_occurrences, 94)
        self.assertEqual(self.catalog.metadata.unique_videos, 86)

    def test_default_full_episode_filter_excludes_asl(self):
        selected = osc.filter_videos(
            self.catalog.videos,
            video_types={"fullEpisode"},
            include_asl=False,
            collections=None,
        )
        self.assertEqual(len(selected), 14)
        self.assertFalse(any("(ASL)" in video.title for video in selected))

    def test_include_asl_returns_all_full_episodes(self):
        selected = osc.filter_videos(
            self.catalog.videos,
            video_types={"fullEpisode"},
            include_asl=True,
            collections=None,
        )
        self.assertEqual(len(selected), 24)

    def test_watch_url(self):
        video = next(
            v for v in self.catalog.videos
            if v.slug == "best-seats-in-the-house-agent-obfusco"
        )
        self.assertEqual(
            video.preferred_watch_url,
            "https://pbskids.org/videos/watch/odd-squad-full-episodes/1385810/"
            "best-seats-in-the-house-agent-obfusco/28590",
        )
        self.assertEqual(video.guid, "83f6dec7-bfed-42f8-9096-ba6124626728")

    def test_phase_two_caption_field_exists(self):
        self.assertTrue(all(video.captions == [] for video in self.catalog.videos))


if __name__ == "__main__":
    unittest.main()
