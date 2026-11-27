import json
from http import HTTPStatus
from pathlib import Path
import shutil
import struct
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile
import zlib

from archive_config import ArchiveProfile, SourceRepository
from build_deployment import describe_assets, trace, write_package
from catalog_client_assets import ClientAssetPath, TOC_ENTRY, TOC_HEADER, TRE_ENTRY, TRE_HEADER, client_asset_root
from catalog_db import Catalog, Writer
from catalog_dialogue import DialogueIndex
from catalog_models import ModelLibrary
from catalog_sounds import SoundIndex
from serve import ArchiveHandler
from test_model_assets import iff_chunk, iff_form
from test_source_profiles import save


TABLE = "conversation/fixture"
TEMPLATE = "sound/greeting.snd"
SAMPLE = "sample/greeting.mp3"
ENGINE = "object/tangible/ship/components/engine/fixture.iff"
SCRIPT = Path("dsrc/sku.0/sys.server/compiled/game/script/conversation/fixture.script")


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


def native_string_table(*rows):
    columns = rows[0]
    return iff_form(b"DTII", iff_form(b"0001",
        iff_chunk(b"COLS", struct.pack("<I", len(columns)) + b"\0".join(columns) + b"\0"),
        iff_chunk(b"TYPE", b"s\0" * len(columns)),
        iff_chunk(b"ROWS", struct.pack("<I", len(rows) - 1) + b"".join(
            value + b"\0" for row in rows[1:] for value in row))))


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


    def test_dialogue_engine_playback_and_deployment_from_tre(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / "client"
            save(install / "client.cfg", "[SharedFile]\nsearchTree_00_01=game.tre")
            attachment = iff_chunk(b"XXXX", b"clientDataFile\0\x01clientdata/ship/engine.cdf\0")
            client_tre(install, "game.tre", {
                SAMPLE: b"\xff\xfbmock audio sample",
                TEMPLATE: iff_form(b"SD3D", iff_form(b"SD2D",
                    iff_chunk(b"0004", struct.pack("<I", 1) + SAMPLE.encode() + b"\0"))),
                "datatables/space/ship_chassis_fixture.iff": native_string_table(
                    (b"component", b"engine"), (b"engine_fixture", b"fixture:engine1")),
                "object/tangible/ship/attachment/engine/shared_fixture.iff":
                    iff_form(b"STOT", iff_form(b"0000", attachment)),
                "clientdata/ship/engine.cdf": iff_form(b"CLDF", iff_chunk(b"TEST", TEMPLATE.encode() + b"\0")),
            })
            profile = ArchiveProfile({"clientdata": SourceRepository(install)}, root / "output")
            profile.state.mkdir()
            index = SoundIndex(profile.clientdata, ModelLibrary(profile.clientdata))
            index.inventory()
            strings = SimpleNamespace(conversation_names=lambda: [TABLE],
                                      table=lambda _: ({"s_1": "Hello", "s_10": "Hello again", "s_2": "Goodbye"}, None))
            dialogue = DialogueIndex(strings, ["quest/fixture"])
            dialogue.scan_script("conversation.fixture", SCRIPT, """
public int cue(obj_id speaker) {
    play2dNonLoopingSound(speaker, "sound/greeting.snd");
    return 0;
}
public String reply(obj_id speaker, String response) {
    string_id message;
    if (response.equals("first")) {
        cue(speaker);
        message = new string_id("conversation/fixture", "s_1");
    } else if (response.equals("repeat")) {
        cue(speaker);
        message = new string_id("conversation/fixture", "s_10");
    } else {
        message = new string_id("conversation/fixture", "s_2");
    }
    return null;
}
""", "conversation_script", True)

            writer = Writer(profile.database)
            try:
                for kind, ident in (("quests", "quest/fixture"), ("npcs", "fixture")):
                    writer.entity(kind, ident, "Fixture", {})
                    writer.link(kind, ident, "dialogue", "conversations", TABLE)
                writer.entity("ships", "fixture", "Fixture ship", {})
                writer.entity("items", ENGINE, "Fixture engine", {})
                index.engines(writer, SimpleNamespace(tab=lambda path:
                    [{"name": "engine_fixture", "object_template": ENGINE}] if path.name == "ship_components.tab" else []))
                index.dialogue(writer, dialogue)
                index.publish(writer)
                entry = dialogue.conversation(TABLE)
                for line in entry["lines"]:
                    if cues := index.line_audio.get((TABLE, line["key"])):
                        line["sounds"] = cues
                writer.entity("conversations", TABLE, "Fixture conversation", entry)
                writer.finish({"revision": "fixture", "locations": {}, "maps": {}})
            finally:
                writer.connection.close()

            catalog = Catalog(profile.database)
            lines = catalog.dialogue(TABLE, "@conversation/fixture:s_1", "", 1, 20)["rows"]
            self.assertEqual([line["key"] for line in lines], ["s_1"])
            self.assertEqual(lines[0]["sounds"][0]["clip"], SAMPLE)
            self.assertNotIn("sounds", catalog.dialogue(TABLE, "s_2", "", 1, 20)["rows"][0])
            sound = catalog.entity("sounds", TEMPLATE)
            self.assertEqual(sound["data"]["playable"], [SAMPLE])
            self.assertEqual(sound["related"]["items"]["rows"][0]["id"], ENGINE)
            self.assertEqual(sound["related"]["ships"]["rows"][0]["id"], "fixture")
            self.assertEqual(sound["related"]["npcs"]["rows"][0]["id"], "fixture")
            self.assertEqual(sound["related"]["quests"]["rows"][0]["id"], "quest/fixture")
            evidence = sound["related"]["conversations"]["rows"][0]["detail"]["dialogue_lines"]
            self.assertEqual([line["code"] for line in evidence], ["@conversation/fixture:s_1", "@conversation/fixture:s_10"])

            handler = object.__new__(ArchiveHandler)
            handler.server = SimpleNamespace(audio=index.library)
            handler.headers = {"Range": "bytes=2-5"}
            handler.respond = Mock()
            handler.sound(SAMPLE)
            handler.respond.assert_called_once_with(b"mock", "audio/mpeg", HTTPStatus.PARTIAL_CONTENT,
                cache="private, max-age=3600", headers=[("Accept-Ranges", "bytes"), ("Content-Range", "bytes 2-5/19")])
            with patch("build_deployment.audit_in_parallel", return_value=({}, set())):
                identity, paths, _ = trace(catalog, profile)
            expected_file = "clientdata/" + SAMPLE
            self.assertEqual(set(paths), {expected_file})
            assets = describe_assets(paths, ())
            package = root / "fixture.zip"
            write_package(package, "embedded", paths, assets, profile.database, "checksum", identity, b"", {})
            with ZipFile(package) as deployment:
                self.assertEqual(deployment.read(expected_file), b"\xff\xfbmock audio sample")
                self.assertNotIn("game.tre", deployment.namelist())

            if node := shutil.which("node"):
                renderer = root / "renderer"
                save(renderer / "package.json", '{"type":"module"}')
                static = Path(__file__).resolve().parents[1] / "static"
                for name in ("views.js", "ui.js", "inventory-views.js"):
                    shutil.copyfile(static / name, renderer / name)
                script = """
import { readFileSync } from "node:fs";
import { dialogueLines, entityPage } from "./views.js";
const { sound, page } = JSON.parse(readFileSync(0, "utf8"));
console.log(dialogueLines(page, "", "", ""));
console.log(entityPage("sounds", sound, "overview", {}).html);
"""
                rendered = subprocess.run([node, "--input-type=module", "-e", script], cwd=renderer,
                    input=json.dumps({"sound": sound, "page": {"rows": lines, "total": 1, "page": 1, "size": 20}}),
                    capture_output=True, encoding="utf-8")
                self.assertEqual(rendered.returncode, 0, rendered.stderr)
                self.assertIn('<audio controls preload="none"', rendered.stdout)
                self.assertIn("?line=%40conversation%2Ffixture%3As_1", rendered.stdout)


if __name__ == "__main__":
    unittest.main()
