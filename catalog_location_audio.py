"""Index ambient sound locations using the client's environment data and placed script cues."""

from collections import defaultdict
from functools import lru_cache
import json
from math import isfinite
from pathlib import PurePosixPath
import re
import struct

from archive_quests.manifest_scripts import assignments
from catalog_entities import script_ids
from catalog_location_hierarchy import canonical_location
from catalog_locations import _vars
from catalog_models import chunks, value, version
from catalog_sources import ROOT, Script, arguments, literal, read_iff_table, script_paths
from catalog_sounds import CALL, PLAY_CALLS, sound_id
from catalog_terrain_audio import read_terrain_audio


OUTDOOR_COLUMNS = {
    "Day Ambient Sound 1 (2d .snd)": "Day ambience",
    "Day Ambient Sound 2 (2d .snd)": "Day ambience",
    "Night Ambient Sound 1 (2d .snd)": "Night ambience",
    "Night Ambient Sound 2 (2d .snd)": "Night ambience",
    "First Music (2d .snd)": "First-visit music",
    "Sunrise Music (2d .snd)": "Sunrise music",
    "Sunset Music (2d .snd)": "Sunset music",
}
INTERIOR_COLUMNS = {
    "Day sound/ambient Sound (2d .snd)": "Day interior ambience",
    "Night sound/ambient Sound (2d .snd)": "Night interior ambience",
    "First Music (2d .snd)": "First-visit interior music",
}
INTERIOR_TABLE = "datatables/interior/interior.iff"
READ_ERRORS = (OSError, ValueError, KeyError, AssertionError, IndexError, StopIteration, struct.error)


def _coordinates(place):
    return (place.get("marker") or {}).get("coordinates") or place.get("coordinates")


def _area_points(place, anchors):
    """Use the map point, interior samples from its bounds, and terrain area centers."""
    point = _coordinates(place)
    positions = [(point[0], point[2])] if point and point[0] is not None and point[2] is not None else []
    boundary = place.get("boundary") or {}
    bounds = place.get("bounds") or boundary.get("values")
    if not bounds:
        return positions
    if boundary.get("shape") == "circle":
        cx, cz, radius = boundary["values"]
        positions.extend((cx + radius * dx, cz + radius * dz)
                         for dx, dz in ((0, 0), (0, .8), (.8, 0), (0, -.8), (-.8, 0)))
        positions.extend((x, z) for x, z in anchors if (x - cx) ** 2 + (z - cz) ** 2 < radius ** 2)
    elif len(bounds) == 4:
        left, bottom, right, top = bounds
        positions.extend((left + (right - left) * x, bottom + (top - bottom) * z)
                         for x in (.1, .5, .9) for z in (.1, .5, .9))
        positions.extend((x, z) for x, z in anchors if left < x < right and bottom < z < top)
    return list(dict.fromkeys(positions))


def _audible_fields(row, columns):
    for column, label in columns.items():
        if ident := sound_id(row.get(column)):
            yield ident, label


