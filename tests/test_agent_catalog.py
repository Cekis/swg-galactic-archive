"""Regression checks for name variants, ranked probabilities, and portable context."""

from http.client import HTTPConnection
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_config import ArchiveProfile
from catalog_agent import export_catalog, loot_ranking
from catalog_db import Catalog, Writer, packed
from catalog_drops import DROP_SCHEMA
from catalog_loot import NPC_LOOT_SCHEMA
from serve import ArchiveServer


GENERIC = "object/tangible/jedi/holocron.iff"
HIGH = "item_collection_jedi_holocron_02_03"
LOW = "item_collection_jedi_holocron_01_03"
PROOF = {"path": "dsrc/sku.0/sys.server/compiled/game/datatables/loot/fixture.tab", "line": 3}
NPCS = ("drifter_one", "drifter_two", "condition_only", "below_gate")


def build_fixture(database):
    writer = Writer(database)
    try:
        for item, name in ((GENERIC, "A Jedi Holocron"), (HIGH, "Strange Jedi Holocron 3/5"),
                           (LOW, "Strange Jedi Holocron 3/5")):
            writer.entity("items", item, name, {"id": item, "acquisitions": [], "sources": [PROOF]},
                          category="Collection item")
        for ident, level in zip(NPCS, (21, 35, 26, 5)):
            writer.entity("npcs", ident, ident.replace("_", " ").title(),
                          {"id": ident, "sources": [PROOF]}, level=level)
        dialogue = {"lines": [{"key": "s_1", "code": "@conversation/fixture:s_1",
                               "text": "Return to the archive.", "status": "aligned", "usages": []}]}
        writer.entity("conversations", "conversation/fixture", "Archive Greeting", dialogue)
        writer.link("conversations", "conversation/fixture", "npcs", "npcs", NPCS[0], {"source": PROOF})

        db = writer.connection
        db.executescript(DROP_SCHEMA)
        db.execute(NPC_LOOT_SCHEMA)
        evidence = packed({"selector": "fixture", "sources": [PROOF]})
        db.executemany("INSERT INTO loot_pool VALUES (?,?,?,?,?)", (
            ("fixed", HIGH, 10, 0.4, evidence),
            ("fixed", LOW, 10, 0.1, evidence),
            ("conditional", HIGH, 0, 0.95, evidence),
        ))
        db.executemany("INSERT INTO npc_drop_profile VALUES (?,?,?,?,?,?)",
                       ((npc, "ground", "fixed", 1, 0.5, packed({"rule": PROOF}))
                        for npc in (NPCS[0], NPCS[1], NPCS[3])))
        db.execute("INSERT INTO npc_drop_profile VALUES (?,?,?,?,?,?)",
                   (NPCS[2], "script", "conditional", 1, 1,
                    packed({"condition": "Only after the encounter unlocks", "conditional_probability": 0.95})))
        db.execute("INSERT INTO item_loot_source VALUES (?,?,?)",
                   (HIGH, "treasure", packed({"name": "Treasure chest", "order": 1, "chance": 0.95})))
        writer.finish({"revision": "a" * 40, "locations": {}, "maps": {}, "resource_icons": {}})
    finally:
        writer.connection.close()


class AgentCatalogTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.catalog = Catalog(self.root / "catalog.sqlite3")
        build_fixture(self.catalog.path)

    def test_drop_lookup_keeps_same_name_ids_conditions_and_ties_separate(self):
        answer = loot_ranking(self.catalog, query="the jedi holocron", limit=1)
        self.assertEqual(answer["selection"], "related_variants")
        self.assertEqual([item["id"] for item in answer["exact_items"]], [GENERIC])
        self.assertEqual({item["id"] for item in answer["related_items"]}, {HIGH, LOW})
        self.assertEqual([winner["item_id"] for winner in answer["highest"]], [HIGH])
        best = answer["highest"][0]
        self.assertEqual(best["tied_npcs"], 2)
        self.assertEqual(best["npc"]["chance_percent"], 20.0)
        self.assertEqual(best["npc"]["url"], "/npcs/drifter_one")
        self.assertIn("sort=chance-desc", best["evidence"])
        highest = next(item["ranking"] for item in answer["related_items"] if item["id"] == HIGH)
        self.assertEqual(highest["npc_sources"], 4)
        self.assertEqual(highest["unranked_npcs"], 1)
        self.assertEqual(highest["zero_chance_npcs"], 1)
        self.assertEqual(highest["other_sources"], 1)
        self.assertEqual(len(highest["rows"]), 1)
        self.assertEqual(self.catalog.item_loot_npcs(HIGH, size=1)["rows"][0]["chance"], best["npc"]["chance"])

        variant = loot_ranking(self.catalog, query="Strange Jedi Holocron 3/5")
        self.assertEqual(variant["selection"], "exact_name")
        self.assertEqual(variant["exact_count"], 2)
        exact = loot_ranking(self.catalog, item_id=GENERIC)
        self.assertEqual(exact["selection"], "item_id")
        self.assertEqual(exact["highest"], [])
        self.assertEqual(exact["related_count"], 0)

    def test_jsonl_export_preserves_dialogue_evidence_and_existing_zip_on_failure(self):
        output = self.root / "agent-data.zip"
        result = export_catalog(self.catalog, output)
        self.assertEqual(result["files"]["names.jsonl"], 8)
        with ZipFile(output) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            self.assertEqual(manifest["snapshot"]["source_revision"], "a" * 40)
            self.assertEqual(manifest["files"]["entities/items.jsonl"]["records"], 3)
            self.assertEqual(manifest["unavailable_tables"], [])
            conversation = json.loads(bundle.read("entities/conversations.jsonl"))
            self.assertEqual(conversation["data"]["lines"][0]["text"], "Return to the archive.")
            self.assertEqual(json.loads(bundle.read("link.jsonl"))["detail"]["source"], PROOF)
            self.assertTrue(all("sources" in row["data"] for row in
                                map(json.loads, bundle.read("loot_pool.jsonl").splitlines())))
            self.assertIn(b"names.jsonl", bundle.read("llms.txt"))
        published = output.read_bytes()
        with patch("catalog_agent.write_jsonl", side_effect=OSError("interrupted export")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                export_catalog(self.catalog, output)
        self.assertEqual(output.read_bytes(), published)
        self.assertEqual(list(self.root.glob("*.pending")), [])

    def test_offline_queries_read_detail_relationships_and_dialogue(self):
        script = Path(__file__).resolve().parents[1] / "catalog_agent.py"

        def run(*args):
            result = subprocess.run([sys.executable, "-B", str(script), "--database", str(self.catalog.path), *args],
                                    capture_output=True, encoding="utf-8", text=True, check=True)
            return json.loads(result.stdout)

        records = run("list", "conversations", "--query", "Greeting", "--size", "1")
        self.assertEqual(records["rows"][0]["id"], "conversation/fixture")
        links = run("related", "conversations", "conversation/fixture", "npcs")
        self.assertEqual(links["rows"][0]["id"], NPCS[0])
        lines = run("dialogue", "conversation/fixture", "--query", "return")
        self.assertEqual(lines["rows"][0]["code"], "@conversation/fixture:s_1")

    def test_read_only_http_discovery_and_loot_routes(self):
        profile = ArchiveProfile({}, self.root)
        with ArchiveServer(("127.0.0.1", 0), self.catalog, profile) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                try:
                    def get(route):
                        connection.request("GET", route)
                        response = connection.getresponse()
                        return response.status, response.read()

                    status, index = get("/")
                    self.assertEqual(status, 200)
                    self.assertIn(b'rel="describedby" href="/llms.txt"', index)
                    status, guide = get("/llms.txt")
                    self.assertEqual(status, 200)
                    self.assertIn(b"/api/agent/manifest", guide)
                    status, data = get("/api/agent/manifest")
                    self.assertEqual(status, 200)
                    manifest = json.loads(data)
                    self.assertEqual(manifest["counts"]["items"], 3)
                    self.assertEqual(manifest["routes"]["dialogue"].split("?")[0], "/api/dialogue")
                    for kind in manifest["collections"]:
                        self.assertIn(f"/api/list?type={kind}".encode(), guide)
                    status, data = get("/api/agent/loot?" + urlencode({"item": HIGH, "size": 1}))
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(data)["highest"][0]["tied_npcs"], 2)
                    self.assertEqual(get("/api/agent/loot")[0], 400)
                finally:
                    connection.close()
            finally:
                server.shutdown()
                thread.join(5)


if __name__ == "__main__":
    unittest.main()
