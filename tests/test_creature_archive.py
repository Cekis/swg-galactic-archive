"""Creature spawn, loot and appearance checks against the local archive."""

from fractions import Fraction
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_db import Catalog
from catalog_drops import DropTables, _hit, gate_probability
from catalog_models import ModelLibrary
from archive_config import legacy_asset
from catalog_sources import PROJECT, SERVER
from archive_quests.manifest_sources import read_tab


class TableFiles:
    def __init__(self, files):
        self.files = {SERVER / path: data for path, data in files.items()}

    def exists(self, path):
        return path in self.files

    def text(self, path):
        return self.files[path]

    def tab(self, path):
        return read_tab(path, self.text(path))


class DropProbabilityTests(unittest.TestCase):
    def test_duplicate_rows_add_their_full_uniform_share(self):
        items = ["repeated_item"] * 3 + [f"other_{i}" for i in range(17)]
        tables = DropTables(TableFiles({
            "loot/loot_types/example.tab": "strItems\ns\nroll\n",
            "loot/loot_items/roll.tab": "strItemType\ns[unused]\n" + "\n".join([*items, "unused", ""]),
        }))
        result = tables.distribution("ground", "example")
        self.assertEqual(result["repeated_item", 0][0], Fraction(3, 20))
        self.assertEqual(len(result["repeated_item", 0][1]), 4)
        self.assertEqual(result["other_0", 0][0], Fraction(1, 20))
        self.assertEqual(sum(chance for chance, _ in result.values()), 1)

    def test_nested_and_required_rolls_keep_separate_probabilities(self):
        tables = DropTables(TableFiles({
            "loot/loot_types/example.tab": "strItems\tstrRequiredItems\ns\ts\nchoice\tguaranteed\nchoice\nother\n",
            "loot/loot_items/choice.tab": "strItemType\ns\nloot_items/nested:column\nfiller\n",
            "loot/loot_items/other.tab": "strItemType\ns\nother_item\n",
            "loot/loot_items/nested.tab": "column\ns\ntarget\nother_item\n",
        }))
        self.assertEqual(tables.distribution("ground", "example")["target", 0][0], Fraction(1, 6))
        self.assertEqual(tables.distribution("required", "example")["guaranteed", 0][0], 1)
        gate = gate_probability(50, 99)
        self.assertEqual(gate, 49 / 99)
        self.assertAlmostEqual(_hit(gate / 6, 3), 1 - (1 - gate / 6) ** 3)
        self.assertEqual(_hit(1, 0), 0)


class CreatureArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database = PROJECT / ".local/catalog.sqlite3"
        if not database.is_file():
            raise unittest.SkipTest("Build the Galactic Archive index first.")
        cls.catalog = Catalog(database)

    def test_heroic_table_and_planet_pools_have_distinct_spawn_types(self):
        result = self.catalog.placements("npcs", "heroic_axkva_nandina", spawn_type="Heroic encounter table")
        self.assertGreater(result["total"], 0)
        row = next(row for row in result["rows"] if row["coordinates"])
        self.assertEqual((row["scene"], row["cell"], row["scope"]), ("dungeon1", "r2", "scripted"))
        self.assertTrue(row["source"]["path"].endswith("/spawning/heroic/heroic_axkva_min.tab"))
        self.assertEqual(self.catalog.placement(row["id"])["marker"]["coordinates"], row["coordinates"])
        region = self.catalog.placements("npcs", "bageraset", scene="corellia", spawn_type="Region lair or herd")
        row = next(row for row in region["rows"] if row["scope"] == "region")
        self.assertIsNone(row["coordinates"])
        self.assertTrue(row["location_id"].startswith("corellia/region/"))
        self.assertTrue(row["region_source"]["path"].endswith("/region/region_corellia.tab"))

    def test_script_created_cantina_npc_and_space_squad(self):
        result = self.catalog.placements("npcs", "wuher", scene="tatooine", spawn_type="Scripted creature spawn")
        row = next(row for row in result["rows"] if row["cell"] == "cantina" and row["coordinates"])
        self.assertTrue(row["map_location"].startswith("tatooine/building/"))
        self.assertTrue(row["source"]["path"].endswith("/mos_eisley/masterspawner.script"))
        result = self.catalog.placements("npcs", "space/imp_tie_fighter_tier1", spawn_type="Space squad spawner")
        self.assertGreater(result["total"], 0)
        self.assertTrue(result["rows"][0]["source"]["path"].endswith(".tab"))

    def test_authored_loot_has_linked_items_and_explainable_numeric_chances(self):
        entity = self.catalog.entity("npcs", "tusken_executioner")
        self.assertTrue(entity["has_loot"])
        result = self.catalog.npc_drops("tusken_executioner")
        drop = next(row for row in result["rows"] if row["name"] and 0 < (row["chance"] or 0) <= 1)
        self.assertTrue(self.catalog.entity("items", drop["id"]))
        self.assertTrue(drop["details"][0]["sources"])
        self.assertEqual(self.catalog.npc_drops("tusken_executioner", "no_such_drop_in_this_catalog")["total"], 0)

    def test_item_loot_uses_the_same_relic_probability_as_the_creature(self):
        item = "col_lightsaber_1h_01_03"
        npc = "nightsister_initiate"
        entity = self.catalog.entity("items", item)
        self.assertEqual(entity["card"]["name"], "A Jedi Relic")
        self.assertTrue(entity["has_loot"])
        self.assertTrue(any(row["kind"] == "loot" and row["table"] == "col_5th_gen_1h_lightsaber"
                            for row in entity["data"]["acquisitions"]))
        sources = self.catalog.item_loot_npcs(item, size=30)
        by_npc = {row["id"]: row for row in sources["rows"]}
        self.assertEqual(len(by_npc), sources["total"])
        self.assertIn("dark_jedi_knight", by_npc)
        self.assertIn("spider_nightsister_elder", by_npc)
        reverse = by_npc[npc]
        forward, = self.catalog.npc_drops(npc, "col_lightsaber_1h_01_03")["rows"]
        self.assertAlmostEqual(reverse["chance"], 79 / 1683)
        self.assertEqual((reverse["chance"], reverse["details"]), (forward["chance"], forward["details"]))
        self.assertTrue(any(proof["path"].endswith("/treasure/col_5th_gen_1h_lightsaber.tab")
                            for proof in reverse["details"][0]["sources"]))
        filtered = self.catalog.item_loot_npcs(item, query="nightsister initiate")
        self.assertEqual([row["id"] for row in filtered["rows"]], [npc])

    def test_holocrons_with_the_same_name_keep_distinct_ids_and_roll_weights(self):
        npc = "dark_jedi_knight"
        result = self.catalog.npc_drops(npc, "strange sith holocron 3/5")
        by_id = {row["id"]: row for row in result["rows"]}
        row_counts = {"item_collection_sith_holocron_01_03": 6, "item_collection_sith_holocron_02_03": 7}
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["rows"]), len(by_id))
        self.assertEqual(by_id.keys(), row_counts.keys())
        for ident, copies in row_counts.items():
            with self.subTest(item=ident):
                row = by_id[ident]
                detail, = row["details"]
                self.assertEqual(row["name"], "Strange Sith Holocron 3/5")
                self.assertEqual(len(detail["sources"]), copies)
                self.assertAlmostEqual(detail["table_chance"], copies / (39 * 4))
                self.assertAlmostEqual(row["chance"], .05 * copies / (39 * 4))
                match, = self.catalog.npc_drops(npc, ident)["rows"]
                self.assertEqual(match["id"], ident)
                reverse, = self.catalog.item_loot_npcs(ident, query=npc)["rows"]
                self.assertEqual(reverse["id"], npc)
                self.assertEqual(reverse["chance"], row["chance"])

    def test_relic_chest_and_pickpocket_probabilities_keep_their_conditions(self):
        sources = self.catalog.item_loot_npcs("col_lightsaber_1h_01_03")["other_sources"]
        highest_map = [entry for entry in sources if entry["name"] == "Treasure map chest" and entry["level"] == 81]
        probability = 9 / 140
        misses = (1 - probability) ** 6
        self.assertEqual([row["order"] for row in highest_map], [0, 1, 2])
        for row, expected in zip(highest_map, [1 - misses, 1 - misses * (1 - .75 * probability),
                                               1 - misses * (1 - .80 * probability) ** 2]):
            self.assertAlmostEqual(row["chance"], expected)
        pickpocket, = [entry for entry in sources if entry["name"] == "Rare pickpocket loot"]
        self.assertIn("Per rare loot roll", pickpocket["condition"])
        self.assertAlmostEqual(pickpocket["chance"], probability)

    def test_entity_with_only_a_conditional_corpse_drop_displays_its_source(self):
        source, = self.catalog.item_loot_npcs("object/tangible/loot/quest/tusken_head.iff",
                                              query="Tusken Executioner")["rows"]
        self.assertEqual(source["id"], "tusken_executioner")
        self.assertIsNone(source["chance"])
        self.assertEqual(source["details"][0]["conditional_probability"], 1)

    def test_model_table_and_bounds_recovery_use_supplied_native_geometry(self):
        if not legacy_asset("clientdata", "SWG_CLIENTDATA").is_dir():
            self.skipTest("Client assets are not available.")
        models = ModelLibrary(legacy_asset("clientdata", "SWG_CLIENTDATA"), legacy_asset("client-tools", "SWG_CLIENT_TOOLS"))
        for ident in ("dark_jedi_sentinel", "bol", "space/bestine_pirate"):
            with self.subTest(ident=ident):
                profile = self.catalog.entity("npcs", ident)["data"]
                result = models.get(profile["model_asset"])
                self.assertEqual(result["status"], "available", result.get("message"))
                self.assertGreater(result["triangles"], 0)
                if ident == "bol":
                    self.assertTrue(result["recovered_geometry"].endswith(".mgn"))
                if ident.startswith("space/"):
                    self.assertEqual(profile["model_selection"], "Pilot communications appearance")