class LocationAudio:
    def __init__(self, writer, sounds, places, issues):
        self.writer = writer
        self.root = sounds.library.root
        self.sounds = sounds
        self.issues = issues
        self.places = {ident: place for ident, place in places.items() if ident == canonical_location(ident)}
        self.by_scene = defaultdict(dict)
        for ident, place in self.places.items():
            self.by_scene[place["scene"]][ident] = place
        self.buildings_by_source = {}
        self.building_ids = set()
        for ident, scene, raw in writer.connection.execute("""
            SELECT id,scene,json_extract(data,'$.source') FROM entity
            WHERE kind='locations' AND category='Building'
        """):
            self.building_ids.add(ident)
            if raw:
                proof = json.loads(raw)
                key = (scene, proof["path"], proof.get("line"))
                self.buildings_by_source[key] = None if key in self.buildings_by_source else ident
        self.uses = defaultdict(list)

    def table(self, ref):
        path = self.root / ref
        if not path.is_file():
            return ()
        try:
            return read_iff_table(path)[2]
        except READ_ERRORS as error:
            self.issues("Sounds", f"Could not read the location sound table {ref}. {type(error).__name__}.")
            return ()

    def add(self, location, ident, use):
        if location not in self.places and location not in self.building_ids:
            return
        for target in dict.fromkeys((ident, *self.sounds.ensure(ident)["samples"])):
            entries = self.uses[location, target]
            if use not in entries:
                entries.append(use)

    def outdoor(self):
        for scene, places in self.by_scene.items():
            ref = f"datatables/environment/{scene}.iff"
            rows = self.table(ref)
            if not rows:
                continue
            profiles = defaultdict(lambda: defaultdict(list))
            for number, row in enumerate(rows, 1):
                family = str(row.get("Environment Family Name", "")).strip().casefold()
                weather = row.get("Weather Index", 0)
                for ident, label in _audible_fields(row, OUTDOOR_COLUMNS):
                    profiles[family][ident, label].append((number, weather))
            world = canonical_location(scene)
            terrain_ref = f"terrain/{scene}.trn"
            terrain_source = self.root / terrain_ref
            try:
                terrain = read_terrain_audio(terrain_source) if terrain_source.is_file() else None
            except READ_ERRORS as error:
                self.issues("Sounds", f"Could not resolve environment layers in {terrain_ref}. {type(error).__name__}.")
                terrain = None
            if not terrain or not terrain.rules:
                for family in profiles:
                    self._environment(world, profiles, family, ref, context="Authored environment profile")
                continue
            anchors = terrain.anchors
            assigned = dict.fromkeys(rule.family for rule in terrain.rules)
            for family in assigned:
                self._environment(world, profiles, family, ref, terrain_ref, context="Outdoor terrain")
            for location, place in places.items():
                if location == world:
                    continue
                heard = {}
                for x, z in _area_points(place, anchors):
                    for family, (layer, conditional) in terrain.at(x, z).items():
                        if family not in heard or heard[family][1] and not conditional:
                            heard[family] = (layer, conditional, [round(x, 2), None, round(z, 2)])
                for family, (layer, conditional, point) in heard.items():
                    self._environment(location, profiles, family, ref, terrain_ref,
                                      context=layer or "Outdoor terrain", conditional=conditional, point=point)

    def _environment(self, location, profiles, family, ref, terrain=None, *, context, conditional=False, point=None):
        for (ident, label), variants in profiles.get(family, {}).items():
            self.add(location, ident, {
                "label": label, "context": context, "family": family,
                "weather_indices": list(dict.fromkeys(weather for _, weather in variants)),
                "conditional": conditional, "rows": [number for number, _ in variants],
                "source": {"path": "clientdata/" + ref, "row": variants[0][0]},
                **({"terrain": {"path": "clientdata/" + terrain}} if terrain else {}),
                **({"coordinates": point} if point is not None else {}),
            })


    def interiors(self):
        entries = {}
        for number, row in enumerate(self.table(INTERIOR_TABLE), 1):
            layout = str(row.get("PobName", "")).casefold()
            cell = str(row.get("Cell Name", "")).casefold()
            entries.setdefault((layout, cell), (number, row))
        if not entries:
            return
        for ident, raw in self.writer.connection.execute(
            "SELECT id,data FROM entity WHERE kind='locations' AND category='Building'"
        ):
            place = json.loads(raw)
            layout = place.get("layout_asset")
            if not layout or place.get("layout_mismatch"):
                continue
            short = PurePosixPath(layout).stem.casefold()
            assigned = defaultdict(list)
            for cell in place.get("cells", []):
                name = cell.get("name")
                if not name or cell.get("index") == 0:
                    continue
                record = (entries.get((short, name.casefold())) or entries.get((short, "default"))
                          or entries.get(("default", "default")))
                if record is not None:
                    row_id, row = record
                    for sound, label in _audible_fields(row, INTERIOR_COLUMNS):
                        assigned[sound, label, row_id].append(name)
            for (sound, label, row_id), cells in assigned.items():
                self.add(ident, sound, {
                    "label": label, "cells": list(dict.fromkeys(cells)),
                    "source": {"path": "clientdata/" + INTERIOR_TABLE, "row": row_id},
                    "portal": {"path": "clientdata/" + layout},
                })

    def nebulae(self):
        columns = {"ambientSound": "Nebula ambience", "lightningSound": "Nebula lightning",
                   "lightningSoundLoop": "Nebula lightning loop"}
        for scene, places in self.by_scene.items():
            ref = f"datatables/space/nebula/{scene}.iff"
            for number, row in enumerate(self.table(ref), 1):
                try:
                    center = [float(row[axis]) for axis in ("x", "y", "z")]
                    radius = float(row["radius"])
                    if radius <= 0 or not all(isfinite(n) for n in (*center, radius)):
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
                locations = [canonical_location(scene)]
                for location, place in places.items():
                    if location in locations or not (point := _coordinates(place)) or None in point:
                        continue
                    if sum((a - b) ** 2 for a, b in zip(center, point)) <= radius ** 2:
                        locations.append(location)
                for ident, label in _audible_fields(row, columns):
                    if label == "Nebula ambience" and not row.get("ambientSoundVolume", 1):
                        continue
                    if label != "Nebula ambience" and not row.get("lightningFrequency", 0):
                        continue
                    use = {"label": label, "context": row.get("name"), "coordinates": center,
                           "radius": radius, "source": {"path": "clientdata/" + ref, "row": number}}
                    for location in locations:
                        self.add(location, ident, use)

    def scripted(self, files, spatial, nearby):
        attachments = defaultdict(list)
        for script, path in script_paths(files).items():
            text = files.text(path)
            if EFFECT_CALL not in text.lower() and not any(name in text for name in PLACED_TRIGGERS):
                continue
            parsed = Script(path, text)
            for asset, setting, effect, point, cue in _script_uses(parsed):
                if effect:
                    source_asset = asset.casefold()
                    cue["effect"] = {"path": "clientdata/" + source_asset}
                    assets = _effect_audio(self.root, source_asset)
                else:
                    assets = (asset,) if asset else ()
                if not (assets or setting):
                    continue
                if point:
                    scene, coordinates = point
                    site = {"scene": scene, "coordinates": coordinates}
                    for ident in assets:
                        self._at_placement(site, ident, cue, nearby)
                else:
                    attachments[script].append((assets, setting, cue))
        if not attachments:
            return
        sound_scripts = set(attachments)
        buildout_rows = lru_cache(maxsize=64)(lambda path: _script_buildout_rows(files, path))
        for point in spatial.buildout_objects():
            if not point.get("coordinates"):
                continue
            proof = point["source"]
            attached, overrides = buildout_rows(proof["path"]).get(proof["line"], ((), {}))
            template_scripts, defaults = spatial._template_spawn_settings(point["template"])
            scripts = sound_scripts.intersection(set(attached) | template_scripts.keys())
            if not scripts:
                continue
            for script in sorted(scripts):
                for assets, setting, cue in attachments[script]:
                    if setting:
                        if not (ident := sound_id(overrides.get(setting, defaults.get(setting)))):
                            continue
                        assets = (ident,)
                    use = {**cue, "placement": proof,
                           **({"attachment": template_scripts[script]} if script in template_scripts else {})}
                    for ident in assets:
                        self._at_placement(point, ident, use, nearby)

    def _at_placement(self, point, ident, cue, nearby):
        scene = point["scene"]
        locations = {canonical_location(scene), *nearby(point)}
        for proof in point.get("parent_sources", []):
            if building := self.buildings_by_source.get((scene, proof["path"], proof.get("line"))):
                locations.add(building)
                break
        use = {**cue, "coordinates": point["coordinates"]}
        if name := (point.get("cell") or {}).get("name"):
            use["cells"] = [name]
        if point.get("required_event"):
            use["required_event"] = point["required_event"]
        for location in sorted(locations):
            self.add(location, ident, use)

    def publish(self):
        for (location, ident), entries in self.uses.items():
            reason = " · ".join(dict.fromkeys(use["label"] for use in entries))
            detail = {"reason": reason, "ambient_uses": entries}
            self.writer.link("locations", location, "sounds", "sounds", ident, detail)
            self.writer.link("sounds", ident, "locations", "locations", location, detail)
        return {"locations": len({location for location, _ in self.uses}),
                "sounds": len({ident for _, ident in self.uses}), "links": len(self.uses)}

