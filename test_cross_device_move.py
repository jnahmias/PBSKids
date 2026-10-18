"""Regression test for the cross-filesystem download promotion helper."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pbs_kids_video_download import promote_download


class CrossDeviceMoveTests(unittest.TestCase):
    def test_cross_device_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temporary = root / "staging" / "download.mp4"
            destination = root / "media" / "episode.mp4"
            temporary.parent.mkdir()
            temporary.write_bytes(b"video-content")

            real_replace = os.replace
            call_count = 0

            def simulated_replace(source, target):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise OSError(errno.EXDEV, "Invalid cross-device link")
                return real_replace(source, target)

            with patch("pbs_kids_video_download.os.replace", simulated_replace):
                promote_download(temporary, destination)

            self.assertFalse(temporary.exists())
            self.assertEqual(destination.read_bytes(), b"video-content")


if __name__ == "__main__":
    unittest.main()
