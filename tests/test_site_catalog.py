"""Acceptance checks against the generated Star Wars Galaxies catalog."""

import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_db import Catalog
from catalog_models import ModelLibrary
from catalog_provenance import SourceCatalog
from catalog_resources import RESOURCE_TREE
from catalog_sources import DEFAULT_CLIENT, PROJECT, ROOT, SERVER_OBJECTS


class CatalogAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database = PROJECT / ".local/catalog.sqlite3"
        if not database.is_file():
            raise RuntimeError("Run build_catalog.py before testing the catalog.")
        cls.catalog = Catalog(database)

    def test_import_covers_every_quest_in_the_manifest(self):
        expected = json.loads((ROOT / "docs/quests/manifest.json").read_text(encoding="utf-8"))
        with self.catalog.connect() as connection:
            indexed = {row[0] for row in connection.execute("SELECT id FROM entity WHERE kind='quests'")}
        self.assertEqual(indexed, {record["id"] for record in expected["quests"]})

    def test_quest_start_at_wattos_junkshop_has_both_links(self):
        building = "tatooine/building/1255995"
        quest = "quest/tatooine_espa_leg_work"
        entry = self.catalog.entity("quests", quest)
        starts = {row["id"]: row["detail"] for row in entry["related"]["origin_sites"]["rows"]}
        self.assertEqual(starts[building]["start_index"], 0)
        self.assertAlmostEqual(starts[building]["coordinates"][0], -2893.45, places=1)
        reverse = self.catalog.links("locations", building, "quests", 1, 60)
        self.assertEqual([row["id"] for row in reverse["rows"]].count(quest), 1)
        landmark = self.catalog.entity("locations", "tatooine/landmark/1255995")
        self.assertIn(quest, {row["id"] for row in landmark["related"]["quests"]["rows"]})

    def test_browse_and_item_drop_order_apply_before_pagination(self):
        def first_two(listing, order, field):
            return [listing(sort=order, size=1, page=page)["rows"][0][field] for page in (1, 2)]

        ships = lambda **paging: self.catalog.list("ships", **paging)
        tiers = first_two(ships, "level-desc", "level")
        self.assertEqual(tiers, sorted(tiers, reverse=True))
        drops = lambda **paging: self.catalog.item_loot_npcs("col_lightsaber_1h_01_03", **paging)
        low = first_two(drops, "chance", "chance")
        high = first_two(drops, "chance-desc", "chance")
        self.assertLess(low[0], high[-1])
        self.assertEqual(low, sorted(low))
        self.assertEqual(high, sorted(high, reverse=True))

    def test_source_links_use_verified_whitengold_paths_and_local_recovery_lines(self):
        catalog = SourceCatalog(self.catalog.meta()["revision"])
        prefix = "dsrc/sku.0/sys.server/compiled/game/"
        script = prefix + "script/poi/interior_spawner/interior_spawner.script"
        public = catalog.github(script, 111)
        self.assertRegex(public, r"^https://github\.com/SWG-Source/whitengold/blob/[a-f0-9]{40}/")
        self.assertTrue(public.endswith(script + "#L111"))
        table = prefix + "datatables/mob/creatures.tab"
        self.assertTrue(catalog.github(table, 3).endswith(table))
        local = prefix + "datatables/buildout/tatooine/snapshot_functional_restore.tab"
        self.assertIsNone(catalog.github(local, 626))
        self.assertIn("7875692\t1189174\tobject/tangible/poi/spawnegg/interior_spawnegg.iff",
                      dict(catalog.excerpt(local, 626))[626])
        self.assertFalse(catalog.excerpt("dsrc/../README.md", 1))

    def test_quest_search_reward_objective_and_dialogue_are_linked(self):
        result = self.catalog.list("quests", query="purvis", scene="tatooine", min_level=5, max_level=5)
        ident = "quest/purvis_kill_zealots"
        self.assertIn(ident, {row["id"] for row in result["rows"]})
        quest = self.catalog.entity("quests", ident)
        self.assertEqual(quest["card"]["reward_credits"], [1650])
        self.assertEqual(quest["card"]["reward_experience"], [1353])
        self.assertTrue(quest["data"]["detail"]["steps"])
        self.assertTrue(quest["data"]["detail"]["description"]["text"])
        table = "conversation/tatooine_eisley_purvis"
        self.assertIn(table, {row["id"] for row in quest["related"]["dialogue"]["rows"]})
        found = self.catalog.dialogue(table, f"@{table}:s_68", "", 1, 10)["rows"]
        line = next(item for item in found if item["key"] == "s_68")
        self.assertEqual(line["code"], f"@{table}:s_68")
        self.assertTrue(line["text"])
        self.assertTrue(any(usage.get("caption") and usage["source"]["line"] for usage in line["usages"]))

    def test_dialogue_exposes_its_npc_when_the_line_filter_matches_nothing(self):
        table = "conversation/tatooine_espa_watto"
        result = self.catalog.dialogue(table, "__unmatched line filter__", "", 1, 10)
        self.assertFalse(result["rows"])
        linked = result["npcs"]
        self.assertEqual(linked["count"], 1)
        self.assertEqual([(row["kind"], row["id"], row["card"]["name"]) for row in linked["rows"]],
                         [("npcs", "watto", "Watto")])
        self.assertIn(table, {row["id"] for row in self.catalog.links("npcs", "watto", "dialogue", 1, 10)["rows"]})

    def test_dialogue_preserves_unresolved_codes_and_different_text(self):
        with self.catalog.connect() as connection:
            tables = [json.loads(row[0]) for row in connection.execute(
                "SELECT data FROM entity WHERE kind='conversations' AND id LIKE 'conversation/%'")]
        different = next(line for table in tables for line in table["lines"] if line["status"] == "different")
        missing = next(line for table in tables for line in table["lines"] if line["status"] == "missing")
        self.assertTrue(any(use["alignment"] == "different" for use in different["usages"] if "alignment" in use))
        self.assertTrue(different["text"])
        self.assertTrue(missing["code"].startswith("@conversation/"))
        self.assertIsNone(missing["text"])

    def test_npc_quest_tab_separates_series_entry_from_follow_up(self):
        npc = self.catalog.entity("npcs", "newbie_ranged_teacher_purvis_arrison")
        openers = {row["id"]: row["card"]["level"] for row in npc["related"]["series_starts"]["rows"]}
        continued = {row["id"] for row in npc["related"]["continuation_quests"]["rows"]}
        self.assertEqual(openers["quest/purvis_kill_zealots"], 5)
        self.assertNotIn("quest/purvis_kill_soldiers", openers)
        self.assertIn("quest/purvis_kill_soldiers", continued)

    def test_global_search_ranks_the_exact_npc_before_related_names(self):
        matches = self.catalog.list("npcs", query="Watto", size=5, sort="relevance")["rows"]
        self.assertEqual(matches[0]["id"], "watto")

    def test_resolved_cell_coordinates_and_game_map(self):
        npc = self.catalog.entity("npcs", "watto")
        point = next(mark for mark in npc["data"]["locations"] if mark.get("coordinates") and mark.get("cell"))
        self.assertAlmostEqual(point["coordinates"][0], -2893.453, places=2)
        self.assertAlmostEqual(point["coordinates"][2], 2428.822, places=2)
        self.assertTrue(point["local_coordinates"])
        self.assertEqual(point["cell"]["name"], "r2")
        self.assertEqual(self.catalog.meta()["maps"]["tatooine"]["image"], "/api/map/tatooine/image")

    def test_npc_links_to_named_weapon_options(self):
        npc = self.catalog.entity("npcs", "aakuan_anarchist")
        weapons = npc["related"]["weapons"]["rows"]
        self.assertTrue(weapons)
        item = self.catalog.entity("items", weapons[0]["id"])
        self.assertEqual(item["card"]["category"], "Weapon")
        self.assertGreater(item["related"]["equipment_npcs"]["count"], 0)

    def test_catalog_has_every_authored_schematic_and_resource_class(self):
        revision = self.catalog.meta()["revision"]
        schematic_source = (SERVER_OBJECTS / "draft_schematic").relative_to(ROOT).as_posix()
        paths = subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", revision, "--", schematic_source],
            cwd=ROOT, encoding="utf-8",
        ).splitlines()
        schematics = {path.removeprefix(schematic_source).removesuffix(".tpf") + ".iff"
                      for path in paths if path.endswith(".tpf")}
        raw = subprocess.check_output(
            ["git", "show", f"{revision}:{RESOURCE_TREE.relative_to(ROOT).as_posix()}"],
            cwd=ROOT, encoding="utf-8", errors="replace",
        )
        rows = [line.split("\t") for line in raw.splitlines()[2:] if line.strip()]
        ids = {row[1] for row in rows if len(row) > 1 and row[0].isdigit() and int(row[0]) > 0}
        with self.catalog.connect() as db:
            indexed_schematics = {row[0] for row in db.execute("SELECT id FROM entity WHERE kind='schematics'")}
            indexed_resources = {row[0] for row in db.execute("SELECT id FROM entity WHERE kind='resources'")}
        self.assertFalse({"object/draft_schematic" + path for path in schematics} - indexed_schematics)
        self.assertEqual(ids, indexed_resources)

    def test_crafting_pages_link_to_products_and_resource_requirements(self):
        ident = "object/draft_schematic/weapon/wookiee_knuckler.iff"
        recipe = self.catalog.entity("schematics", ident)
        native = recipe["data"]
        self.assertEqual(len(native["slots"]), 5)
        self.assertEqual(native["xp"][0]["amount"], 402)
        self.assertEqual(native["items_per_container"], 25)
        self.assertTrue(native["station"]["required"])
        output = native["outputs"][0]
        self.assertEqual(output["quantity"], 1)
        self.assertEqual(output["quantity_kind"], "prototype_objects")
        product = self.catalog.entity("items", output["catalog_id"])
        self.assertTrue(product["card"]["crafted"])
        self.assertIn(ident, {row["id"] for row in product["related"]["crafted_by"]["rows"]})
        self.assertEqual(self.catalog.list("items", query=output["catalog_id"], acquisition="crafted")["total"], 1)
        resources = {row["id"] for row in recipe["related"]["resources"]["rows"]}
        self.assertIn("steel", resources)
        related = self.catalog.links("resources", "steel", "schematics", 1, 1)
        self.assertGreater(related["total"], 0)

    def test_resource_page_uses_parent_sprite_and_expands_the_planet_list(self):
        resource = self.catalog.entity("resources", "aluminum_chromium")["data"]
        self.assertEqual(resource["parent"], "aluminum")
        self.assertEqual(resource["icon"]["sprite"], "appearance/ui_res_aluminum.spr")
        self.assertEqual(resource["icon"]["texture"], "texture/ui_res_aluminum.dds")
        self.assertIn("tatooine", {row["scene"] for row in resource["distribution"]})
        self.assertEqual(self.catalog.list("resources", query="Chromium Aluminum", scene="tatooine")["total"], 1)
        if DEFAULT_CLIENT.is_dir():
            model = ModelLibrary(DEFAULT_CLIENT)
            self.assertEqual(model.sprite_texture(resource["icon"]["sprite"])["texture"], resource["icon"]["texture"])

    def test_ship_links_use_native_component_and_chassis_ids(self):
        ship = self.catalog.entity("ships", "player_xwing")
        self.assertEqual(ship["card"]["level"], 3)
        self.assertEqual(len(ship["data"]["slots"]), 15)
        engine = next(slot for slot in ship["data"]["slots"] if slot["id"] == "engine")
        engine_item = self.catalog.entity("items", engine["catalog_id"])
        self.assertEqual(engine_item["card"]["category"], "Ship component")
        self.assertIn("player_xwing", {row["id"] for row in engine_item["related"]["ship_components"]["rows"]})
        self.assertEqual(ship["related"]["schematics"]["rows"][0]["id"], "object/draft_schematic/space/chassis/xwing.iff")
        variant = self.catalog.entity("ships", "player_yt1300_decorated_01")
        self.assertEqual(variant["related"]["chassis"]["rows"][0]["id"], "player_yt1300")

    def test_item_vendor_and_loot_filters_have_real_table_sources(self):
        ident = "object/tangible/ship/components/shield_generator/shd_unknown_holosteel_x1.iff"
        item = self.catalog.entity("items", ident)["data"]
        self.assertEqual({entry["kind"] for entry in item["acquisitions"]}, {"vendor", "loot"})
        for kind in ("vendor", "loot"):
            rows = self.catalog.list("items", query=ident, acquisition=kind)["rows"]
            self.assertEqual([row["id"] for row in rows], [ident])
            self.assertTrue(all(entry["source"].get("line") for entry in item["acquisitions"] if entry["kind"] == kind))

    def test_scripted_items_expose_storage_and_acquisition_proof(self):
        expected = {
            "object/intangible/buy_back/buy_back_control_device.iff": ("intangible", "datapad", "smuggler.java"),
            "item_empire_day_6yr_painting": ("physical", "inventory", "holiday.java"),
        }
        for ident, (form, storage, filename) in expected.items():
            with self.subTest(ident=ident):
                item = self.catalog.entity("items", ident)["data"]
                self.assertEqual(item["form"], form)
                self.assertGreater(item["script_destinations"][storage], 0)
                sources = [row["source"] for row in item["acquisitions"] if row["kind"] == "script"
                           and row["destination"] == storage and not row.get("development")]
                self.assertTrue(any(ref["path"].endswith("/" + filename) and ref["line"] for ref in sources))
                result = self.catalog.list("items", query=ident, acquisition="script", storage=storage)
                self.assertIn(ident, {row["id"] for row in result["rows"]})
        disk = self.catalog.entity("items", "col_dooku_datadisk_02_04")["data"]
        self.assertTrue(any(row.get("selection") == "possible loot roll"
                            and row["source"]["path"].endswith("/dummy_treasure_drum.java")
                            and row["table_source"]["path"].endswith("/collection_treasure_loot.tab")
                            for row in disk["acquisitions"]))

    def test_filtered_item_pages_keep_the_same_order_and_total(self):
        filters = {"acquisition": "script", "storage": "datapad"}
        first = self.catalog.list("items", page=1, size=5, **filters)
        second = self.catalog.list("items", page=2, size=5, **filters)
        together = self.catalog.list("items", page=1, size=10, **filters)
        self.assertEqual(second["page"], 2)
        self.assertEqual(second["total"], first["total"])
        self.assertEqual(first["rows"] + second["rows"], together["rows"])
        self.assertTrue(all(row["storage"]["datapad"] > 0 for row in together["rows"]))

    def test_item_loot_npcs_follow_ground_and_space_table_columns(self):
        cases = {
            "armor_assault_agi_lvl80_bicep_l_02_01": ("mand_bunker_blksun_assassin", "npc/mand_blksun_ext", "creatures.tab"),
            "object/tangible/ship/components/shield_generator/shd_unknown_holosteel_x1.iff":
                ("space/decimator_tier6", "imperial_tier5", "space_mobile.tab"),
        }
        for item, (npc, selector, definition) in cases.items():
            with self.subTest(item=item):
                acquisition = self.catalog.entity("items", item)["data"]["acquisitions"]
                loot_columns = {(source["source"]["path"], source["column"])
                                for source in acquisition if source["kind"] == "loot"}
                result = self.catalog.item_loot_npcs(item, size=1000)
                sources = next(row["matches"] for row in result["rows"] if row["id"] == npc)
                self.assertTrue(any(match["selector"] == selector for match in sources))
                for match in sources:
                    self.assertTrue(match["source"]["path"].endswith(definition))
                    self.assertIn((match["path"], match["column"]), loot_columns)

    def test_inventory_crosslinks_resolve_to_detail_pages(self):
        with self.catalog.connect() as db:
            missing = db.execute("""
                SELECT l.target_kind,l.target_id FROM link l
                LEFT JOIN entity e ON e.kind=l.target_kind AND e.id=l.target_id
                WHERE l.kind IN ('items','schematics','resources','ships') AND e.id IS NULL LIMIT 1
            """).fetchone()
        self.assertIsNone(missing)


if __name__ == "__main__":
    unittest.main()