PLACED_TRIGGERS = {"OnTriggerVolumeEntered", "OnTriggerVolumeExited"}
EFFECT_CALL = "playclienteffectloc"
EFFECT_PATH = re.compile(r"clienteffect/[\w/.-]+\.cef", re.I)
FIXED_LOCATION = re.compile(r"\bnew\s+location\s*\(")
LOCATION_ASSIGNMENT = re.compile(r"\b(?:location\s+)?(\w+)\s*=\s*(new\s+location\s*\()")
SETTING_ASSIGNMENT = re.compile(r"\bString\s+(\w+)\s*=\s*getStringObjVar\s*\(")


def _fixed_point(expression, symbols):
    match = FIXED_LOCATION.match(expression)
    if not match:
        return None
    args = arguments(expression, match.end() - 1)
    if len(args) not in (4, 5) or (len(args) == 5 and args[4] not in ("null", "obj_id.NULL_ID", "0")):
        return None
    scene = literal(args[3], symbols)
    if not isinstance(scene, str) or not re.fullmatch(r"[\w-]+", scene):
        return None
    try:
        numbers = [literal(arg, symbols) for arg in args[:3]]
        position = [float(number if number is not None else arg.rstrip("fF")) for arg, number in zip(args, numbers)]
    except ValueError:
        return None
    if all(isfinite(axis) for axis in position):
        return scene.casefold(), position
    return None


