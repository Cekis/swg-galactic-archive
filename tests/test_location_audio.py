import json
from pathlib import Path
import shutil
import struct
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from catalog_db import Catalog, Writer
from catalog_location_audio import INTERIOR_TABLE, LocationAudio, _script_uses
from catalog_models import ModelLibrary
from catalog_sounds import SoundIndex
from catalog_sources import SERVER, SCRIPTS, Files, Script
from catalog_terrain_audio import read_terrain_audio
from test_client_assets import native_string_table
from test_model_assets import iff_chunk, iff_form
from test_source_profiles import save


BASE = "sample/wind.wav"
TOWN = "sound/town.snd"
TOWN_NIGHT = "sound/town_alt.snd"
TOWN_CLIP = "sample/crowd.wav"
CITY_NIGHT = "sample/city_night.wav"
ROOM = "sample/hall.wav"
DEFAULT_ROOM = "sample/interior.wav"
NEBULA = "sample/nebula.wav"
SCRIPT_CUE = "sample/trigger.wav"
EFFECT_CUE = "sample/chime.wav"
WORLD = "fixture"
CITY = "fixture/region/town"
RANGE = "fixture/region/range"
BUILDING = "fixture/building/10"
SPACE = "space_fixture"
CLOUD = "space_fixture/area/cloud"
SCRIPT = SCRIPTS / "fixture/ambience.script"
BUILDOUT = SERVER / "buildout/fixture/placement.tab"


def header(name, active=True):
    return iff_form(b"IHDR", iff_form(b"0001", iff_chunk(
        b"DATA", struct.pack("<i", active) + name.encode() + b"\0")))


def layer(name, family, *bounds, active=True):
    effect = iff_form(b"AENV", iff_form(b"0000", header("Environment"),
                                       iff_chunk(b"DATA", struct.pack("<2if", family, 0, 1))))
    return iff_form(b"LAYR", iff_form(b"0003", header(name, active),
                                     iff_chunk(b"ADTA", b"\0" * 13), *bounds, effect))


def terrain_asset():
    families = iff_form(b"EGRP", iff_form(b"0002", *(
        iff_form(b"EFAM", iff_chunk(b"DATA",
                struct.pack("<i", i) + name.encode() + b"\0\0\0\0" + struct.pack("<f", 1)))
        for i, name in enumerate(("range", "town", "precipice", "unused"), 1))))
    circle = iff_form(b"BCIR", iff_form(b"0002", header("Town boundary"),
                                       iff_chunk(b"DATA", struct.pack("<3fif", 0, 0, 12, 0, .2))))
    conditional = iff_form(b"FHGT", iff_form(b"0000", header("High altitude")))
    return iff_form(b"PTAT", iff_form(b"0014", families, iff_form(b"LYRS",
        layer("Range", 1), layer("Town", 2, circle), layer("Precipice", 3, circle, conditional),
        layer("Disabled", 4, active=False))))


def table(path, *rows):
    save(path, native_string_table(*(tuple(column.encode() for column in row) for row in rows)))


