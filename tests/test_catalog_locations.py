"""Regression coverage for authored SWG location proofs."""

from __future__ import annotations

import sys
import unittest
from fnmatch import fnmatchcase
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from catalog_locations import BUILDOUT, SERVER, LocationIndex, MapLibrary
from archive_quests.manifest_sources import read_tab


SERVER_TABLE = SERVER / "buildout/test_scene/city.tab"
AREA_TABLE = BUILDOUT / "areas_test_scene.tab"


def table(columns, *rows):
    return "\n".join(("\t".join(columns), "\t".join("s" for _ in columns),
                      *("\t".join(str(value) for value in row) for row in rows)))


class InMemoryFiles:
    def __init__(self, duplicate_area=False):
        rectangles = [("city", -1000, 500, 1000, 1500)]
        if duplicate_area:
            rectangles.append(("city", 7000, 500, 9000, 1500))
        self.tables = {
            BUILDOUT / "buildout_scenes.tab": table(["sceneName"], ["test_scene"]),
            AREA_TABLE: table(["area", "x1", "z1", "x2", "z2"], *rectangles),
            SERVER_TABLE: table(
                ["objid", "container", "server_template_crc", "cell_index", "px", "py", "pz",
                 "qw", "qx", "qy", "qz", "scripts", "objvars"],
                [-1, 0, "object/building/demo.iff", 0, 200, 15, 300, ".70710678", 0, ".70710678", 0, "", ""],
                [-2, -1, "object/cell/cell.iff", 3, 2, 0, 0, 1, 0, 0, 0, "", ""],
                [-3, -2, "object/mobile/demo.iff", 1, 0, 1, 5, 1, 0, 0, 0, "", ""],
                [-4, -99, "object/mobile/orphan.iff", 2, 500, 1, 500, 1, 0, 0, 0, "", ""],
            ),
        }

    def paths(self, directory, pattern, recursive=False):
        for path in self.tables:
            if path.is_relative_to(directory):
                relative = path.relative_to(directory).as_posix()
                if relative.count("/") == pattern.count("/") and fnmatchcase(relative, pattern):
                    yield path

    def exists(self, path):
        return path in self.tables

    def text(self, path):
        return self.tables[path]

    def tab(self, path):
        return read_tab(path, self.text(path))


class LocationIndexTest(unittest.TestCase):
    def test_rotated_root_and_nested_cell_offset(self):
        index = LocationIndex(InMemoryFiles())

        placement = index.lookup_id("test_scene", -3)

        self.assertEqual(placement["status"], "transformed_cell")
        self.assertEqual(placement["coordinates"], [-795, 16, 798])
        self.assertEqual(placement["cell"]["index"], 3)
        self.assertEqual(placement["local_coordinates"], [0, 1, 5])
        self.assertEqual(len(placement["parent_sources"]), 2)
        self.assertEqual(index.by_mob["demo"][0]["coordinates"], placement["coordinates"])

    def test_missing_parent_never_yields_world_coordinates(self):
        index = LocationIndex(InMemoryFiles())

        placement = index.lookup_id("test_scene", -4)

        self.assertEqual(placement["status"], "missing_buildout_parent")
        self.assertIsNone(placement["coordinates"])
        self.assertEqual(placement["local_coordinates"], [500, 1, 500])

    def test_reused_area_keeps_both_registrations(self):
        index = LocationIndex(InMemoryFiles(duplicate_area=True))
        first, second = index.by_mob["demo"]

        self.assertNotEqual(first["runtime_object_id"], second["runtime_object_id"])
        self.assertIsNone(index.lookup_id("test_scene", -3))
        placement = index.lookup(first["source"])
        self.assertEqual(placement["status"], "multiple_buildout_placements")
        self.assertEqual([item["coordinates"][0] for item in placement["alternatives"]], [-795, 7205])

    def test_inherited_static_and_patrol_spawners_resolve_their_own_counts(self):
        files = InMemoryFiles()
        default_patrol = "object/tangible/ground_spawning/patrol_base.iff"
        patrol = "object/tangible/ground_spawning/authored_patrol.iff"
        static = "object/tangible/ground_spawning/mob_spawner.iff"
        files.tables[SERVER.parent / Path(default_patrol).with_suffix(".tpf")] = (
            'scripts = ["systems.spawning.spawner_patrol"]\n'
            'objvars = ["strSpawns" = "guard_pool", "intSpawnCount" = 1]\n'
        )
        files.tables[SERVER.parent / Path(patrol).with_suffix(".tpf")] = f"@base {default_patrol}\n"
        files.tables[SERVER.parent / Path(static).with_suffix(".tpf")] = 'scripts = ["systems.spawning.mob_spawner"]'
        files.tables[SERVER / "mob/creatures.tab"] = table(["creatureName"], ["city_guard"], ["bandit"])
        files.tables[SERVER / "spawning/ground_spawning/types/guard_pool.tab"] = table(
            ["strItem"], ["city_guard"], ["bandit"], ["city_guard"])
        files.tables[SERVER / "spawning/ground_spawning/static/test_scene.tab"] = table(
            ["strObjId", "strType", "strMob", "fltMinSpawnTime", "fltMaxSpawnTime"],
            [300, "", "city_guard", 10, 20], [301, "", "bandit", 50, 80])
        files.tables[SERVER_TABLE] += (
            f"\n300\t0\t{static}\t0\t100\t5\t200\t1\t0\t0\t0\t\t$|"
            f"\n301\t0\t{patrol}\t0\t150\t5\t220\t1\t0\t0\t0\t\tintSpawnCount|0|3|patrolPathType|4|cycle|$|"
        )
        index = LocationIndex(files)
        single = index.lookup_id("test_scene", 300)["spawner"]
        self.assertEqual((single["count"], single["delay_min"], single["delay_max"]), (1, 10, 20))
        self.assertEqual([entry["npc"] for entry in single["entries"]], ["city_guard"])
        group = index.lookup_id("test_scene", 301)["spawner"]
        self.assertEqual((group["count"], group["patrol_type"], group["selection"]), (3, "cycle", "random"))
        self.assertEqual([(entry["npc"], entry["rows"]) for entry in group["entries"]],
                         [("city_guard", 2), ("bandit", 1)])

    def test_map_library_rejects_unrelated_asset_paths(self):
        library = MapLibrary(None)
        self.assertIsNone(library.map_image({"asset": "texture/../ui_map_tatooine.dds"}))
        self.assertIsNone(library.map_image({"asset": "texture/a_ship.dds"}))


if __name__ == "__main__":
    unittest.main()
