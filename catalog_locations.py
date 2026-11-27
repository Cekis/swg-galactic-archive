"""Resolve authored SWG placements and project them onto the supplied game maps."""

from __future__ import annotations

import io
import math
import re
import struct
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from catalog_sources import ROOT, SERVER, SHARED, read_iff_table, source
from catalog_iff import block_positions, form

BUILDOUT = SHARED / "buildout"
ASSET_NAME = re.compile(r"[a-z0-9_/-]+", re.ASCII)
BUILDOUT_ENGINE = "src/engine/server/library/serverGame/src/shared/core/ServerBuildoutManager.cpp"
CELL_TEMPLATE = "object/cell/"
NPC_TEMPLATES = ("object/mobile/", "object/creature/")
AREA_SPAWNER = "systems.spawning.spawner_area"
LIST_SPAWNERS = {
    AREA_SPAWNER: "Area spawner",
    "systems.spawning.spawner_patrol": "Patrol spawner",
    "systems.spawning.spawner_random": "Random spawner",
    "systems.spawning.spawner_random_proxy": "Random spawner",
}
POI_SPAWNERS = {
    "poi.interior_spawner.interior_spawner": "Interior spawn egg",
    "poi.exterior_spawner.exterior_spawner": "Exterior spawn egg",
}
STATIC_SPAWNER = "systems.spawning.mob_spawner"
FORMATION_SPAWNER = "theme_park.patrols.patrol_controller"
HRACCA_SPAWNER = "theme_park.kashyyyk.hracca_spawner"
BET_SPAWNER = "theme_park.kashyyyk.bocctyyy_bet_spawner"
OUTBREAK_SPAWNER = "theme_park.outbreak.camp_defense_spawner"
DELIVERY_SPAWNER = "theme_park.outbreak.delivery_beast_spawner"
JAWA_SPAWNER = "city.jawa_spawner"
TUSKEN_SPAWNER = "city.bestine.tusken_master_spawner"
INTELLECT_SPAWNER = "quest.hero_of_tatooine.intellect_spawner"
BATTLE_SPAWNER = "creature_spawner.rebels_vs_imperials_yavin4"
DUMPSTER_SPAWNER = "theme_park.nym.dumpster_spawner"
ENCOUNTER_SPAWNERS = {
    TUSKEN_SPAWNER: ("Tusken reinforcements spawner", "Per wave"),
    INTELLECT_SPAWNER: ("Hero of Tatooine quest spawner", "Per encounter"),
    BATTLE_SPAWNER: ("Rebel and Imperial battle spawner", "Initial wave"),
}
SPECIAL_SPAWNERS = (FORMATION_SPAWNER, HRACCA_SPAWNER, BET_SPAWNER, OUTBREAK_SPAWNER,
                    DELIVERY_SPAWNER, JAWA_SPAWNER, DUMPSTER_SPAWNER, *ENCOUNTER_SPAWNERS)
CELEBRITY_SPAWNER = "npc.celebrity.celebrity_spawner"
BUILDING_TABLE_SCRIPTS = ("theme_park.dungeon.generic_spawner", "theme_park.dungeon.ep3_clone_relics_generic_spawner")
MASTER_SPAWNER = "Building master spawner"
DEDICATED_SPAWNER = "Dedicated spawner object"
SENTINEL_SPAWNERS = {
    f"object/tangible/jedi/jedi_sentinel_{side}_spawner.iff": f"creature_spawner.jedi_sentinel_{side}_spawner"
    for side in ("dark", "light")
}
OBJECT_ASSET = re.compile(r"object/[a-z0-9_/-]+\.iff")
SCRIPT_ARRAY = re.compile(r"^\s*scripts\s*=\s*(\+?)\s*\[([^\]]*)\]", re.M)
TEMPLATE_VARS = re.compile(r"^\s*objvars\s*=\s*(\+?)\s*\[([^\]]*)\]", re.I | re.M)
TEMPLATE_VALUE = re.compile(r'"([\w.]+)"\s*=\s*(?:"([^"]*)"|(-?\d+(?:\.\d+)?))')
TEMPLATE_BASE = re.compile(r"^@base\s+(object/\S+\.iff)", re.M)
MAP_LAYOUT = "ui/ui_planet_map.inc"
MAP_ADJUSTMENTS = "datatables/planetary_map/map_adjustments.iff"
INSTANCE_AREAS = "datatables/instance/instance_areas.iff"
UI_MAP_ENTRY = re.compile(
    r"<page\b([^>]*?\bName='([a-z0-9_]+)'[^>]*)>\s*"
    r"<image\b([^>]*?\bSourceResource='(ui_map_[a-z0-9_]+)'[^>]*)>",
    re.I,
)
IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


class MapLibrary:
    """Serve decoded game map textures from persisted map metadata."""

    def __init__(self, clientdata, maps=None):
        self.clientdata = Path(clientdata).resolve() if clientdata else None
        self.maps = maps or {}

    def map_image(self, metadata, *, thumbnail=False):
        if isinstance(metadata, str):
            metadata = self.maps.get(metadata)
        asset = metadata.get("asset") if metadata else None
        rect = metadata.get("source_rect") if metadata else None
        if not asset or not re.fullmatch(r"texture/ui_map_[a-z0-9_]+\.dds", asset):
            return None
        return self._decode(asset, tuple(rect) if rect else None, thumbnail)

    @lru_cache(maxsize=24)
    def _decode(self, key, rect, thumbnail):
        path = _asset(self.clientdata, key)
        if not path:
            return None
        try:
            from PIL import Image
            with Image.open(path) as original:
                if original.width > 4096 or original.height > 4096:
                    return None
                image = original.convert("RGBA")
                if rect and rect != (0, 0, image.width, image.height):
                    x, y, x2, y2 = rect
                    image = image.crop((max(0, x), max(0, y), min(image.width, x2), min(image.height, y2)))
                if thumbnail:
                    image.thumbnail((384, 256), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, "PNG")
                return "image/png", output.getvalue()
        except (ImportError, OSError, ValueError, NotImplementedError):
            return None


def _asset(clientdata, key):
    if clientdata is None:
        return None
    candidate = (clientdata / key).resolve()
    if candidate.is_relative_to(clientdata) and candidate.is_file():
        return candidate
    return None


