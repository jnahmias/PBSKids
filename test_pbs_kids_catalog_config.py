from pathlib import Path
import tempfile, unittest
import pbs_kids_catalog_db as app


class ConfigTests(unittest.TestCase):
    def test_defaults_without_file(self):
        with tempfile.TemporaryDirectory() as d:
            old = Path.cwd()
            try:
                import os

                os.chdir(d)
                s, rest = app.load_settings(["init"])
                self.assertEqual(s.config_path, Path(d) / "pbs_kids_catalog.ini")
                self.assertEqual(s.database, Path(d) / "pbs-kids-catalog.sqlite3")
                self.assertIsNone(s.snapshot_root)
                self.assertEqual(rest, ["init"])
            finally:
                os.chdir(old)

    def test_explicit_config_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "custom.ini"
            p.write_text(
                "[application]\ndatabase=db/catalog.sqlite3\nsnapshot_root=snaps\nlog_level=DEBUG\n[http]\ntimeout_seconds=12.5\n[sqlite]\nbusy_timeout_ms=7000\n"
            )
            s, rest = app.load_settings(["--config", str(p), "init"])
            self.assertEqual(s.database, Path(d) / "db/catalog.sqlite3")
            self.assertEqual(s.snapshot_root, Path(d) / "snaps")
            self.assertEqual(s.log_level, "DEBUG")
            self.assertEqual(s.timeout_seconds, 12.5)
            self.assertEqual(s.busy_timeout_ms, 7000)
            self.assertEqual(rest, ["init"])

    def test_missing_explicit_config_fails(self):
        with self.assertRaises(ValueError):
            app.load_settings(["--config", "/definitely/missing.ini", "init"])


if __name__ == "__main__":
    unittest.main()
