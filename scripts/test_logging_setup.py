"""Testes isolados da rotacao e retencao de logs (sem PyQt6)."""

import gzip
import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logging_setup import (
    AgeAndSizeRotatingFileHandler,
    archive_legacy_log,
    cleanup_expired_logs,
)


class LoggingSetupTests(unittest.TestCase):
    def test_archive_legacy_log_preserves_content_and_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            base_dir = Path(temporary_dir)
            archive_dir = base_dir / "logs" / "archive"
            legacy = base_dir / "app.log"
            legacy.write_text("diagnostico legado", encoding="utf-8")
            original_mtime = time.time() - (5 * 24 * 60 * 60)
            os.utime(legacy, (original_mtime, original_mtime))

            archived = archive_legacy_log(base_dir, archive_dir)

            self.assertIsNotNone(archived)
            self.assertFalse(legacy.exists())
            self.assertEqual(archived.read_text(encoding="utf-8"), "diagnostico legado")
            self.assertAlmostEqual(archived.stat().st_mtime, original_mtime, delta=2)

    def test_cleanup_removes_only_expired_managed_logs(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_dir = Path(temporary_dir)
            old_managed = archive_dir / "voximago-20260101-000000-000000.log.gz"
            recent_managed = archive_dir / "voximago-20260220-000000-000000.log.gz"
            unrelated = archive_dir / "nao-remover.txt"
            for path in (old_managed, recent_managed, unrelated):
                path.write_text("x", encoding="utf-8")

            now = time.time()
            old_mtime = now - (46 * 24 * 60 * 60)
            recent_mtime = now - (44 * 24 * 60 * 60)
            os.utime(old_managed, (old_mtime, old_mtime))
            os.utime(recent_managed, (recent_mtime, recent_mtime))
            os.utime(unrelated, (old_mtime, old_mtime))

            removed = cleanup_expired_logs(archive_dir, retention_days=45, now=now)

            self.assertEqual(removed, [old_managed])
            self.assertFalse(old_managed.exists())
            self.assertTrue(recent_managed.exists())
            self.assertTrue(unrelated.exists())

    def test_handler_rotates_and_compresses_at_size_limit(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            base_dir = Path(temporary_dir)
            active = base_dir / "logs" / "voximago.log"
            archive_dir = base_dir / "logs" / "archive"
            active.parent.mkdir(parents=True)

            logger = logging.Logger("voximago-test", level=logging.INFO)
            logger.propagate = False
            handler = AgeAndSizeRotatingFileHandler(
                active,
                archive_dir,
                max_bytes=180,
                retention_days=45,
            )
            handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            logger.addHandler(handler)
            try:
                logger.info("primeiro %s", "a" * 110)
                logger.info("segundo %s", "b" * 110)
            finally:
                handler.close()

            archives = list(archive_dir.glob("voximago-*.log.gz"))
            self.assertEqual(len(archives), 1)
            with gzip.open(archives[0], "rt", encoding="utf-8") as archive_file:
                self.assertIn("primeiro", archive_file.read())
            self.assertIn("segundo", active.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