def _numeric(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _vector(row, columns):
    values = tuple(_numeric(row.get(column)) for column in columns)
    return None if None in values else values


def _object_id(value):
    try:
        return int(value) or None
    except (TypeError, ValueError):
        return None


def _rectangle(row, columns):
    rect = _vector(row, columns)
    return rect if rect and rect[0] < rect[2] and rect[1] < rect[3] else None


def _rotation(quaternion):
    # Quaternion::getTransformPreserveTranslation uses the authored values directly.
    w, x, y, z = quaternion
    if w >= 1:
        return IDENTITY
    return (
        (1 - 2 * (y*y + z*z), 2 * (x*y - w*z), 2 * (x*z + w*y)),
        (2 * (x*y + w*z), 1 - 2 * (x*x + z*z), 2 * (y*z - w*x)),
        (2 * (x*z - w*y), 2 * (y*z + w*x), 1 - 2 * (x*x + y*y)),
    )


def _rotate(rotation, point):
    return tuple(sum(a * b for a, b in zip(axis, point)) for axis in rotation)


def _compose(parent, position, rotation):
    origin, orientation = parent
    world = tuple(a + b for a, b in zip(origin, _rotate(orientation, position)))
    axes = tuple(zip(*rotation))
    world_rotation = tuple(tuple(sum(a * b for a, b in zip(row, axis)) for axis in axes)
                           for row in orientation)
    return world, world_rotation


def _display_coordinates(vector):
    return [round(value, 5) for value in vector] if vector is not None else None


def _vars(text):
    # Buildout dynamic variable records are key|type|value triples.
    fields = text.split("|")
    return {name: value for name, _, value in zip(fields[0::3], fields[1::3], fields[2::3])
            if name and name != "$"}


@dataclass(slots=True, frozen=True)
class Area:
    scene: str
    name: str
    index: int
    bounds: tuple
    composite: str
    composite_bounds: tuple | None
    event: str
    source: dict


@dataclass(slots=True, eq=False)
class Placement:
    scene: str
    template: str
    path: str
    line: int
    area: Area | None
    object_id: int | None
    parent_id: int | None
    cell_index: int
    local: tuple | None
    rotation: tuple
    portal_crc: int | None
    parent: Placement | None
    failure: str | None
    label: str | None = None

    @property
    def source(self):
        return {"path": self.path, "line": self.line}

    @property
    def runtime_id(self):
        if self.object_id is not None and self.object_id < 0 and self.area is not None:
            return self.object_id ^ ((self.area.index + 1) << 48)
        return self.object_id

    @property
    def placement_key(self):
        if self.runtime_id is not None:
            return str(self.runtime_id)
        return f"{self.area.index if self.area else 'unregistered'}/{Path(self.path).stem}/{self.line}"


class _ObjectLocations(Mapping):
    """Read-only lookup. Repeated negative source IDs are scoped by their area."""

    def __init__(self, catalog):
        self.catalog = catalog

    def __getitem__(self, key):
        scene, object_id = key
        result = self.catalog.lookup_id(scene, object_id)
        if result is None:
            raise KeyError(key)
        return result

    def __iter__(self):
        return ((scene, str(object_id)) for (scene, object_id), records in self.catalog._objects.items()
                if len(records) == 1)

    def __len__(self):
        return sum(len(records) == 1 for records in self.catalog._objects.values())


class LocationIndex:
    """Provide world-space NPC, object and map locations from read-only game sources."""

    def __init__(self, files, clientdata: Path | None = None, *, report_issue=None):
        self.files = files
        self.clientdata = Path(clientdata).resolve() if clientdata else None
        self.report_issue = report_issue or (lambda *_: None)
        self.areas = self._read_areas()
        self._authored_mobs = {row.get("creatureName") for row in self._table(SERVER / "mob/creatures.tab")}
        self._sources = defaultdict(list)
        self._objects = defaultdict(list)
        self._poi_sources = {}
        self._mob_sources = defaultdict(set)
        self._building_spawns = []
        self._spawner_details = {}
        self._keyed_spawners = {}
        self.script_sites = {}
        self.table_spawn_points = []
        self.building_spawn_points = []
        self._building_cells = defaultdict(dict)
        self.by_object = _ObjectLocations(self)
        self.by_mob = defaultdict(list)
        self.places = {}
        self.maps = self._create_maps()
        self._map_images = MapLibrary(self.clientdata, self.maps)
        self._index_buildouts()
        self._index_keyed_spawners()
        self._index_building_spawns()
        self._index_poi()
        self._index_space_npcs()
        self._index_places()
        self._mob_sources.clear()

    def _table(self, path):
        return self.files.tab(path) if self.files.exists(path) else ()

    def _read_areas(self):
        registry = BUILDOUT / "buildout_scenes.tab"
        if not self.files.exists(registry):
            self.report_issue("Buildouts", f"Missing source scene registry {registry.as_posix()}.")
        scene_indexes = {row.get("sceneName"): index for index, row in enumerate(self._table(registry))}
        for scene in scene_indexes:
            path = BUILDOUT / f"areas_{scene}.tab"
            if scene and not self.files.exists(path):
                self.report_issue("Buildouts", f"Missing source area registration {path.as_posix()}.")
        areas = defaultdict(list)
        for path in self.files.paths(BUILDOUT, "areas_*.tab"):
            scene = path.stem.removeprefix("areas_")
            if scene not in scene_indexes:
                continue
            for index, row in enumerate(self._table(path)):
                name = row.get("area")
                bounds = _rectangle(row, ("x1", "z1", "x2", "z2"))
                if name and bounds:
                    areas[scene].append(Area(
                        scene, name, 100 * scene_indexes[scene] + index, bounds,
                        row.get("compositeName"),
                        _rectangle(row, ("compositeX1", "compositeZ1", "compositeX2", "compositeZ2")),
                        row.get("eventRequired"), row.source,
                    ))
        return dict(areas)

    def _index_buildouts(self):
        area_keys = defaultdict(list)
        for regions in self.areas.values():
            for area in regions:
                area_keys[area.scene, area.name].append(area)
        for path in self.files.paths(SERVER / "buildout", "*/*.tab"):
            scene = path.parent.name
            rows = self._table(path)
            explicit_ids = bool(rows and "objid" in rows[0].cells)
            records = [(row, _object_id(row.get("objid"))) for row in rows if row.get("server_template_crc")]
            ids = Counter(object_id for _, object_id in records if object_id is not None)
            for area in area_keys.get((scene, path.stem), (None,)):
                known = {}
                current_building = None
                current_cell = None
                for row, object_id in records:
                    template = row.get("server_template_crc")
                    parent_id = _object_id(row.get("container"))
                    cell_index = _object_id(row.get("cell_index")) or 0
                    parent = known.get(parent_id)
                    failure = None
                    if not explicit_ids:
                        # Version one buildouts identify rooms by ordered building and cell rows.
                        if template.startswith(CELL_TEMPLATE):
                            parent = current_building
                        elif cell_index:
                            parent = current_cell
                    if explicit_ids and object_id is None:
                        failure = "invalid_object_id"
                    elif ids[object_id] > 1:
                        failure = "ambiguous_object_id"
                    elif (parent_id or cell_index) and parent is None:
                        failure = "missing_buildout_parent"
                    elif parent and not cell_index:
                        failure = "ambiguous_coordinate_frame"
                    local = _vector(row, ("px", "py", "pz"))
                    q = tuple(_numeric(row.get(axis), 0.0) for axis in ("qw", "qx", "qy", "qz"))
                    raw_vars = row.get("objvars")
                    variables = _vars(raw_vars) if "portalProperty.crc|" in raw_vars or "travel.point_name|" in raw_vars else {}
                    portal_crc = _object_id(variables.get("portalProperty.crc"))
                    placement = Placement(
                        scene, template, row.source["path"], row.source["line"], area, object_id, parent_id,
                        cell_index, local, q, portal_crc, parent, failure, variables.get("travel.point_name"),
                    )
                    if object_id is not None:
                        known.setdefault(object_id, placement)
                    elif not explicit_ids and template.startswith("object/building/"):
                        current_building, current_cell = placement, None
                    elif not explicit_ids and template.startswith(CELL_TEMPLATE):
                        current_cell = placement
                    self._sources[placement.path, placement.line].append(placement)
                    if object_id is not None:
                        self._objects[scene, object_id].append(placement)
                        if placement.runtime_id != object_id:
                            self._objects[scene, placement.runtime_id].append(placement)
                    if parent and template.startswith(CELL_TEMPLATE):
                        self._building_cells[parent][cell_index] = placement
                    self._index_buildout_npc(placement, row.get("scripts"), raw_vars)

    @lru_cache(maxsize=32768)
    def _transform(self, record):
        if record.failure:
            return None, record.failure
        if record.area is None:
            return None, "unregistered_buildout"
        if record.local is None:
            return None, "invalid_coordinates"
        rotation = _rotation(record.rotation)
        if record.parent is None:
            x, y, z = record.local
            xmin, zmin, _, _ = record.area.bounds
            return ((x + xmin, y, z + zmin), rotation), "authored_world"
        transform, error = self._transform(record.parent)
        if transform is None:
            return None, error
        return _compose(transform, record.local, rotation), "transformed_cell"

    def _placement(self, record):
        transform, status = self._transform(record)
        parent = record.parent
        cell = None
        lineage = []
        while parent is not None:
            lineage.append(parent.source)
            if cell is None and parent.template.startswith(CELL_TEMPLATE):
                cell = {"object_id": str(parent.object_id) if parent.object_id else None,
                        "index": parent.cell_index, "source": parent.source}
                if parent.parent and self.clientdata:
                    rooms = self._portal_rooms(parent.parent.template)
                    if rooms and parent.cell_index in rooms[1] and (
                        parent.parent.portal_crc is None or parent.parent.portal_crc == rooms[2]
                    ):
                        cell.update(name=rooms[1][parent.cell_index], layout_asset=rooms[0])
            parent = parent.parent
        coordinates = _display_coordinates(transform[0]) if transform else None
        point = {
            "scene": record.scene, "coordinates": coordinates, "status": status,
            "source": record.source, "local_coordinates": _display_coordinates(record.local),
            "coordinate_frame": "cell" if record.parent or record.cell_index else "buildout_area",
            "cell_index": record.cell_index or None,
            "area": record.area.name if record.area else Path(record.path).stem,
            "area_index": record.area.index if record.area else None,
            "area_source": record.area.source if record.area else None,
            "required_event": (record.area.event or None) if record.area else None,
            "object_id": str(record.object_id) if record.object_id is not None else None,
            "runtime_object_id": str(record.runtime_id) if record.runtime_id is not None else None,
            "cell": cell, "parent_sources": lineage, "template": record.template,
            "map_id": self.map_id_for(record.scene, coordinates),
        }
        if info := self._spawner_details.get(record):
            point["spawner"] = info
            if info.get("radius") is not None:
                point["radius"] = info["radius"]
        return point

    def buildout_objects(self):
        """Iterate physical buildout occurrences, including repeated area tables."""
        for records in self._sources.values():
            for record in records:
                point = self._placement(record)
                point.update(placement_key=record.placement_key, portal_crc=record.portal_crc,
                             world_transform=self._transform(record)[0],
                             parent_key=record.parent.placement_key if record.parent else None, label=record.label)
                if point["status"] == "missing_buildout_parent" and record.area and record.parent_id:
                    parent_id = record.parent_id
                    if parent_id < 0:
                        parent_id ^= (record.area.index + 1) << 48
                    point["required_parent"] = str(parent_id)
                    point["parent_key"] = str(parent_id)
                yield point

    def lookup_all(self, proof, scene=None, object_id=None):
        """Retain all registered placements when a source row is used in multiple areas."""
        proof = proof or {}
        path = str(proof.get("path", "")).replace("\\", "/")
        line = _object_id(proof.get("line"))
        exact = self._poi_sources.get((path, line))
        if exact and (scene is None or exact["scene"] == scene):
            return [exact]
        candidates = self._sources.get((path, line), ())
        if not candidates and scene and object_id is not None:
            candidates = self._objects.get((scene, _object_id(object_id)), ())
        return [self._placement(record) for record in candidates
                if (scene is None or record.scene == scene)
                and (proof.get("area_index") is None or record.area and record.area.index == proof["area_index"])
                and (object_id is None or _object_id(object_id) in (record.object_id, record.runtime_id))]

    def lookup(self, proof, scene=None, object_id=None):
        candidates = self.lookup_all(proof, scene, object_id)
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            return {"scene": scene or candidates[0]["scene"], "coordinates": None,
                    "status": "multiple_buildout_placements", "source": proof, "alternatives": candidates}
        return None

    def lookup_id(self, scene, object_id):
        records = self._objects.get((scene, _object_id(object_id)), ())
        return self._placement(records[0]) if len(records) == 1 else None

    def _add_mob(self, mob_id, location):
        if not mob_id:
            return
        source = location["source"]
        key = (source["path"], source.get("line"), location.get("area_index"),
               location.get("kind"), location.get("building_id"))
        if key not in self._mob_sources[mob_id]:
            self._mob_sources[mob_id].add(key)
            self.by_mob[mob_id].append(location)

    @lru_cache(maxsize=4096)
    def _spawn_types(self, key):
        if not ASSET_NAME.fullmatch(key):
            return ()
        path = SERVER / "spawning/ground_spawning/types" / f"{key}.tab"
        return tuple((row.get("strItem"), row.source) for row in self._table(path) if row.get("strItem"))

    def _spawn_entry(self, ref, proof=None):
        name = Path(ref).stem.removeprefix("shared_") if ref.startswith(NPC_TEMPLATES) else ref
        return {"ref": ref, "npc": name if name in self._authored_mobs else None, "source": proof}

    @lru_cache(maxsize=8192)
    def _template_spawn_settings(self, template):
        chain = []
        seen = set()
        while template and template not in seen and OBJECT_ASSET.fullmatch(template):
            seen.add(template)
            path = SERVER.parent / Path(template).with_suffix(".tpf")
            if not self.files.exists(path):
                break
            text = self.files.text(path)
            chain.append((path, text))
            base = TEMPLATE_BASE.search(text)
            template = base[1] if base else None
        scripts = {}
        variables = {}
        for path, text in reversed(chain):
            if match := SCRIPT_ARRAY.search(text):
                if not match[1]:
                    scripts.clear()
                for name in re.finditer(r'"([\w.]+)"', match[2]):
                    line = 1 + text.count("\n", 0, match.start(2) + name.start())
                    scripts[name[1].removeprefix("script.")] = source(path, line)
            if match := TEMPLATE_VARS.search(text):
                if not match[1]:
                    variables.clear()
                variables.update((name, value or number) for name, value, number in TEMPLATE_VALUE.findall(match[2]))
        return scripts, variables

    @lru_cache(maxsize=128)
    def _spawn_script(self, name):
        stem = SERVER.parent / "script" / Path(*name.split("."))
        for extension in (".java", ".script", ".scriptlib"):
            path = stem.with_suffix(extension)
            if self.files.exists(path):
                return {"path": path.relative_to(ROOT).as_posix()}
        return None

    def _spawn_choices(self, choices):
        entries = {}
        for spawn, proof in choices:
            entry = entries.setdefault(spawn, {**self._spawn_entry(spawn, proof), "rows": 0})
            entry["rows"] += 1
        return list(entries.values())

    @lru_cache(maxsize=32)
    def _patrol_choices(self, faction, squad):
        squads = {"mixed", "plain"} if squad == "mixed" else {squad}
        return tuple((row.get("template"), row.source) for row in self._table(SERVER / "spawning/patrol_spawner.tab")
                     if row.get("template") and row.get("faction") == faction and row.get("squadClass") in squads
                     and (row.get("leader") == "1" or _numeric(row.get("maxSpawnChance"), 0) >= 1
                          and _numeric(row.get("minSpawnChance"), 0) <= 100))

    @lru_cache(maxsize=16)
    def _script_choices(self, script, pattern, *, before=None):
        proof = self._spawn_script(script)
        if not proof:
            return ()
        text = self.files.text(ROOT / proof["path"])
        if before:
            text = text.partition(before)[0]
        return tuple((match[1], {**proof, "line": 1 + text.count("\n", 0, match.start(1))})
                     for match in re.finditer(pattern, text, re.M))

    def _special_spawner(self, record, script, variables):
        config = {"type": "Patrol spawner", "source": record.source, "script_source": self._spawn_script(script),
                  "count_label": "Maximum active", "selection": "conditional"}
        if script == FORMATION_SPAWNER:
            count = _object_id(variables.get("size"))
            choices = self._patrol_choices(variables.get("faction", "neutral"), variables.get("squadClass"))
            config.update(selection="random", delay_min=_numeric(variables.get("respawnTime")))
        elif script == HRACCA_SPAWNER:
            count = _object_id(variables.get("intSpawnCount")) or 1
            targets = {"chiss": "ep3_hracca_chiss_poacher_hunter", "kkorrwrot": "ep3_hracca_kkorrwrot"}
            choices = tuple(choice for choice in self._script_choices(script, r'^\s*(?:string\s+)?whatToSpawn\s*=\s*"([\w-]+)"')
                            if choice[0] == targets.get(variables.get("spawnType")))
            config.update(type="Hracca encounter spawner")
        elif script == BET_SPAWNER:
            count = _object_id(variables.get("intSpawnCount")) or 1
            choices = self._script_choices(script, r'^\s*spawnType\s*=\s*"([\w-]+)"')
            config.update(type="Bocctyyy hunting quest spawner", radius=3)
        elif script == OUTBREAK_SPAWNER:
            count = _object_id(variables.get("numberOfEnemies"))
            choices = self._script_choices(script, r'^\s*public static final String MOB_TO_SPAWN\s*=\s*"([\w-]+)"')
            config.update(type="Camp defense spawner", count_label="Per encounter")
        elif script == DELIVERY_SPAWNER:
            creature = variables.get("creatureName")
            choices = ((creature, record.source),) if creature else ()
            count = 1 if choices else None
            config.update(type="Outbreak escort spawner")
        elif script == JAWA_SPAWNER:
            count = _object_id(variables.get("pop", "20"))
            choices = self._script_choices(script, r'^\s*spawn\s*=\s*"([\w-]+)"')
            config.update(type="Jawa spawner", selection="random")
        elif script == DUMPSTER_SPAWNER:
            choices = self._script_choices(script, r'create\.object\s*\(\s*"(object/[^"]+\.iff)"')
            count = len(choices) or None
            config.update(type="Quest object spawner", count_label="Per activation", selection="fixed")
        else:
            label, count_label = ENCOUNTER_SPAWNERS[script]
            before = "messageHandler imperialDied" if script == BATTLE_SPAWNER else None
            choices = self._script_choices(script, r'create\.object\s*\(\s*"([\w-]+)"', before=before)
            config.update(type=label, count_label=count_label, selection="fixed")
            if script == INTELLECT_SPAWNER:
                row = next((row for row in self._table(SERVER / "quest/hero_of_tatooine/intellect_spawner.tab")
                            if _object_id(row.get("spawner_id")) == record.runtime_id), None)
                if not row:
                    return {**config, "entries": [], "configuration_status": "missing_row"}
                config["source"] = row.source
            count = len(choices) or None
        entries = self._spawn_choices(choices)
        if script in ENCOUNTER_SPAWNERS:
            for entry in entries:
                entry["count"] = entry.pop("rows")
        elif count is not None and len(entries) == 1:
            entries[0]["count"] = count
            if script == DUMPSTER_SPAWNER:
                entries[0]["item"] = entries[0]["ref"]
        config.update(entries=entries, count=count, configuration_status="configured" if entries else "scripted")
        return config

    @lru_cache(maxsize=2)
    def _sentinel_spawn(self, script):
        choices = self._script_choices(script, r'^\s*string\s+whatToSpawn\s*=\s*"([\w-]+)"')
        if len(choices) != 1:
            return None
        creature, proof = choices[0]
        delays = self._script_choices("theme_park.npc_died.npc_died",
                                     r'\bmessageTo\s*\(\s*spawner,\s*"npcDied",\s*npcParams,\s*(\d+)')
        return {
            "type": "Jedi sentinel spawner", "entries": [{**self._spawn_entry(creature, proof), "count": 1}],
            "count": 1, "count_label": "Per activation", "selection": "fixed",
            "script_source": proof, "configuration_status": "configured",
            "delay_min": int(delays[0][0]) if delays else None, "delay_source": delays[0][1] if delays else None,
        }

    def scripted_spawn_definitions(self):
        for template, script in SENTINEL_SPAWNERS.items():
            scripts, _ = self._template_spawn_settings(template)
            if script in scripts and (config := self._sentinel_spawn(script)):
                path = (SERVER.parent / template).with_suffix(".tpf")
                yield template, {**config, "source": {"path": path.relative_to(ROOT).as_posix()}}

    def _record_spawner(self, record, config, kind, *, area=False, register_mobs=True):
        self._spawner_details[record] = config
        if not config["entries"] or not register_mobs:
            return None
        point = self._placement(record)
        point["kind"] = kind
        if point["status"] == "missing_buildout_parent" and record.local:
            parents = self._objects.get((record.scene, record.parent_id), ())
            if len(parents) == 1:
                parent = parents[0]
                transform, _ = self._transform(parent)
                if transform and parent.template.startswith(CELL_TEMPLATE) and parent.cell_index == record.cell_index:
                    position = _display_coordinates(_compose(transform, record.local, IDENTITY)[0])
                    point.update(coordinates=position, status="transformed_cell",
                                 map_id=self.map_id_for(record.scene, position),
                                 cell={"index": parent.cell_index, "object_id": str(parent.object_id),
                                       "source": parent.source})
        if area and point["coordinates"]:
            point["status"] = "spawn_area"
        for entry in config["entries"]:
            ref = entry["ref"]
            if entry["npc"]:
                self._add_mob(entry["npc"], {**point, "spawn_type_source": entry["source"]})
            elif ref.startswith(NPC_TEMPLATES):
                self._add_mob(Path(ref).stem.removeprefix("shared_"),
                              {**point, "spawn_type_source": entry["source"], "npc_template": ref})
        return point

    def _index_buildout_npc(self, record, scripts, raw_vars):
        inherited, defaults = self._template_spawn_settings(record.template)
        attached = {name.removeprefix("script.") for name in re.split(r"[:,\s]+", scripts.strip('"')) if name}
        scripts = {name: [{"kind": "template", **proof}] for name, proof in inherited.items()}
        for name in sorted(attached):
            scripts.setdefault(name, []).append({"kind": "buildout", **record.source})
        if scripts or "spawn" in record.template or "/lair/" in record.template:
            self.script_sites[record] = (scripts, raw_vars)
        if record.template.startswith(NPC_TEMPLATES):
            location = self._placement(record)
            location["kind"] = "direct_buildout"
            self._add_mob(Path(record.template).stem.removeprefix("shared_"), location)
        if record.template.startswith(("object/tangible/npc/", "object/tangible/poi/")):
            celebrity = self._celebrity_template(record.template)
            if celebrity and CELEBRITY_SPAWNER not in scripts:
                self._add_celebrity(record, celebrity[0], celebrity[1])
        list_script = next((key for key in LIST_SPAWNERS if key in scripts), None)
        keyed = next((name for name in (*POI_SPAWNERS, STATIC_SPAWNER) if name in scripts), None)
        if keyed:
            self._keyed_spawners[record] = keyed
            return
        sentinel = next((name for name in SENTINEL_SPAWNERS.values() if name in scripts), None)
        if sentinel and (config := self._sentinel_spawn(sentinel)):
            self._record_spawner(record, {**config, "source": record.source}, "configured_spawner")
            return
        if special := next((name for name in SPECIAL_SPAWNERS if name in scripts), None):
            config = self._special_spawner(record, special, {**defaults, **_vars(raw_vars)})
            self._record_spawner(record, config, "configured_spawner", area=bool(config.get("radius")),
                                 register_mobs=special not in (FORMATION_SPAWNER, JAWA_SPAWNER, *ENCOUNTER_SPAWNERS))
            return
        building_script = next((name for name in BUILDING_TABLE_SCRIPTS if name in scripts), None)
        if not list_script and CELEBRITY_SPAWNER not in scripts and not building_script:
            spawn_script = next((name for name in sorted(scripts) if "spawner" in name), None)
            if spawn_script and any(part in record.template for part in ("/spawnegg/", "/creature_spawner/", "/spawn_egg.iff")):
                self._spawner_details[record] = {"type": "Scripted spawn egg", "entries": [],
                                                 "configuration_status": "scripted", "script_source": self._spawn_script(spawn_script)}
            return
        overrides = _vars(raw_vars)
        variables = {**defaults, **overrides}
        if list_script:
            spawn_key = variables.get("strSpawns")
            authored = self._spawn_types(spawn_key) if spawn_key else ()
            spawns = authored or (((spawn_key, record.source),) if spawn_key else ())
            config = {
                "name": variables.get("strName"), "type": LIST_SPAWNERS[list_script], "table": spawn_key,
                "entries": self._spawn_choices(spawns), "total_rows": len(authored) or None,
                "selection": "random" if authored else "fixed",
                "count": _object_id(variables.get("intSpawnCount")), "count_label": "Maximum active",
                "radius": _numeric(variables.get("fltRadius")),
                "delay_min": _numeric(variables.get("fltMinSpawnTime")), "delay_max": _numeric(variables.get("fltMaxSpawnTime")),
                "patrol_type": variables.get("patrolPathType"),
                "script_source": self._spawn_script(list_script), "source": record.source,
                "configuration_status": "configured" if spawns else "unresolved",
            }
            self._record_spawner(record, config, "area_spawner" if list_script == AREA_SPAWNER else "configured_spawner",
                                 area=bool(config["radius"] or variables.get("intGoodLocationSpawner") == "1"))
        elif CELEBRITY_SPAWNER in scripts and variables.get("spawns"):
            template_evidence = self._celebrity_template(record.template)
            evidence = record.source if overrides.get("spawns") else template_evidence[1] if template_evidence else None
            self._add_celebrity(record, variables["spawns"], evidence)
        elif building_script and variables.get("spawn_table"):
            self._building_spawns.append((record, building_script, variables["spawn_table"], variables.get("portalProperty.crc")))

    def _index_keyed_spawners(self):
        indexes = {}
        for record, script in self._keyed_spawners.items():
            poi = script in POI_SPAWNERS
            table = "spawning/poi_spawner" if poi else "spawning/ground_spawning/static"
            path = SERVER / table / f"{record.scene}.tab"
            if path not in indexes:
                # The runtime dictionary lookup selects the first matching object ID.
                indexes[path] = {
                    _object_id((row.get("OBJ_ID") or row.get("w")) if poi else row.get("strObjId")): row
                    for row in reversed(self._table(path))
                }
            row = indexes[path].get(record.runtime_id) if record.runtime_id is not None else None
            config = {"type": POI_SPAWNERS[script] if poi else "Static mob spawner", "entries": [],
                      "configuration_status": "missing_row", "script_source": self._spawn_script(script)}
            if row and poi:
                count = _object_id(row.get("POP")) or 5
                entry = self._spawn_entry(row.get("TYPE") or "thug", row.source)
                radius = _numeric(row.get("RADIUS"), 0)
                if radius < 0 or (count > 1 and radius == 0):
                    radius = 1.5
                delay = _numeric(row.get("DELAY"), 0)
                config.update(entries=[{**entry, "count": count}], count=count, count_label="Maximum active",
                              selection="fixed", source=row.source, configuration_status="configured",
                              radius=radius, delay_min=delay if 0 < delay < 86400 else 120)
            elif row:
                group = row.get("strType")
                mob = row.get("strMob")
                choices = self._spawn_types(group) if group else ((mob, row.source),) if mob else ()
                config.update(entries=self._spawn_choices(choices), count=1 if choices else None,
                              count_label="Maximum active", name=row.get("strName"), table=group,
                              total_rows=len(choices) if group else None, selection="random" if group else "fixed",
                              configuration_status="configured" if choices else "unresolved", source=row.source,
                              delay_min=_numeric(row.get("fltMinSpawnTime")), delay_max=_numeric(row.get("fltMaxSpawnTime")))
            point = self._record_spawner(record, config, "poi_spawner" if poi else "configured_spawner",
                                         area=bool(config.get("radius")))
            if point and poi:
                point["spawn_type_source"] = row.source
                ref = row.get("TYPE")
                if ref and not entry["npc"] and not ref.startswith(NPC_TEMPLATES):
                    self._add_mob(ref, {**point, "status": "unresolved_creature_template"})
                if point["coordinates"]:
                    self._poi_sources.setdefault((row.source["path"], row.line), point)

    @lru_cache(maxsize=4096)
    def _celebrity_template(self, template):
        if not OBJECT_ASSET.fullmatch(template):
            return None
        path = SERVER.parent / Path(template).with_suffix(".tpf")
        text = self.files.text(path) if self.files.exists(path) else ""
        if '"' + CELEBRITY_SPAWNER + '"' not in text:
            return None
        match = re.search(r'"spawns"\s*=\s*"([a-zA-Z0-9_]+)"', text)
        if match:
            return match[1], {"path": path.relative_to(ROOT).as_posix(), "line": 1 + text.count("\n", 0, match.start())}
        return None

    @lru_cache(maxsize=2048)
    def _portal_rooms(self, template):
        if not OBJECT_ASSET.fullmatch(template) or self.clientdata is None:
            return None
        server = SERVER.parent / Path(template).with_suffix(".tpf")
        text = self.files.text(server) if self.files.exists(server) else ""
        shared = re.search(r'^\s*sharedTemplate\s*=\s*"([^"]+)"', text, re.M)
        if not shared or not OBJECT_ASSET.fullmatch(shared[1]):
            return None
        path = SHARED.parent / Path(shared[1]).with_suffix(".tpf")
        text = self.files.text(path) if self.files.exists(path) else ""
        match = re.search(r'^\s*portalLayoutFilename\s*=\s*"(appearance/[a-z0-9_/-]+\.pob)"', text, re.M)
        if match:
            candidates = {match[1]}
        else:
            asset = self._client_asset(shared[1])
            try:
                data = asset.read_bytes() if asset else b""
            except OSError:
                return None
            candidates = {key.decode("ascii") for key in re.findall(rb"appearance/[a-z0-9_/-]+\.pob", data)}
        if len(candidates) != 1:
            return None
        return self._read_portal_asset(candidates.pop())

    @lru_cache(maxsize=2048)
    def _read_portal_asset(self, asset_name):
        asset = self._client_asset(asset_name)
        try:
            if not asset:
                self.report_issue("Buildouts", f"Missing client portal layout {self.clientdata / asset_name}.")
                return None
            parsed = _portal_cells(asset)
            return (asset_name, *parsed) if parsed else None
        except (OSError, ValueError, IndexError, StopIteration, struct.error) as error:
            self.report_issue("Buildouts", f"Could not read client portal layout {asset}. {type(error).__name__}.")
            return None

    def spawn_origin(self, record, script, script_source, *, table_source=None, caller_source=None):
        master = record.template.startswith("object/building/")
        scripts, _ = self.script_sites[record]
        return {"type": MASTER_SPAWNER if master else DEDICATED_SPAWNER, "script": script,
                "script_source": script_source, "table_source": table_source, "caller_source": caller_source,
                "attachments": scripts.get(script, []), "template": None if master else record.template}

    def _index_building_spawns(self):
        for building, script, table, expected_layout in self._building_spawns:
            if not re.fullmatch(r"datatables/[a-z0-9_/-]+\.iff", table):
                continue
            path = SERVER.parent / Path(table).with_suffix(".tab")
            if not self.files.exists(path):
                self.report_issue("Buildouts", f"Missing building spawn table {path.as_posix()}.")
                continue
            script_source = self._spawn_script(script)
            layout = self._portal_rooms(building.template)
            expected_crc = _object_id(expected_layout) if expected_layout else building.portal_crc
            verified_layout = layout and (expected_crc is None or expected_crc & 0xffffffff == layout[2] & 0xffffffff)
            for row in self._table(path):
                spawn_ref = row.get("spawns")
                local = _vector(row, ("loc_x", "loc_y", "loc_z"))
                room = row.get("room")
                if not spawn_ref or not local:
                    continue
                mob_id = (Path(spawn_ref).stem.removeprefix("shared_") if spawn_ref.startswith(NPC_TEMPLATES)
                          else spawn_ref if "/" not in spawn_ref else None)
                source = row.source
                coordinates, cell = None, None
                status = "unproved_portal_cell"
                parent_sources = []
                exterior = room.casefold() == "world"
                if exterior:
                    coordinates, status = _display_coordinates(local), "authored_world"
                elif room and verified_layout:
                    matches = [(index, name) for index, name in layout[1].items()
                               if index and name.casefold() == room.casefold()]
                    if len(matches) == 1:
                        index, name = matches[0]
                        parent = self._building_cells[building].get(index)
                        parent_transform, status = self._transform(parent or building)
                        if parent_transform:
                            coordinates = _display_coordinates(_compose(parent_transform, local, IDENTITY)[0])
                            status = "transformed_cell"
                        cell = {"index": index, "name": name, "layout_asset": layout[0]}
                        if parent:
                            cell.update(object_id=str(parent.object_id) if parent.object_id else None, source=parent.source)
                            parent_sources.append(parent.source)
                point = {
                    "scene": building.scene, "coordinates": coordinates, "status": status,
                    "source": source, "kind": "building_spawn", "local_coordinates": _display_coordinates(local),
                    "coordinate_frame": "world" if exterior else "cell",
                    "cell": cell, "room": room, "name": row.get("name") or None,
                    "area_index": building.area.index if building.area else None,
                    "building_id": str(building.runtime_id) if building.runtime_id else None,
                    "building_key": building.placement_key, "building_source": building.source,
                    "required_event": (building.area.event or None) if building.area else None,
                    "parent_sources": [*parent_sources, building.source],
                    "anchor_source": building.source, "map_id": self.map_id_for(building.scene, coordinates),
                    "spawn_origin": self.spawn_origin(building, script, script_source, table_source=source),
                }
                self.table_spawn_points.append({**point, "spawn_ref": spawn_ref, "mob_id": mob_id,
                                                "delay": _numeric(row.get("respawn_time")),
                                                "trigger": row.get("required_config") or None})
                if room and not exterior:
                    self.building_spawn_points.append({
                        **point, "building_key": building.placement_key, "spawn_ref": spawn_ref, "mob_id": mob_id,
                    })
                self._add_mob(mob_id, point)

    def _add_celebrity(self, record, mob_id, evidence=None):
        self._spawner_details[record] = {
            "type": "Named NPC spawner", "entries": [self._spawn_entry(mob_id, evidence)],
            "count": 1, "count_label": "Maximum active", "script_source": self._spawn_script(CELEBRITY_SPAWNER),
        }
        location = self._placement(record)
        location["kind"] = "celebrity_spawner"
        location["spawn_type_source"] = evidence
        transform, _ = self._transform(record)
        if transform and not (record.scene == "kashyyyk_rryatt_trail"
                              and re.fullmatch(r"ep3_rryatt_trail_guide_[fm]_0[1-4]", mob_id)):
            # The normal script spawns half a meter above the anchor in its local frame.
            x, y, z = record.local
            offset = (x, y + 0.5, z)
            if record.parent:
                parent, _ = self._transform(record.parent)
                position = _compose(parent, offset, IDENTITY)[0]
            else:
                wx, wy, wz = transform[0]
                position = wx, wy + 0.5, wz
            location["coordinates"] = _display_coordinates(position)
            location["map_id"] = self.map_id_for(record.scene, position)
        self._add_mob(mob_id, location)

    def _index_poi(self):
        for path in self.files.paths(SERVER / "spawning/poi_spawner", "*.tab"):
            for row in self._table(path):
                scene, mob_id = row.get("PLANET") or path.stem, row.get("TYPE")
                if not mob_id:
                    continue
                source = row.source
                if (source["path"], row.line) in self._poi_sources:
                    continue
                radius = _numeric(row.get("RADIUS"), 0)
                local = _vector(row, ("LOC_X", "LOC_Y", "LOC_Z"))
                coordinates = _display_coordinates(local)
                status = "spawn_area" if radius else "authored_world"
                container = _object_id(row.get("BUILDING"))
                cell = None
                if container:
                    matches = self._objects.get((scene, container), ())
                    if len(matches) == 1 and matches[0].template.startswith(CELL_TEMPLATE):
                        parent, status = self._transform(matches[0])
                        coordinates = _display_coordinates(_compose(parent, local, IDENTITY)[0]) if parent and local else None
                        cell = {"object_id": str(container), "index": matches[0].cell_index, "source": matches[0].source}
                        if coordinates:
                            status = "spawn_area" if radius else "transformed_cell"
                    else:
                        coordinates, status = None, "unproved_poi_container"
                location = {
                    "scene": scene, "coordinates": coordinates, "status": status if local else "invalid_coordinates",
                    "source": source, "local_coordinates": _display_coordinates(local), "object_id": None,
                    "kind": "poi_spawner", "radius": radius, "cell": cell,
                    "coordinate_frame": "cell" if container else "world",
                    "map_id": self.map_id_for(scene, coordinates),
                    "spawner": {"type": "Point of interest spawner", "entries": [self._spawn_entry(mob_id, source)],
                                "count": _object_id(row.get("POP")), "count_label": "Population", "radius": radius,
                                "delay_min": _numeric(row.get("DELAY")), "script_source":
                                    self._spawn_script("poi.interior_spawner.interior_spawner")},
                }
                self._poi_sources[source["path"], source["line"]] = location
                self._add_mob(mob_id, location)

    def _index_space_npcs(self):
        for row in self._table(SERVER / "space_content/npc_spawners.tab"):
            mob, template, object_id = row.get("sidName"), row.get("strTemplate"), _object_id(row.get("strObjId"))
            if not mob or not template.startswith("object/") or not object_id:
                continue
            for scene in self.maps:
                for anchor in self._objects.get((scene, object_id), ()):
                    if anchor.template != "object/tangible/space/content_infrastructure/ground_npc_spawner.iff":
                        continue
                    if template != "unused" and anchor.runtime_id == object_id and anchor not in self._spawner_details:
                        self._spawner_details[anchor] = {
                            "type": "Ground NPC spawner",
                            "entries": [{**self._spawn_entry(mob, row.source), "npc": mob, "count": 1}],
                            "count": 1, "count_label": "Maximum active", "selection": "fixed",
                            "source": row.source, "configuration_status": "configured",
                            "script_source": self._spawn_script("space.content_tools.npc_spawner"),
                        }
                    location = self._placement(anchor)
                    location.update(kind="space_npc_spawner", spawn_type_source=row.source, npc_template=template)
                    self._add_mob(mob, location)

    def _index_places(self):
        transport_ids = set()
        for row in self._table(SERVER / "planet_map/transport_locations.tab"):
            scene, object_id = row.get("SCENE"), row.get("OBJECT_ID")
            point = _vector(row, ("X", "Y", "Z"))
            if not (scene and object_id and point):
                continue
            kind = "shuttleport" if row.get("IS_SHUTTLEPORT") == "1" else "starport"
            ident = f"{scene}/transport/{object_id}"
            self.places[ident] = {"id": ident, "scene": scene, "name": row.get("POINT_NAME"),
                                 "kind": kind, "coordinates": _display_coordinates(point),
                                 "status": "authored_world", "object_id": object_id, "source": row.source,
                                 "map_id": self.map_id_for(scene, point)}
            transport_ids.add((scene, _object_id(object_id)))
        for scene, areas in self.areas.items():
            counts = Counter(area.name for area in areas)
            for area in areas:
                name = f"{area.name}-{area.index}" if counts[area.name] > 1 else area.name
                ident = f"{scene}/area/{name}"
                self.places[ident] = {"id": ident, "scene": scene, "name": area.name.replace("_", " "),
                                     "kind": "buildout_area", "coordinates": _region_center(area.bounds),
                                     "bounds": list(area.bounds), "area_index": area.index,
                                     "required_event": area.event or None,
                                     "status": "authored_extent", "source": area.source,
                                     "map_id": self.map_id_for(scene, _center(area.bounds))}
        named_templates = {
            row.get("TEMPLATE"): row for row in self._table(SERVER / "planet_map/map_locations.tab")
            if row.get("PM_NAME")
        }
        # A template's named landmark can have several physical placements.
        for records in self._sources.values():
            for record in records:
                if record.template not in named_templates or (record.scene, record.object_id) in transport_ids:
                    continue
                entry = named_templates[record.template]
                location = self._placement(record)
                if not location["coordinates"]:
                    continue
                ident = f"{record.scene}/landmark/{location['runtime_object_id'] or record.area.name + '-' + str(record.line)}"
                name = entry.get("PM_NAME")
                self.places[ident] = {
                    **location, "id": ident, "name": name.split(":")[-1].replace("_", " ") if name.startswith("@") else name,
                    "name_key": name if name.startswith("@") else None,
                    "kind": entry.get("PM_CATEGORY") or "landmark", "name_source": entry.source,
                }

    def _client_asset(self, key):
        return _asset(self.clientdata, key)

    def _client_table(self, key):
        path = self._client_asset(key)
        if not path:
            return ()
        try:
            return read_iff_table(path)[2]
        except (ValueError, OSError, AssertionError, KeyError, struct.error):
            return ()

    def _create_maps(self):
        layout = self._client_asset(MAP_LAYOUT)
        style = {}
        if self.clientdata and not layout:
            self.report_issue("Maps", f"Missing client map layout {self.clientdata / MAP_LAYOUT}. Coordinate maps will be used.")
        if layout:
            for match in UI_MAP_ENTRY.finditer(layout.read_text(encoding="utf-8-sig", errors="replace")):
                page, name, image, resource = match.groups()
                # The client default falls back to Dathomir when no map exists.
                if name in {"default", "taanab"}:
                    continue
                asset = f"texture/{resource.lower()}.dds"
                if not self._client_asset(asset):
                    self.report_issue("Maps", f"Missing map texture {self.clientdata / asset} referenced by {layout.name}.")
                    continue
                style[name] = (asset, _attribute_numbers(page, "MapWidth", 2),
                               _attribute_numbers(image, "SourceRect", 4))
        adjustments = {row["Zone"]: row for row in self._client_table(MAP_ADJUSTMENTS) if row.get("Zone")}
        maps = {}
        for scene, areas in self.areas.items():
            zone_bounds = tuple(operation(area.bounds[index] for area in areas)
                                for index, operation in enumerate((min, min, max, max)))
            terrain_path = f"terrain/{scene}.trn"
            terrain_width = _terrain_width(self._client_asset(terrain_path))
            if self.clientdata and terrain_width is None:
                self.report_issue("Maps", f"Could not read terrain bounds from {self.clientdata / terrain_path}. Using buildout bounds for {scene}.")
            base_bounds = (-terrain_width / 2, -terrain_width / 2,
                           terrain_width / 2, terrain_width / 2) if terrain_width else zone_bounds
            base_source = {"path": terrain_path, "asset_root": "clientdata"} if terrain_width else areas[0].source
            maps[scene] = _map_record(scene, scene, base_bounds, base_source, style.get(scene), adjustments.get(scene))
            local_maps = {}
            for area in areas:
                region = area.composite or area.name
                name = f"{scene}__{region}"
                if name not in style:
                    continue
                bounds = area.composite_bounds or area.bounds
                identity = name, bounds
                if identity not in local_maps:
                    ident = name if name not in maps else f"{name}-{area.index}"
                    maps[ident] = _map_record(ident, scene, bounds, area.source, style[name], adjustments.get(name))
                    maps[ident]["region"] = region
                    local_maps[identity] = ident
            regions = list(local_maps.values())
            maps[scene]["regions"] = regions
        return maps

    def map_info(self, ident):
        return self.maps.get(ident)

    def map_id_for(self, scene, point):
        if scene not in self.maps:
            return None
        region_ids = self.maps[scene].get("regions", ())
        if point is not None:
            x, _, z = point
            for ident in region_ids:
                xmin, zmin, xmax, zmax = self.maps[ident]["bounds"]
                if xmin <= x <= xmax and zmin <= z <= zmax:
                    return ident
        return scene

    def map_image(self, ident):
        return self._map_images.map_image(ident)


def _attribute_numbers(attributes, name, count):
    match = re.search(rf"\b{name}='([\d., +\-]+)'", attributes, re.I)
    if match:
        values = tuple(_numeric(value.strip()) for value in match[1].split(","))
        if len(values) == count and None not in values:
            return values
    return None


def _center(bounds):
    return (bounds[0] + bounds[2]) / 2, 0, (bounds[1] + bounds[3]) / 2


def _region_center(bounds):
    x, _, z = _center(bounds)
    return [round(x, 5), None, round(z, 5)]


def _portal_cells(path):
    """Read named portal cells from the local POB using its original hierarchy."""
    data = path.read_bytes()
    root, children = form(data, *next(block_positions(data)))
    if root != b"PRTO":
        return None
    _, children = form(data, *children[0])
    _, start, end = next(child for child in children if child[0] == b"CRC ")
    if end - start != 4:
        return None
    crc = struct.unpack_from("<i", data, start)[0]
    cell_form = next(child for child in children if child[0] == b"FORM" and data[child[1]:child[1] + 4] == b"CELS")
    _, cells = form(data, *cell_form)
    rooms = {}
    for index, entry in enumerate(cells):
        kind, body = form(data, *entry)
        version, body = form(data, *body[0])
        if kind != b"CELL" or version not in (b"0004", b"0005") or body[0][0] != b"DATA":
            continue
        _, start, end = body[0]
        stop = data.index(0, start + 5, end)
        rooms[index] = data[start + 5:stop].decode("ascii")
    return rooms, crc


def _terrain_width(path):
    if path is None:
        return None
    try:
        with path.open("rb") as stream:
            blob = stream.read(4096)
        if not (blob.startswith(b"FORM") and blob[8:16] == b"PTATFORM"
                and blob[20:24] == b"0015" and blob[24:28] == b"DATA"):
            return None
        end = min(len(blob), 32 + struct.unpack_from(">I", blob, 28)[0])
        # PTAT 0015 DATA starts with a terminated source filename then map width.
        offset = blob.index(0, 32, end) + 1
        if offset + 4 > end:
            return None
        width = struct.unpack_from("<f", blob, offset)[0]
        return width if math.isfinite(width) and 0 < width < 1_000_000 else None
    except (OSError, ValueError, struct.error):
        return None


def _map_record(ident, scene, bounds, extent_source, style, adjustment):
    asset, override, source_rect = style or (None, None, None)
    if override and all(value > 0 for value in override):
        x, _, z = _center(bounds)
        w, h = override
        bounds = x - w / 2, z - h / 2, x + w / 2, z + h / 2
    image_rect = [0, 0, 1, 1]
    if adjustment:
        if adjustment["unused"]:
            image_rect = [adjustment["UnusedMapPixelsOnLeft"] / 512,
                          adjustment["UnusedMapPixelsOnTop"] / 512,
                          1 - adjustment["UnusedMapPixelsOnRight"] / 512,
                          1 - adjustment["UnusedMapPixelsOnBottom"] / 512]
        else:
            x, y = adjustment["offsetX"] / 1024, adjustment["offsetY"] / 1024
            image_rect = [x, y, 1 + x, 1 + y]
    return {
        "id": ident, "scene": scene, "region": None, "bounds": list(bounds),
        "image": f"/api/map/{ident}/image" if asset else None, "asset": asset,
        "source_rect": [int(value) for value in source_rect] if source_rect else None,
        "image_rect": image_rect, "extent_source": extent_source,
        "layout_source": {"path": MAP_LAYOUT, "asset_root": "clientdata"} if asset else None,
        "adjustment_source": {"path": MAP_ADJUSTMENTS, "asset_root": "clientdata"} if adjustment else None,
    }