class LocationAudioTests(unittest.TestCase):
    def test_terrain_layers_apply_boundaries_priority_and_unknown_runtime_filters(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.trn"
            save(path, terrain_asset())
            terrain = read_terrain_audio(path)
            self.assertEqual(terrain.at(40, 0), {"range": ("Range", False)})
            self.assertEqual(terrain.at(0, 0), {"town": ("Town", True), "precipice": ("Precipice", True)})
            self.assertNotIn("unused", {rule.family for rule in terrain.rules})

    def test_location_sounds_and_sound_locations_preserve_their_playback_conditions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            save(root / "terrain/fixture.trn", terrain_asset())
            for name in (TOWN, TOWN_NIGHT):
                save(root / name, iff_form(b"SD2D", iff_chunk(b"0004",
                    struct.pack("<I", 1) + TOWN_CLIP.encode() + b"\0")))
            for name in (BASE, TOWN_CLIP, CITY_NIGHT, ROOM, DEFAULT_ROOM, NEBULA, SCRIPT_CUE, EFFECT_CUE):
                save(root / name, b"fixture clip")
            table(root / "datatables/environment/fixture.iff",
                  ("Environment Family Name", "Weather Index", "Day Ambient Sound 1 (2d .snd)",
                   "Night Ambient Sound 1 (2d .snd)"),
                  ("range", "0", BASE, ""), ("town", "0", TOWN, TOWN_NIGHT),
                  ("town", "2", TOWN, CITY_NIGHT), ("unused", "0", ROOM, ""))
            table(root / INTERIOR_TABLE,
                  ("PobName", "Cell Name", "Day sound/ambient Sound (2d .snd)", "Night sound/ambient Sound (2d .snd)"),
                  ("default", "default", DEFAULT_ROOM, DEFAULT_ROOM),
                  ("fixture", "hall", ROOM, ""))
            table(root / "datatables/space/nebula/space_fixture.iff",
                  ("name", "x", "y", "z", "radius", "ambientSound"),
                  ("Cloud", "5", "10", "5", "12", NEBULA))
            save(root / "clienteffect/fixture.cef", iff_form(b"CLEF", iff_form(
                b"0003", iff_chunk(b"PSND", EFFECT_CUE.encode() + b"\0"))))

            files = Files({
                SCRIPT.as_posix(): b"""
public int OnTriggerVolumeEntered(obj_id self, String volume, obj_id who) {
    String entry = getStringObjVar(self, "soundFile");
    playMusic(who, entry);
    return 0;
}
public int fixedEffect(obj_id self) {
    location site = new location(3.0f, 0.0f, 2.0f, "fixture", null);
    playClientEffectLoc(self, "clienteffect/fixture.cef", site, 0);
    return 0;
}
public int playerEffect(obj_id self, obj_id player) {
    playClientEffectLoc(self, "clienteffect/fixture.cef", getLocation(player), 0);
    return 0;
}
""",
                BUILDOUT.as_posix(): b"scripts\tobjvars\ns\ts\nfixture.ambience\tsoundFile|4|sample/trigger.wav\n",
            })
            placement = {"scene": WORLD, "coordinates": [3, 0, 2], "source": {
                "path": BUILDOUT.as_posix(), "line": 3}, "template": "object/tangible/fixture.iff"}
            spatial = SimpleNamespace(buildout_objects=lambda: [placement],
                _template_spawn_settings=lambda _: ({}, {}))
            def nearby(point):
                return [CITY] if point["scene"] == WORLD and point["coordinates"][0] < 10 else []


            index = SoundIndex(root, ModelLibrary(root))
            index.inventory()
            places = {
                WORLD: {"scene": WORLD, "category": "World", "name": "Fixture world"},
                CITY: {"scene": WORLD, "category": "Region", "name": "Town", "coordinates": [0, 0, 0]},
                RANGE: {"scene": WORLD, "category": "Region", "name": "Range", "coordinates": [40, 0, 0]},
                SPACE: {"scene": SPACE, "category": "World", "name": "Fixture system"},
                CLOUD: {"scene": SPACE, "category": "Region", "name": "Cloud", "coordinates": [5, 10, 5]},
            }
            database = root / "catalog.sqlite3"
            writer = Writer(database)
            try:
                for ident, place in places.items():
                    writer.entity("locations", ident, place["name"], place,
                                  category=place["category"], scene=place["scene"])
                writer.entity("locations", BUILDING, "Hall building", {
                    "layout_asset": "appearance/fixture.pob", "cells": [
                        {"name": "world", "index": 0, "contents": []},
                        {"name": "hall", "index": 1, "contents": []},
                        {"name": "storage", "index": 2, "contents": []},
                    ]}, category="Building", scene=WORLD)
                warnings = []
                sound_locations = LocationAudio(writer, index, places, lambda *detail: warnings.append(detail))
                sound_locations.outdoor()
                sound_locations.interiors()
                sound_locations.nebulae()
                positions = list(_script_uses(Script(SCRIPT, files.text(SCRIPT))))
                self.assertEqual(len(positions), 2)
                sound_locations.scripted(files, spatial, nearby)
                self.assertGreater(sound_locations.publish()["links"], 10)
                index.publish(writer)
                writer.finish({"revision": "fixture", "locations": {}, "maps": {}})
                self.assertFalse(warnings)
            finally:
                writer.connection.close()

            catalog = Catalog(database)
            town = catalog.entity("locations", CITY)
            rows = {row["id"]: row for row in town["related"]["sounds"]["rows"]}
            self.assertEqual(rows[TOWN]["detail"]["sound_uses"], ["Ambience"])
            self.assertEqual(catalog.entity("sounds", TOWN_CLIP)["card"]["sound_uses"], ["Ambience"])
            self.assertEqual(set(rows), {TOWN, CITY_NIGHT, SCRIPT_CUE, EFFECT_CUE})
            self.assertEqual(rows[TOWN]["clip"], TOWN_CLIP)
            self.assertEqual(set(rows[TOWN]["sound_aliases"]), {TOWN_CLIP, TOWN_NIGHT})
            self.assertEqual({tuple(use["weather_indices"]) for use in rows[TOWN]["detail"]["ambient_uses"]},
                             {("0", "2"), ("0",)})
            self.assertEqual(rows[SCRIPT_CUE]["detail"]["ambient_uses"][0]["placement"]["line"], 3)
            self.assertEqual(rows[EFFECT_CUE]["detail"]["ambient_uses"][0]["coordinates"], [3, 0, 2])
            self.assertEqual({row["id"] for row in catalog.links("locations", RANGE, "sounds", 1, 20)["rows"]}, {BASE})

            sound = catalog.entity("sounds", TOWN)
            self.assertEqual({row["id"] for row in sound["related"]["locations"]["rows"]}, {WORLD, CITY})
            cave = catalog.links("locations", BUILDING, "sounds", 1, 20)["rows"]
            matching = {row["id"]: row["detail"]["ambient_uses"][0]["cells"] for row in cave}
            self.assertEqual(matching, {ROOM: ["hall"], DEFAULT_ROOM: ["storage"]})
            self.assertEqual({row["id"] for row in catalog.links("sounds", NEBULA, "locations", 1, 20)["rows"]},
                             {SPACE, CLOUD})

            if node := shutil.which("node"):
                renderer = root / "renderer"
                save(renderer / "package.json", '{"type":"module"}')
                static = Path(__file__).resolve().parents[1] / "static"
                for name in ("views.js", "ui.js", "inventory-views.js"):
                    shutil.copyfile(static / name, renderer / name)
                script = """
import { readFileSync } from "node:fs";
import { entityPage } from "./views.js";
const { town, sound, metadata } = JSON.parse(readFileSync(0, "utf8"));
const location = entityPage("locations", town, "sounds", metadata);
const audio = entityPage("sounds", sound, "locations", metadata);
console.log(JSON.stringify([location.html, audio.html]));
"""
                rendered = subprocess.run([node, "--input-type=module", "-e", script], cwd=renderer,
                    input=json.dumps({"town": town, "sound": sound, "metadata": catalog.metadata()}),
                    capture_output=True, encoding="utf-8")
                self.assertEqual(rendered.returncode, 0, rendered.stderr)
                location_html, sound_html = json.loads(rendered.stdout)
                self.assertIn("Sounds heard here", location_html)
                self.assertEqual(location_html.count('src="/api/audio?id=sample%2Fcrowd.wav"'), 1)
                self.assertIn('href="/sounds/sound%2Ftown_alt.snd"', location_html)
                self.assertIn("Weather variants 0, 2", location_html)
                self.assertIn("Where it plays", sound_html)
                self.assertIn('/locations/fixture%2Fregion%2Ftown?view=sounds"', sound_html)


if __name__ == "__main__":
    unittest.main()