def _script_uses(parsed):
    for name, method in parsed.methods.items():
        symbols = {**parsed.constants, **assignments(method.body)}
        locations = defaultdict(list)
        variables = {}
        for assignment in LOCATION_ASSIGNMENT.finditer(method.body):
            location = _fixed_point(method.body[assignment.start(2):], symbols)
            if location:
                locations[assignment[1]].append(location)
        for assignment in SETTING_ASSIGNMENT.finditer(method.body):
            args = arguments(method.body, assignment.end() - 1)
            if len(args) == 2 and args[0] == "self":
                setting = literal(args[1], symbols)
                if isinstance(setting, str):
                    variables[assignment[1]] = setting
        for call in CALL.finditer(method.body):
            kind = call[1].lower()
            effect = kind == EFFECT_CALL
            if not (effect or name in PLACED_TRIGGERS and kind in PLAY_CALLS):
                continue
            args = arguments(method.body, call.end() - 1)
            if len(args) < (3 if effect else 2):
                continue
            asset = literal(args[1], symbols)
            point = None
            setting = None
            if effect:
                if not isinstance(asset, str) or not EFFECT_PATH.fullmatch(asset) or ".." in asset.split("/"):
                    continue
                position = args[2]
                point = _fixed_point(position, symbols)
                if not point and len(locations[position]) == 1:
                    point = locations[position][0]
                if not point and not re.fullmatch(r"getLocation\s*\(\s*self\s*\)", position):
                    continue
            else:
                asset = sound_id(asset)
                setting = variables.get(args[1]) if not asset else None
                if not asset and not setting:
                    continue
            cue = {"label": "Scripted positional effect" if effect else
                   "Scripted area entry" if name == "OnTriggerVolumeEntered" else "Scripted area exit",
                   "source": parsed.evidence(method, call.start())}
            yield asset, setting, effect, point, cue


@lru_cache(maxsize=2048)
def _effect_audio(root, asset):
    try:
        node = next(chunks(memoryview((root / asset).read_bytes())))
        data = version(node, "0001", "0002", "0003")
        return tuple(dict.fromkeys(ident for part in chunks(data.body)
                                  if part.tag == "PSND" and (ident := sound_id(value(part.body)))))
    except READ_ERRORS:
        return ()


def _script_buildout_rows(files, path):
    return {row.line: (script_ids(row.get("scripts")), _vars(row.get("objvars")))
            for row in files.tab(ROOT / path)}
