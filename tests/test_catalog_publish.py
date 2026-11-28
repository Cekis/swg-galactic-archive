"""Verify failed SQLite publication can resume without regenerating the catalog."""

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from build_catalog import build
from catalog_db import Catalog, Writer
from catalog_publish import CatalogBuildError, build_lock, publish_catalog, staged_catalogs


def finish_catalog(writer, revision):
    return writer.finish({
        "source_key": "fixture", "revision": revision, "build_issue_count": 0,
        "locations": {}, "maps": {},
    })


class CatalogPublishTests(unittest.TestCase):
    def test_publication_retries_a_windows_sharing_error(self):
        error = PermissionError("Access is denied")
        error.winerror = 5
        candidate = Mock()
        candidate.replace.side_effect = (error, None)
        destination = Path("catalog.sqlite3")
        with patch("catalog_publish.time.sleep") as wait, redirect_stdout(StringIO()) as output:
            publish_catalog(candidate, destination)
        self.assertEqual(candidate.replace.call_count, 2)
        candidate.replace.assert_called_with(destination)
        wait.assert_called_once()
        self.assertIn("Retrying briefly", output.getvalue())

    def test_only_one_builder_can_hold_the_catalog_lock(self):
        with TemporaryDirectory() as work:
            destination = Path(work) / "catalog.sqlite3"
            with build_lock(destination):
                with self.assertRaisesRegex(CatalogBuildError, "Another Archive build"), build_lock(destination):
                    self.fail("The second builder acquired the lock")
            with build_lock(destination):
                self.assertTrue(destination.with_suffix(".sqlite3.lock").exists())

    @unittest.skipUnless(os.name == "nt", "Windows holds open SQLite files against replacement")
    def test_open_reader_preserves_the_build_for_the_next_start(self):
        with TemporaryDirectory() as work:
            database = Path(work).resolve() / "catalog.sqlite3"
            writer = Writer(database)
            try:
                finish_catalog(writer, "prior")
            finally:
                writer.connection.close()
            os.utime(database, (1_700_000_000, 1_700_000_000))

            profile = SimpleNamespace(key="fixture", clientdata=None)
            catalog = Catalog(database)
            with catalog.connect() as reader:
                reader.execute("SELECT COUNT(*) FROM metadata").fetchone()
                with (
                    patch("build_catalog.write_catalog", side_effect=lambda writer, *_: finish_catalog(writer, "next")),
                    patch("catalog_publish.RETRY_DELAYS", ()),
                ):
                    with self.assertRaisesRegex(CatalogBuildError, "completed catalog is saved.+--if-needed"):
                        build(database, profile)
                self.assertEqual(catalog.metadata()["revision"], "prior")
                (_, candidate), = staged_catalogs(database)
                self.assertEqual(Catalog(candidate).metadata()["revision"], "next")

            with patch("build_catalog.write_catalog") as regenerate, redirect_stdout(StringIO()) as output:
                build(database, profile, if_needed=True)
            regenerate.assert_not_called()
            self.assertIn("Published the previously completed catalog", output.getvalue())
            self.assertEqual(catalog.metadata()["revision"], "next")
            self.assertFalse(candidate.parent.exists())
