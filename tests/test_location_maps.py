"""Map coverage, clustering and the original client coordinate formats."""

from pathlib import Path
import sqlite3
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_db import Catalog
from catalog_map import MAP_INDEX, MapWriter, bounds_from_query, map_list, map_window
from catalog_sources import PROJECT
from client_map_objects import compile_area_rows, snapshot_points


class MapWindowTests(unittest.TestCase):
    def test_clusters_reveal_every_coincident_marker_and_respect_filters(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript(MAP_INDEX)
            markers = MapWriter(db)
            markers.add("tatooine", "npc", "Watto", [8, 4, 2])
            markers.add("tatooine", "quest_start", "Watto's Favor", [8, 4, 2])
            markers.add("tatooine", "npc", "Off the map", [21, 4, 2])
            markers.add("naboo", "npc", "A different planet", [8, 4, 2])
            markers.flush()
            layers = ("npc", "quest_start")
            result = map_window(db, "tatooine", layers, [0, 0, 10, 10], "watto", 4, 4)
            self.assertEqual(result["total"], 2)
            cluster = result["groups"][0]
            self.assertEqual(cluster["count"], 2)
            self.assertEqual(cluster["bounds"], [8, 2, 8, 2])
            first = map_list(db, "tatooine", layers, cluster["bounds"], "watto", 1, 1)
            second = map_list(db, "tatooine", layers, cluster["bounds"], "watto", 2, 1)
            self.assertEqual(first["total"], 2)
            self.assertEqual([first["rows"][0]["name"], second["rows"][0]["name"]], ["Watto", "Watto's Favor"])
            only_npc = map_window(db, "tatooine", ("npc",), [0, 0, 10, 10], "", 4, 4)
            self.assertEqual([entry["name"] for entry in only_npc["groups"]], ["Watto"])

    def test_query_clips_the_window_and_allows_degenerate_cluster_bounds_for_lists(self):
        chart = [-10, -10, 10, 10]
        self.assertEqual(bounds_from_query("-15,-5,5,15", chart), [-10, -5, 5, 10])
        self.assertEqual(bounds_from_query("2,3,2,3", chart, lists=True), [2, 3, 2, 3])
        for malformed in ("-10,nan,10,10", "1,2,3", "10,10,0,0", "0,0,0,0"):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                bounds_from_query(malformed, chart)

    def test_major_city_keeps_its_own_marker_at_low_zoom(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript(MAP_INDEX)
            markers = MapWriter(db)
            for name in ("Lost Dog", "Cantina Host"):
                markers.add("tatooine", "npc", name, [5, 0, 5])
            markers.add("tatooine", "city", "Mos Eisley", [5, None, 5])
            markers.flush()
            bounds = [0, 0, 10, 10]
            result = map_window(db, "tatooine", ("npc", "city"), bounds, "", 1, 1)
            self.assertEqual(result["total"], 3)
            self.assertEqual(result["groups"][0]["count"], 2)
            self.assertEqual(result["groups"][1]["name"], "Mos Eisley")
            self.assertEqual(map_window(db, "tatooine", ("npc",), bounds, "", 1, 1)["total"], 2)


class ClientCoordinateTests(unittest.TestCase):
    def test_compiled_buildout_applies_area_offset_and_parent_rotation(self):
        def row(crc, cell, position, rotation=(1, 0, 0, 0), portal=0):
            return dict(zip(("shared_template_crc", "cell_index", "px", "py", "pz",
                             "qw", "qx", "qy", "qz", "portal_layout_crc"),
                            (crc, cell, *position, *rotation, portal)))

        quarter_turn = (2 ** -.5, 0, 2 ** -.5, 0)
        rows = [row(1, 0, (10, 2, 20), quarter_turn, 91),
                row(2, 1, (0, 0, 0)), row(3, 1, (3, 1, 0))]
        points = dict(compile_area_rows(rows, {"area": "test", "x1": 100, "z1": -200}, 0,
                                        {1: "object/building/test.iff", 2: "object/cell/shared_cell.iff"}))
        self.assertEqual(points[1]["coordinates"], [110, 2, -180])
        self.assertEqual(points[3]["coordinates"], [110, 3, -183])
        self.assertEqual(points[3]["status"], "transformed_cell")

    def test_snapshot_children_inherit_transform_even_if_the_parent_is_deduplicated(self):
        def block(tag, data):
            return struct.pack(">4sI", tag, len(data)) + data

        def form(tag, *entries):
            return block(b"FORM", tag + b"".join(entries))

        def node(ident, parent, name, point, *children):
            data = struct.pack("<4i8fI", ident, parent, name, 0, 1, 0, 0, 0, *point, 100, 0)
            return form(b"NODE", form(b"0000", block(b"DATA", data), *children))

        tree = node(10, 0, 0, (9, 4, 20), node(12, 10, 0, (2, 0, 3)))
        names = block(b"OTNL", struct.pack("<I", 1) + b"object/static/shared_tree.iff\0")
        snapshot = form(b"WSNP", form(b"0001", form(b"NODS", tree), names))
        points = dict(snapshot_points(snapshot, {10}))
        self.assertEqual(list(points), [2])
        self.assertEqual(points[2]["coordinates"], [11, 4, 23])


class LocationMapAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog(PROJECT / ".local/catalog.sqlite3")

    def test_landmark_has_the_client_text_and_a_pin_linking_to_its_page(self):
        ident = "tatooine/poi/forttusken"
        place = self.catalog.entity("locations", ident)["data"]
        self.assertIn("Tusken Raider", place["description"]["text"])
        self.assertTrue(any(source.get("key") == "@clientpoi_d:forttusken" for source in place["description"]["sources"]))
        points = self.catalog.map_list("tatooine", ("landmark",), place["map"]["bounds"], "fort tusken", 1, 20)
        self.assertTrue(any(row["target"] == ["locations", ident] for row in points["rows"]))

    def test_region_geometry_is_queryable_without_its_center_in_view(self):
        window = [7000, 4540, 7510, 4620]
        regions = self.catalog.map_areas("tatooine", ("region_bounds", "no_build"), window, "Krayt Graveyard")
        area = next(row for row in regions if row["target"] == ["locations", "tatooine/region/122"])
        self.assertEqual(area["layer"], "no_build")
        self.assertEqual(area["extent"]["bounds"], [6560, 4096, 7488, 4608])
        self.assertIn("bones of hundreds", area["description"])
        self.assertEqual(area["source"]["line"], 122)
        points = self.catalog.map_list("tatooine", ("region",), [6560, 4096, 7488, 4608], "Krayt Graveyard", 1, 20)
        self.assertTrue(any(point.get("extent") == area["extent"] for point in points["rows"]))

    def test_circle_bounds_reject_corners_outside_the_radius(self):
        layers = ("no_build",)
        partial = [-1900, -3710, -1890, -3660]
        circle, = self.catalog.map_areas("tatooine", layers, partial, "Bestine Nobuild 1")
        self.assertEqual(circle["extent"]["values"], [-1218, -3688, 700])
        self.assertEqual(self.catalog.map_areas("tatooine", layers, [-1900, -4370, -1880, -4350], "Bestine Nobuild 1"), [])

    def test_no_build_objects_include_placement_and_template_proof(self):
        chart = self.catalog.meta()["maps"]["dantooine"]
        zones = self.catalog.map_areas("dantooine", ("no_build",), chart["bounds"], "bannerpole")
        self.assertTrue(zones)
        authored = next(zone for zone in zones if zone.get("geometry_source"))
        self.assertGreater(authored["extent"]["values"][2], 0)
        self.assertIn("/sys.shared/", authored["geometry_source"]["path"])
        self.assertIn("/sys.server/", authored["source"]["path"])

    def test_starport_outline_links_to_cells_objects_and_spawn_coordinates(self):
        ident = "tatooine/building/1026824"
        starport = self.catalog.entity("locations", ident)["data"]
        self.assertEqual(len(starport["cells"]), 16)
        self.assertFalse(starport["layout_mismatch"])
        self.assertTrue(starport["cells"][0]["floor"]["faces"])
        rooms = [item for cell in starport["cells"] for item in cell["contents"]]
        self.assertTrue(any(item["origin"] == "building_spawn" and item["coordinates"] for item in rooms))
        self.assertTrue(any(item["target"] and item["local_coordinates"] and item["coordinates"] for item in rooms))
        outlines = self.catalog.map_areas("tatooine", ("building_enterable",), starport["bounds"], "Bestine Starport")
        self.assertTrue(any(row["target"] == ["locations", ident] and row["extent"]["shape"] == "polygon" for row in outlines))
        scenery = self.catalog.map_areas("tatooine", ("building_filler",), [-8192, -8192, 8192, 8192], "filler_building")
        self.assertTrue(any(not row["enterable"] and not row.get("target") for row in scenery))

    def test_dashboard_search_resolves_exact_matches_across_sections(self):
        result = self.catalog.search("Bestine Starport")
        self.assertTrue(any(row["id"] == "tatooine/building/1026824" for row in result["locations"]))
        self.assertEqual(self.catalog.search("Watto")["npcs"][0]["id"], "watto")

    def test_item_location_filters_retain_exact_room_and_object(self):
        ident = "object/tangible/terminal/terminal_newsnet.iff"
        item = self.catalog.entity("items", ident)
        first = self.catalog.placements("items", ident, scene="tatooine", size=2)
        second = self.catalog.placements("items", ident, scene="tatooine", size=2, page=2)
        self.assertGreater(item["placement_count"], first["total"])
        self.assertIn("tatooine", first["scenes"])
        self.assertGreater(first["pages"], 1)
        self.assertTrue(all(row["scene"] == "tatooine" for row in first["rows"]))
        self.assertFalse({row["id"] for row in first["rows"]} & {row["id"] for row in second["rows"]})
        matches = self.catalog.placements("items", ident, scene="tatooine", query="Bestine Foyer4")
        bestine = next(row for row in matches["rows"] if row["map_location"] == "tatooine/building/1026824")
        focus = self.catalog.placement(bestine["id"])
        building = self.catalog.entity("locations", focus["map_location"])["data"]
        room = next(room for room in building["cells"] if room["index"] == focus["cell_index"])
        terminal = room["contents"][focus["object_index"]]
        self.assertEqual(terminal["target"], ["items", ident])
        self.assertEqual(terminal["coordinates"], bestine["coordinates"])
        self.assertEqual(terminal["source"], bestine["source"])

    def test_static_items_in_room_link_to_item_pages_and_show_placements(self):
        ident = "object/static/item/item_bottle_fat.iff"
        self.assertEqual(self.catalog.entity("items", ident)["card"]["category"], "Scenery item")
        rows = self.catalog.placements("items", ident, scene="tatooine", query="Association Hall")["rows"]
        focus = self.catalog.placement(rows[0]["id"])
        building = self.catalog.entity("locations", focus["map_location"])["data"]
        room = next(room for room in building["cells"] if room["index"] == focus["cell_index"])
        self.assertEqual(room["contents"][focus["object_index"]]["target"], ["items", ident])

    def test_listening_post_console_has_an_item_page_and_keeps_source_coordinates(self):
        ident = "object/static/worldbuilding/terminal/floor_console_sectional_03.iff"
        item = self.catalog.entity("items", ident)
        self.assertEqual(item["card"]["category"], "Scenery item")
        rows = self.catalog.placements("items", ident, scene="corellia", query="Drennic Barracks")["rows"]
        record = next(row for row in rows if row["map_location"] == "corellia/building/-257549605667708003"
                      and row["source"].get("line") == 52)
        self.assertEqual(record["coordinates"], [-11552.93706, 28.48453, -483.91001])

    def test_building_location_opens_containing_map_with_exact_footprint(self):
        ident = "tatooine/building/1026824"
        row, = self.catalog.placements("locations", ident, scene="tatooine")["rows"]
        focus = self.catalog.placement(row["id"])
        self.assertEqual(row["location"], "Bestine")
        self.assertEqual(focus["map_location"], "tatooine/region/5")
        self.assertEqual(focus["marker"]["coordinates"], row["coordinates"])
        self.assertEqual(focus["marker"]["target"], ["locations", ident])
        self.assertTrue(focus["marker"]["extent"]["paths"])
        self.assertEqual(self.catalog.placements("locations", ident, scene="naboo")["total"], 0)

    def test_every_building_floor_plan_has_a_source_backed_outline(self):
        with self.catalog.connect() as db:
            missing = db.execute("""
                SELECT id FROM entity b WHERE kind='locations' AND category='Building'
                AND EXISTS (SELECT 1 FROM json_each(b.data,'$.cells') room WHERE json_type(room.value,'$.floor')='object')
                AND (COALESCE(json_array_length(b.data,'$.extent.paths'),0)=0
                     OR COALESCE(json_extract(b.data,'$.geometry_source.path'),'')='')
            """).fetchall()
        self.assertEqual(missing, [])
        cave = self.catalog.entity("locations", "mustafar/building/-515943632364872448")["data"]
        self.assertTrue(cave["cells"][0]["floor"]["faces"])
        self.assertIn(cave["geometry_kind"], ("Client exterior floor", "Mapped interior footprint"))

    def test_quest_item_location_uses_authored_turn_in(self):
        ident = "object/tangible/component/droid/droid_brain.iff"
        rows = self.catalog.placements("items", ident, scene="tatooine", query="Mos Espa")["rows"]
        result = next(row for row in rows if row["related"] and row["related"][1] == "legacy/static/kitster_banai/3")
        focus = self.catalog.placement(result["id"])
        self.assertEqual(focus["marker"]["name"], "Kitster Banai")
        self.assertEqual(focus["marker"]["coordinates"], [-2887.0, 5.5, 2494.0])
        self.assertEqual(result["method"], "Quest turn-in")

    def test_spawner_settings_belong_to_the_placed_object(self):
        ident = "object/tangible/ground_spawning/area_spawner.iff"
        item = self.catalog.entity("items", ident)
        self.assertNotIn("spawner", item["data"])
        result = self.catalog.map_list("tatooine", ("spawn",), [-8192, -8192, 8192, 8192], "Legacy Watto Thieves", 1, 60)
        anchor = next(row for row in result["rows"] if row.get("source", {}).get("line") == 4
                      and row.get("target") == ["items", ident])
        self.assertEqual(anchor["spawner"]["entries"][0]["ref"], "watto_thieves")
        self.assertEqual(anchor["spawner"]["count"], 2)
        self.assertEqual(anchor["spawner"]["radius"], 10)
        self.assertEqual((anchor["spawner"]["delay_min"], anchor["spawner"]["delay_max"]), (5, 10))

    def test_tusken_fort_egg_uses_the_placed_object_id_and_poi_table(self):
        building = self.catalog.entity("locations", "tatooine/building/1189173")["data"]
        room = next(cell for cell in building["cells"] if cell["index"] == 1)
        egg = next(entry for entry in room["contents"] if entry["source"].get("line") == 626
                   and entry["source"]["path"].endswith("/snapshot_functional_restore.tab"))
        self.assertEqual([round(value, 3) for value in egg["coordinates"]], [-3960.219, 24.326, 6185.004])
        config = egg["spawner"]
        self.assertEqual((config["count"], config["delay_min"]), (2, 400))
        self.assertEqual(config["source"]["line"], 78)
        self.assertTrue(config["source"]["path"].endswith("/spawning/poi_spawner/tatooine.tab"))
        self.assertEqual([(row["npc"], row["count"]) for row in config["entries"]], [("tusken_raider", 2)])
        self.assertTrue(any(entry.get("target") == ["npcs", "tusken_fort_tusken_treasure_guardian"]
                            for entry in room["contents"]))

    def test_talus_poi_spawners_read_the_named_object_id_column(self):
        template = "object/tangible/poi/spawnegg/exterior_spawnegg.iff"
        placements = self.catalog.placements("items", template, scene="talus", size=200)
        egg = next(row for row in placements["rows"] if row["source"]["line"] == 106
                   and row["source"]["path"].endswith("/snapshot_functional_restore.tab"))
        config = egg["spawner"]
        self.assertEqual((config["count"], config["delay_min"]), (4, 1800))
        self.assertTrue(config["source"]["path"].endswith("/spawning/poi_spawner/talus.tab"))
        self.assertEqual([(row["npc"], row["count"]) for row in config["entries"]], [("mutated_kahmurra", 4)])

    def test_creatures_find_their_placed_spawn_eggs_and_map_links(self):
        creature = self.catalog.entity("npcs", "mutated_kahmurra")
        self.assertGreater(creature["placement_count"], 0)
        template = "object/tangible/poi/spawnegg/exterior_spawnegg.iff"
        self.assertIn(template, [row["id"] for row in creature["related"]["spawners"]["rows"]])
        result = self.catalog.placements("npcs", "mutated_kahmurra", scene="talus", size=100)
        egg = next(row for row in result["rows"] if row["spawner_template"] == template and row["source"]["line"] == 106
                   and row["source"]["path"].endswith("/snapshot_functional_restore.tab"))
        self.assertEqual(egg["spawner"]["count"], 4)
        focused = self.catalog.placement(egg["id"])
        self.assertEqual(focused["marker"]["coordinates"], egg["coordinates"])
        self.assertEqual(focused["owner"], template)

    def test_sentinel_spawner_definition_survives_without_a_placed_instance(self):
        for side in ("dark", "light"):
            creature = self.catalog.entity("npcs", f"{side}_jedi_sentinel")
            spawner, = creature["related"]["spawners"]["rows"]
            self.assertEqual(spawner["id"], f"object/tangible/jedi/jedi_sentinel_{side}_spawner.iff")
            evidence = spawner["detail"]
            self.assertEqual((evidence["count"], evidence["delay_min"], evidence["script_source"]["line"]), (1, 5, 14))
            self.assertEqual(evidence["entries"][0]["npc"], f"{side}_jedi_sentinel")
            self.assertEqual(creature["placement_count"], 0)
            self.assertEqual(creature["data"]["locations"], [])

    def test_poi_table_coordinates_remain_available_when_buildout_cell_is_missing(self):
        result = self.catalog.map_list("corellia", ("quest_start",), [-248, -4575, -241, -4568],
                                       "Daclif Gallamby", 1, 20)
        start = next(row for row in result["rows"] if row["target"] == ["quests", "legacy/static/daclif_gallamby/1"])
        self.assertEqual(start["coordinates"], [-245.46, 26.36, -4571.95])
        self.assertTrue(start["source"]["path"].endswith("/spawning/poi_spawner/corellia.tab"))

    def test_scripted_eggs_report_configured_population_or_initial_wave(self):
        jawas = self.catalog.placements("items", "object/tangible/poi/spawnegg/jawa_spawnegg.iff", scene="tatooine")
        configs = [row["spawner"] for row in jawas["rows"] if row.get("spawner")]
        self.assertEqual(sorted(config["count"] for config in configs), [1, 3])
        self.assertTrue(all(config["selection"] == "random" and len(config["entries"]) == 7 for config in configs))
        battles = self.catalog.placements("items", "object/tangible/poi/spawnegg/battle_spawnegg.iff", scene="yavin4")
        wave = next(row["spawner"] for row in battles["rows"] if row.get("spawner"))
        self.assertEqual((wave["count"], wave["count_label"]), (24, "Initial wave"))
        self.assertEqual(sum(entry["count"] for entry in wave["entries"]), 24)
        self.assertEqual(next(entry["count"] for entry in wave["entries"] if entry["ref"] == "at_st"), 1)

    def test_all_location_pages_have_descriptions_and_planets_have_lore_sources(self):
        with self.catalog.connect() as db:
            missing = db.execute("""
                SELECT COUNT(*) FROM entity WHERE kind='locations'
                   AND COALESCE(json_extract(data, '$.description.text'), '') = ''
            """).fetchone()[0]
        self.assertEqual(missing, 0)
        self.assertIn("two suns", self.catalog.entity("locations", "tatooine")["data"]["description"]["text"])
        self.assertIn("swglegends.com", next(source["url"] for source in
                       self.catalog.entity("locations", "talus")["data"]["description"]["sources"] if "url" in source))
        ord_mantell = self.catalog.entity("locations", "ord_mantell")["data"]
        self.assertEqual(ord_mantell["map"]["scene"], "space_ord_mantell")

    def test_locations_link_to_their_city_and_planet_in_both_directions(self):
        city = "tatooine/region/8"
        building, transport = "tatooine/building/1106368", "tatooine/transport/1106368"
        detail = self.catalog.entity("locations", transport)
        self.assertEqual([parent["id"] for parent in detail["ancestors"]], ["tatooine", city])
        city_places = self.catalog.links("locations", city, "places", 1, 200)
        starport = [row["id"] for row in city_places["rows"] if row["card"]["name"] == "Mos Eisley Starport"]
        self.assertEqual(starport, [building])
        self.assertEqual(detail["related"]["interior"]["rows"][0]["id"], building)
        interior = self.catalog.entity("locations", building)
        self.assertEqual(interior["ancestors"], detail["ancestors"])
        self.assertEqual(interior["related"]["travel"]["rows"][0]["id"], transport)
        matches = self.catalog.list("locations", query="Mos Eisley Starport")
        self.assertEqual([row["id"] for row in matches["rows"] if row["name"] == "Mos Eisley Starport"], [building])
        travel_matches = self.catalog.list("locations", query="Mos Eisley Starport", category="Transport")
        self.assertEqual([row["id"] for row in travel_matches["rows"]], [transport])
        planet = self.catalog.entity("locations", "tatooine")
        self.assertEqual(planet["ancestors"], [])
        self.assertIn(city, [row["id"] for row in planet["related"]["places"]["rows"]])

    def test_planets_list_child_scenes_and_hoth_has_one_browsable_world(self):
        hoth = self.catalog.list("locations", query="hoth", category="World")
        self.assertEqual([row["id"] for row in hoth["rows"]], ["hoth"])
        planet = self.catalog.entity("locations", "adventure2")
        self.assertEqual(planet["card"]["id"], "hoth")
        self.assertEqual(planet["ancestors"], [])
        bases = [row for row in planet["related"]["places"]["rows"]
                 if row["id"].startswith("adventure2/area/echo_base-")]
        self.assertEqual(len({row["id"] for row in bases}), 9)
        self.assertTrue(all(row["detail"]["shared_name"] for row in bases))
        kashyyyk = self.catalog.entity("locations", "kashyyyk")
        self.assertIn("kashyyyk_main", [row["id"] for row in kashyyyk["related"]["places"]["rows"]])
        trail = self.catalog.entity("locations", "kashyyyk_rryatt_trail")
        self.assertEqual([parent["id"] for parent in trail["ancestors"]], ["kashyyyk"])
        self.assertIn("kashyyyk_rryatt_trail",
                      [row["id"] for row in self.catalog.list("locations", scene="kashyyyk", category="World")["rows"]])
        meta = self.catalog.meta()["locations"]
        self.assertEqual(meta["kashyyyk"]["map_id"], "kashyyyk_main")
        ord_station = self.catalog.entity("locations", "space_ord_mantell/poi/ord_station")
        self.assertEqual([parent["id"] for parent in ord_station["ancestors"]], ["ord_mantell", "space_ord_mantell"])

    def test_location_child_links_have_one_matching_parent_per_identity(self):
        with self.catalog.connect() as db:
            multiple_parents = db.execute("""
                SELECT id FROM link WHERE kind='locations' AND role='parent'
                GROUP BY id HAVING COUNT(*)>1
            """).fetchall()
            missing_reverse = db.execute("""
                SELECT child.id FROM link child
                LEFT JOIN link parent ON parent.kind='locations' AND parent.id=child.target_id
                    AND parent.role='places' AND parent.target_kind='locations' AND parent.target_id=child.id
                WHERE child.kind='locations' AND child.role='parent' AND parent.id IS NULL
            """).fetchall()
        self.assertEqual(multiple_parents, [])
        self.assertEqual(missing_reverse, [])

    def test_tatooine_exposes_an_interior_npc_quest_markers_and_placed_objects(self):
        chart = self.catalog.meta()["maps"]["tatooine"]
        self.assertTrue(all(chart["layers"][name] > 0 for name in
                            ("npc", "quest_start", "quest_objective", "structure", "service", "scenery")))
        result = self.catalog.map_markers("tatooine", ("npc",), chart["bounds"], "watto", 10, 10)
        point, = result["groups"]
        self.assertEqual(point["cell"], "r2")
        self.assertAlmostEqual(point["coordinates"][0], -2893.453, places=2)
        self.assertEqual(point["target"], ["npcs", "watto"])

    def test_server_room_placement_preserves_the_matching_client_parent_source(self):
        coverage = self.catalog.meta()["map_data"]["placement_sources"]
        self.assertGreater(coverage["server_buildout_client_parent"], 0)
        with self.catalog.connect() as db:
            origin, parent, cell = db.execute("""
                SELECT json_extract(data,'$.source.path'),json_extract(data,'$.parent_source.path'),
                       json_extract(data,'$.cell')
                FROM map_marker WHERE json_extract(data,'$.origin')='server_buildout_client_parent' LIMIT 1
            """).fetchone()
        self.assertIn("/sys.server/", origin)
        self.assertTrue(parent.startswith("clientdata/"))
        self.assertTrue(cell.startswith("Cell "))

    def test_planet_and_regional_maps_list_their_named_cities(self):
        maps = self.catalog.meta()["maps"]
        examples = {"tatooine": "Mos Eisley", "corellia": "Coronet", "naboo": "Theed",
                    "kashyyyk_main": "Kachirho", "mustafar": "Mensix Mining Facility"}
        for scene, name in examples.items():
            with self.subTest(scene=scene):
                city = next(row for row in maps[scene]["cities"] if row["name"] == name)
                self.assertEqual(city["layer"], "city")
                self.assertEqual(city["target"][0], "locations")
                self.assertTrue(self.catalog.entity(*city["target"]))


if __name__ == "__main__":
    unittest.main()
