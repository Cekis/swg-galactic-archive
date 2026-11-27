from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zlib

from catalog_client_assets import ClientAssetPath, TOC_ENTRY, TOC_HEADER, TRE_ENTRY, TRE_HEADER, client_asset_root
from test_source_profiles import save


def client_tre(root, filename, assets):
    records, names, payload = bytearray(), bytearray(), bytearray()
    for path, data in assets.items():
        encoded = zlib.compress(data)
        records.extend(TRE_ENTRY.pack(0, len(data), TRE_HEADER.size + len(payload), 2, len(encoded), len(names)))
        payload.extend(encoded)
        names.extend(path.encode("ascii") + b"\0")
    packed_records, packed_names = zlib.compress(records), zlib.compress(names)
    header = TRE_HEADER.pack(b"EERT", b"5000", len(assets), TRE_HEADER.size + len(payload),
                             2, len(packed_records), 2, len(packed_names), len(names))
    save(root / filename, header + payload + packed_records + packed_names)


def client_toc(root, filename, path, data):
    name, tree = path.encode("ascii"), b"patch.tre\0"
    payload = zlib.compress(data)
    record = TOC_ENTRY.pack(2, 0, 0, 0, len(name), 8, len(data), len(payload))
    names = zlib.compress(name + b"\0")
    records = zlib.compress(record)
    header = TOC_HEADER.pack(b" COT", b"1000", 2, 2, 0, 0, 1, len(records), len(names),
                             len(name) + 1, 1, len(tree))
    save(root / filename, header + tree + records + names)
    save(root / "patch.tre", b"EERT6000" + payload)


class ClientAssetsTests(unittest.TestCase):
    def test_reads_toc_and_standalone_tre_in_configured_priority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            save(root / "client.cfg",
                 '[SharedFile]\n.include "archives.cfg"\nsearchPath_00_03=loose\nsearchAbsolute=0')
            save(root / "archives.cfg", "searchTree_00_01=base.tre\nsearchTOC_00_02=patch.toc")
            client_tre(root, "base.tre", {"texture/Planet.dds": b"old map", "appearance/test.apt": b"appearance"})
            client_toc(root, "patch.toc", "texture/Planet.dds", b"updated map")
            save(root / "loose/texture/local.dds", b"loose map")

            client = client_asset_root(root)
            self.assertIsInstance(client, ClientAssetPath)
            self.assertEqual((client / "texture/Planet.dds").read_bytes(), b"updated map")
            self.assertEqual((client / "appearance/test.apt").read_bytes(), b"appearance")
            self.assertEqual({entry.name for entry in (client / "texture").glob("*.dds")}, {"local.dds", "planet.dds"})
            self.assertEqual((client / "texture/local.dds").read_bytes(), b"loose map")
            original_signature = client.installation.signature
            save(root / "archives.cfg", "searchTree_00_01=base.tre\nsearchTOC_00_00=patch.toc")
            client = client_asset_root(root)
            self.assertNotEqual(client.installation.signature, original_signature)
            self.assertEqual((client / "texture/planet.dds").read_bytes(), b"old map")
            (root / "patch.tre").unlink()
            with self.assertRaisesRegex(ValueError, "Missing client archive.*patch.tre.*patch.toc"):
                client_asset_root(root)

    def test_extracted_root_keeps_ordinary_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            save(root / "sample/test.wav", b"audio")
            self.assertEqual(client_asset_root(root), root.resolve())


if __name__ == "__main__":
    unittest.main()
