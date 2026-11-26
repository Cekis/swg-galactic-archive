"""Verify chassis identity, native links and the exterior preview path."""

import os
from pathlib import Path
import struct
import sys
import unittest


PROJECT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT))

from archive_config import legacy_asset, legacy_game
from catalog_models import ModelLibrary, chunks, portal_exterior
from catalog_ships import load_ships
from catalog_sources import ClientStrings, Files


class ShipCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clientdata = Path(os.environ.get("SWG_CLIENTDATA", legacy_asset("clientdata", "SWG_CLIENTDATA")))
        cls.strings = ClientStrings(cls.clientdata, legacy_asset("client-tools", "SWG_CLIENT_TOOLS"))
        game = legacy_game()
        if not (game / "docs/quests/manifest.json").is_file():
            raise unittest.SkipTest("The original ship source fixture is unavailable.")
        cls.ships = load_ships(Files(directory=game), cls.strings, cls.clientdata)

    def test_chassis_identity_and_variants_follow_server_ship_type(self):
        self.assertEqual(self.ships["player_corvette"]["availability"], "server-reference")
        self.assertEqual([variant["id"] for variant in self.ships["player_corvette"]["variants"]],
                         ["player_corellian_corvette"])
        self.assertEqual([variant["id"] for variant in self.ships["player_yt1300"]["variants"]],
                         ["player_yt1300_decorated_01"])
        self.assertEqual(self.ships["player_test_falcon"]["availability"], "server-reference")
        self.assertIsNone(self.ships["player_test_falcon"]["template"])
        self.assertNotIn("player_yt1300_decorated_01", self.ships)
        self.assertNotIn("xwing_tier10", self.ships)

    def test_xwing_native_chassis_and_crafted_mass_are_distinct(self):
        ship = self.ships["player_xwing"]
        if self.strings.name("space_ship", "player_xwing"):
            self.assertEqual(ship["name"], "X-Wing Fighter")
        self.assertEqual(ship["name_ref"]["code"], "@space_ship:player_xwing")
        self.assertEqual(ship["model_asset"], "appearance/xwing_body.apt")
        self.assertEqual(ship["attributes"]["chassis_tier"], 3)
        self.assertEqual(ship["attributes"]["default_spawn_mass_limit"], 10000)
        self.assertEqual(ship["attributes"]["broker_conversion_credits"], 50000)
        self.assertIn("cert_starships_xwing", ship["certifications"])
        reactor = next(slot for slot in ship["slots"] if slot["id"] == "reactor")
        self.assertEqual(reactor["compatibility"], ["rct_0"])
        self.assertEqual(reactor["hit_weight"], 10)
        self.assertTrue(reactor["targetable"])
        self.assertEqual(reactor["default_component"], "rct_generic")
        recipe = next(link for link in ship["relationships"] if link["kind"] == "schematic")
        self.assertEqual(recipe["item"], "object/draft_schematic/space/chassis/xwing.iff")
        self.assertEqual(recipe["product"], "object/tangible/ship/components/chassis/xwing_chassis_token.iff")
        self.assertEqual(recipe["attributes"], {
            "hit_points_min": 1500, "hit_points_max": 2200,
            "mass_limit_min": 97500, "mass_limit_max": 102500,
        })

    def test_script_and_static_deed_stats_are_source_backed(self):
        ship = self.ships["player_sorosuub_space_yacht"]
        scripted = next(link for link in ship["relationships"] if link["kind"] == "deed")
        self.assertEqual(scripted["attributes"], {"hit_points": 3000, "current_hit_points": 3000, "mass_limit": 100000})
        self.assertTrue(scripted["attribute_source"]["path"].endswith("yacht_deed.script"))
        wing = next(link for link in self.ships["player_vwing"]["relationships"] if link["kind"] == "deed")
        self.assertEqual(wing["static_item"], "item_tcg_loot_reward_series1_v_wing")
        self.assertEqual(wing["attributes"]["mass_limit"], 66625)
        self.assertEqual(wing["attributes"]["hit_points"], 1850)

    def test_external_ship_remains_reference_and_previews_native_geometry(self):
        if not (self.clientdata / "appearance/lod/a_havoc_nyms.lod").is_file():
            self.skipTest("Point SWG_CLIENTDATA at the supplied ship assets.")
        ship = self.ships["player_havoc"]
        self.assertEqual(ship["availability"], "client-reference")
        self.assertEqual(ship["template"], "object/ship/player/shared_player_havoc.iff")
        self.assertFalse(ship["slots"])
        self.assertFalse(ship["deeds"])
        model = ModelLibrary(self.clientdata).get(ship["model_asset"])
        self.assertEqual(model["status"], "available")
        self.assertGreater(model["triangles"], 1000)
        self.assertTrue(all(part["asset"] == "appearance/mesh/a_havoc_nyms_l0.msh" for part in model["meshes"]))

    def test_portal_uses_the_native_exterior_cell(self):
        def block(tag, data):
            return struct.pack(">4sI", tag, len(data)) + data

        def form(tag, data):
            return block(b"FORM", tag + data)

        def cell(name, appearance):
            return form(b"CELL", form(b"0005", block(b"DATA", b"\0" * 5 + name + b"\0" + appearance + b"\0\0")))

        cells = form(b"CELS", cell(b"r0", b"appearance/outer.apt") + cell(b"r1", b"appearance/interior.apt"))
        root = next(chunks(memoryview(form(b"PRTO", form(b"0004", cells)))))
        self.assertEqual(portal_exterior(root), "appearance/outer.apt")


if __name__ == "__main__":
    unittest.main()
