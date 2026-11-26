from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_provenance import SourceCatalog
from deployment_assets import MANIFEST, missing_reference_assets, relative_path, verify_or_install


class DeploymentTests(unittest.TestCase):
    def test_github_dependencies_require_matching_content_and_can_be_reused(self):
        revision = "1" * 40
        relative = "fixture.msh"
        path = "clientdata/appearance/mesh/fixture.msh"
        raw = b"original native asset"
        manifest = {"format": 1, "mode": "github", "assets": [
            {"path": path, "bytes": len(raw), "sha256": sha256(raw).hexdigest(), "bundled": False,
             "github": {"repository": "SWG-Source/mesh", "revision": revision, "path": relative}},
        ]}
        with TemporaryDirectory() as work:
            root = Path(work).resolve()
            (root / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            snapshot = root / f"mesh-{revision}.zip"
            self.assertTrue(missing_reference_assets(root))
            with ZipFile(snapshot, "w") as archive:
                archive.writestr(f"mesh-{revision}/{relative}", raw[::-1])
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                verify_or_install(root, root)
            self.assertFalse((root / path).exists())
            with ZipFile(snapshot, "w") as archive:
                archive.writestr(f"mesh-{revision}/{relative}", raw)
            self.assertEqual(verify_or_install(root, root), (1, 1))
            self.assertFalse(missing_reference_assets(root))
            self.assertEqual((root / path).read_bytes(), raw)
            self.assertEqual(verify_or_install(root), (1, 0))

    def test_portable_source_excerpts_come_from_the_pinned_text_archive(self):
        revision = "2" * 40
        path = "dsrc/sku.0/fallback.tab"
        with TemporaryDirectory() as work:
            root = Path(work)
            with ZipFile(root / "local_source_text.zip", "w") as archive:
                archive.comment = revision.encode("ascii")
                archive.writestr(path, b"source line 1\nsource line 2\n")
            index = root / "upstream_source_index.json.gz"
            self.assertEqual(SourceCatalog(revision, index).excerpt(path, 2)[1], (2, "source line 2"))
            self.assertEqual(SourceCatalog("3" * 40, index).excerpt(path), ())

    def test_archive_asset_paths_cannot_escape_the_package(self):
        for path in ("../escape", "/absolute", "clientdata/../escape", r"clientdata\escape", "clientdata/F:/escape"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                relative_path(path)


if __name__ == "__main__":
    unittest.main()
