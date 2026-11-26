"""Build the source-backed reverse index from spawn origins to creatures."""

from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import PurePosixPath
import re

from catalog_locations import (
    IDENTITY, NPC_TEMPLATES, _compose, _display_coordinates, _numeric, _vars, _vector,
)
from catalog_spawn_scripts import Position, SpawnScripts
from catalog_spawn_tables import SpawnTables, authored_count, row_position, spawn_columns, table_name, table_ref, table_type
from catalog_sources import SERVER, SERVER_OBJECTS, source


SPAWNER_NAME = re.compile(r"(?:spawner|spawn_?eggs?|/lair/npc_lair)(?:[_.\d/]|$)", re.I)
SPACE_SCRIPT = "space.content_tools.spawner"
REGION_SCRIPT = "systems.spawning.spawn_base"
TABLE_FAMILIES = ("spawning/", "missions/", "npc_lair/", "dynamic_region_spawns/", "poi/", "uberlair/",
                  "space_zones/", "space_content/", "space_trainer_spawner/", "faction_perk/hq/spawn/", "gcw/static_base/")
PLACEHOLDERS = {"0", "-", "none", "null", "unused", "required", "$"}


def _template_ref(path):
    return "object/" + path.relative_to(SERVER_OBJECTS).with_suffix(".iff").as_posix()


def _recipe_type(script):
    module = script.split(".")[0]
    return {"conversation": "Conversation spawn", "event": "Event script spawn", "quest": "Quest script spawn",
            "city": "City script spawn", "poi": "Point of interest script", "creature_spawner": "Dedicated creature spawner",
            "space": "Space script spawn"}.get(module, "Scripted creature spawn")


class SpawnIndex:
    def __init__(self, files, spatial, npcs, places):
        self.files, self.spatial, self.npcs, self.places = files, spatial, npcs, places
        self.scenes = set(spatial.maps) | {row.get("planet") for row in spatial._table(SERVER / "region/planets.tab")}
        self.sites = defaultdict(list)
        self.table_sites = defaultdict(dict)
        self.used_lairs = set()
        self.used_rows = set()
        self.records = []
        self.stats = Counter()
        self.templates = {record.template for record in spatial.script_sites if SPAWNER_NAME.search(record.template)}
        self.templates.update(ref for path in files.paths(SERVER_OBJECTS, "*.tpf", recursive=True)
                              if (ref := _template_ref(path)).startswith("object/tangible/") and SPAWNER_NAME.search(ref))
        self.scripts = SpawnScripts(files, npcs, self.templates, self.scenes).scan()
        self.tables = SpawnTables(files, spatial, self._entry)

    def _entry(self, ref, proof):
        if not ref or ref.lower() in PLACEHOLDERS:
            return None
        npc = ref
        if ref.startswith(NPC_TEMPLATES):
            npc = PurePosixPath(ref).stem.removeprefix("shared_")
        if npc in self.npcs or ref.startswith(NPC_TEMPLATES):
            return {"ref": ref, "npc": npc, "source": proof}
        if ref in self.templates:
            return {"ref": ref, "item": ref, "source": proof}
        return None

    def _keep(self, point, config, scope, template=None, **extra):
        config = {**config}
        if config.get("entries"):
            entries = []
            seen = set()
            for entry in config["entries"]:
                key = (entry["ref"], entry.get("count"), entry.get("maximum"), entry.get("role"))
                if key not in seen:
                    seen.add(key)
                    npc = self.npcs.get(entry.get("npc"))
                    entries.append({**entry, **({"name": npc["name"]} if npc else {})})
            config["entries"] = entries
        self.records.append(({**point, **extra}, config, scope, template))
        self.stats[scope] += 1
        if not config.get("entries"):
            self.stats["unresolved_origins"] += 1

    def _point(self, record):
        point = self.spatial._placement(record)
        building = record
        while building and not building.template.startswith("object/building/"):
            building = building.parent
        if building:
            point["building_key"] = building.placement_key
        return point

    def _room(self, record, name, xyz):
        building = record
        while building and not building.template.startswith("object/building/"):
            building = building.parent
        if not building or not name:
            return None, None, None
        layout = self.spatial._portal_rooms(building.template)
        if not layout or building.portal_crc is not None and building.portal_crc != layout[2]:
            return building, None, None
        indexes = [index for index, label in layout[1].items() if label.casefold() == name.casefold()]
        if len(indexes) != 1 or not indexes[0]:
            return building, None, None
        index = indexes[0]
        cell = self.spatial._building_cells[building].get(index)
        world, _ = self.spatial._transform(cell or building)
        proof = {"index": index, "name": layout[1][index], "layout_asset": layout[0]}
        if cell:
            proof["source"] = cell.source
        # Rooms without a separate buildout row share the portal building's
        # model frame, as in the existing interior renderer.
        return building, proof, _display_coordinates(_compose(world, xyz, IDENTITY)[0]) if world and xyz else None

    def _position(self, proof, position, record=None):
        point = {"source": proof, "scene": position.scene or (record.scene if record else ""),
                 "coordinates": None, "local_coordinates": None, "status": "runtime_position"}
        if record:
            anchor = self._point(record)
            point.update(anchor_source=record.source, anchor_coordinates=anchor["coordinates"],
                         area_index=anchor["area_index"], required_event=anchor["required_event"])
            if anchor["scene"] == point["scene"] and position.frame == "anchor":
                point.update(coordinates=anchor["coordinates"], cell=anchor.get("cell"), building_key=anchor.get("building_key"),
                             status="spawner_anchor" if anchor["coordinates"] else anchor["status"])
        if position.frame == "world":
            point.update(coordinates=_display_coordinates(position.xyz), status="authored_world")
        elif position.frame == "cell":
            point.update(room=position.room, local_coordinates=_display_coordinates(position.xyz), status="unproved_portal_cell")
            if record and record.scene == point["scene"]:
                building, cell, coordinates = self._room(record, position.room, position.xyz)
                if building:
                    point.update(building_key=building.placement_key, building_source=building.source)
                if cell:
                    point.update(cell=cell, coordinates=coordinates,
                                 status="transformed_cell" if coordinates else "authored_room")
        return point

    def _register_sites(self):
        for record, (scripts, raw) in self.spatial.script_sites.items():
            _, inherited = self.spatial._template_spawn_settings(record.template)
            settings = {**inherited, **_vars(raw)}
            for script in sorted(scripts):
                self.sites[script].append(record)
            for value in settings.values():
                if ref := table_ref(value):
                    self.table_sites[ref][record] = record.source
            if record.template in self.templates or record in self.spatial._spawner_details:
                self._physical(record, scripts, settings)

    def _physical(self, record, scripts, settings):
        config = self.spatial._spawner_details.get(record)
        if SPACE_SCRIPT in scripts:
            config = self.tables.space_spawner(settings, record.source, self.spatial._spawn_script(SPACE_SCRIPT))
        lair = settings.get("spawning.lairType") or settings.get("strLairType")
        if lair and record.template.startswith("object/tangible/lair/"):
            self.used_lairs.add(lair)
            config = {"type": "Placed lair", "entries": list(self.tables.lair(lair)), "selection": "conditional",
                      "source": record.source, "script_source": self.spatial._spawn_script("systems.npc_lair.npc_lair")}
        if not config:
            script = next((name for name in sorted(scripts) if name in self.scripts.by_script or "spawn" in name), None)
            config = {"type": _recipe_type(script) if script else "Placed spawn object", "entries": [],
                      "source": record.source, "script_source": self.spatial._spawn_script(script) if script else None,
                      "configuration_status": "scripted"}
        if not config["entries"] and record.template in self.templates:
            entries = [entry for script in scripts for call in self.scripts.by_script.get(script, ())
                       if not call.development for entry in self._call_entries(call, settings, record.source)]
            if entries:
                config = {**config, "entries": entries, "selection": "conditional", "configuration_status": "configured"}
                self.specialized.add(record)
        config = {**config, "entries": [dict(entry, npc=entry.get("npc") or (self._entry(entry["ref"], entry.get("source")) or {}).get("npc"))
                                        for entry in config["entries"]]}
        self.spatial._spawner_details[record] = config
        point = self._point(record)
        if config.get("source"):
            proof = config["source"]
            self.used_rows.add((proof.get("path"), proof.get("line")))
        self._keep(point, config, "placed" if record.area else "unregistered", record.template)

    def _space(self):
        for path in self.files.paths(SERVER / "space_zones/buildout", "*.tab"):
            for row in self.spatial._table(path):
                template = row.get("strObject")
                scripts = {name.removeprefix("script.") for name in re.split(r"[:,\s]+", row.get("strScripts"))}
                if not SPAWNER_NAME.search(template) and SPACE_SCRIPT not in scripts:
                    continue
                settings = _vars(row.get("strObjVars"))
                config = self.tables.space_spawner(settings, row.source, self.spatial._spawn_script(SPACE_SCRIPT))
                origin = {"scene": path.stem, "source": row.source, "status": "spawner_anchor",
                          "coordinates": _display_coordinates(_vector(row, ("fltPX", "fltPY", "fltPZ")))}
                self._keep(origin, config, "placed", template)
                self.templates.add(template)

    def _known_tables(self):
        for point in self.spatial.table_spawn_points:
            proof = point["source"]
            self.used_rows.add((proof["path"], proof["line"]))
            entry = self._entry(point["spawn_ref"], proof)
            if entry:
                config = {"type": "Building spawn table", "entries": [{**entry, "count": 1}],
                          "count": 1, "count_label": "Per activation", "source": proof, "delay_min": point["delay"],
                          "trigger": point["trigger"], "script_source": self.spatial._spawn_script("theme_park.dungeon.generic_spawner")}
                self._keep(point, config, "placed")
        for point in self.spatial._poi_sources.values():
            proof = point["source"]
            key = (proof["path"], proof["line"])
            if key not in self.used_rows and point["spawner"].get("entries"):
                point = {**point, "coordinates": None, "status": "spawner_placement_unverified",
                         "reference_coordinates": point["coordinates"]}
                self._keep(point, point["spawner"], "definition")
                self.used_rows.add(key)

    def _regions(self):
        pool_root = "datatables/spawning/spawn_lists/"
        regions = defaultdict(list)
        for path in self.files.paths(SERVER / "region", "region_*.tab"):
            scene = path.stem.removeprefix("region_")
            if scene not in self.scenes:
                continue
            for row in self.spatial._table(path):
                name = row.get("regionName").split(":")[-1]
                geographic = row.get("geographicalType")
                spawnable = row.get("spawnableType").lower()
                if not name or not geographic or spawnable in {"spawnfalse", "mission"}:
                    continue
                ref = f"{scene}/{name}" if geographic == "fictional" else (
                    f"spawn_overloads/{name.rsplit('_', 1)[0]}" if geographic == "overload" else f"{scene}/{scene}_{geographic}")
                regions[pool_root + ref + ".iff"].append((scene, row))
        return regions

    def _pool(self, relative, reference, rows, regions):
        parts = relative.split("/")
        mission = relative.startswith("missions/")
        scene = next((part for part in parts[:-1] if part in self.scenes), "")
        matched = regions.get(reference, ())
        if not matched and table_name(reference) == "world_spawner" and scene:
            default = f"datatables/spawning/spawn_lists/{scene}/{scene}_default.iff"
            if not self.files.exists(SERVER / f"spawning/spawn_lists/{scene}/{scene}_default.tab"):
                matched = regions.get(default, ())
        if not matched and scene and not mission:
            for key, candidates in regions.items():
                for candidate, row in candidates:
                    if candidate == scene and row.get("regionName").split(":")[-1] == table_name(reference):
                        matched = [(candidate, row)]
                        break
                if matched:
                    break
        script = self.spatial._spawn_script("systems.missions.base.mission_dynamic_base" if mission else REGION_SCRIPT)
        for row in rows:
            entries = self.tables.pool(row)
            lair = row.get("strLairType")
            if lair:
                self.used_lairs.add(lair)
            if not entries and not lair:
                continue
            config = {"type": "Mission lair or herd" if mission else "Region lair or herd", "name": lair or table_name(reference),
                      "entries": entries, "selection": "random", "count": authored_count(row, "intNumToSpawn"),
                      "count_label": "Herd population", "source": row.source, "script_source": script,
                      "weight": _numeric(row.get("intWeighting")), "maximum": authored_count(row, "intSpawnLimit")}
            for world, region in matched or [(scene, None)]:
                location_id = f"{world}/region/{region.line}" if region else None
                point = {"scene": world, "source": row.source, "region_source": region.source if region else None,
                         "location_id": location_id, "location": self.places.get(location_id, {}).get("name"),
                         "status": "region_selection" if region else "runtime_position"}
                self._keep(point, config, "region" if region else "runtime" if mission else "definition")

    def _table_bindings(self):
        for path in self.files.paths(SERVER / "spawning/script_spawner", "*_spawners.tab", recursive=True):
            if path.parent.name not in self.scenes:
                continue
            for row in self.spatial._table(path):
                if ref := table_ref(row.get("spawnTable")):
                    self.table_sites[ref][None, path.parent.name, row.line] = row.source

    def _infer_tables(self):
        regions = self._regions()
        self._table_bindings()
        table_names = set(self.table_sites) | set(self.scripts.by_table)
        paths = set(self.files.paths(SERVER, "*.tab", recursive=True))
        for path in sorted(paths):
            relative = path.relative_to(SERVER).as_posix()
            reference = "datatables/" + relative.removesuffix(".tab") + ".iff"
            if not relative.startswith(TABLE_FAMILIES) and reference not in table_names:
                continue
            if relative.startswith("buildout/"):
                continue
            rows = self.spatial._table(path)
            self.stats["candidate_tables"] += 1
            if not rows:
                continue
            header = rows[0].cells
            if "strLairType" in header and relative.startswith(("spawning/spawn_lists/", "missions/")):
                self._pool(relative, reference, rows, regions)
                continue
            if relative.startswith("npc_lair/") or relative == "space_content/spawners/squads.tab":
                self.stats["population_tables"] += 1
                continue
            if relative.startswith("spawning/ground_spawning/types/"):
                columns = ("strItem",)
            elif relative.startswith("spawning/ground_spawning/static/"):
                columns = ("strMob",)
            elif relative.startswith("spawning/poi_spawner/") and "TYPE" in header:
                columns = ("TYPE",)
            else:
                columns = spawn_columns(relative, header)
            if columns:
                self._row_sources(relative, reference, rows, columns)

    def _row_sources(self, relative, reference, rows, columns):
        script_refs = self.scripts.by_table.get(reference, {})
        bindings = dict(self.table_sites.get(reference, {}))
        label = table_type(relative)
        parts = relative.split("/")
        if relative.startswith("spawning/ground_spawning/types/"):
            pool = relative.removeprefix("spawning/ground_spawning/types/").removesuffix(".tab")
            if pool in self.used_pools:
                return
        scene = next((part for part in parts[:-1] if part in self.scenes), "")
        if relative.startswith("gcw/static_base/base_spawn_"):
            scene = table_name(reference).removeprefix("base_spawn_")
        if relative.startswith("spawning/restuss_event/"):
            scene = "rori"
        if not bindings and label not in {"Ground spawn pool", "Mission spawn pool", "Legacy quest NPC", "Seasonal event table"}:
            for ident, (proof, development) in script_refs.items():
                if not development:
                    for record in self.sites.get(ident, ()):
                        bindings[record] = proof
        default_script = next((proof for proof, development in script_refs.values() if not development), None)
        if not default_script and script_refs:
            default_script = next(iter(script_refs.values()))[0]
        for row in rows:
            proof = row.source
            if (proof["path"], proof["line"]) in self.used_rows:
                continue
            entries = []
            maximum = authored_count(row, "max_number", "max_population")
            for column in columns:
                value = row.get(column)
                if not value:
                    continue
                for ref in re.split(r"[,;:]" if not value.startswith("formation:") else r";", value):
                    if entry := self._entry(ref.strip(), proof):
                        entries.append({**entry, "role": column if len(columns) > 1 else None, "maximum": maximum,
                                        "count": 1 if "room" in row.cells else None})
            if not entries:
                continue
            trigger = row.get("triggerId") or row.get("required_config") or row.get("phase") or row.get("stage")
            delay = next((delay for field in ("respawn_time", "respawn", "fltMinSpawnTime")
                          if (delay := _numeric(row.get(field))) is not None and delay >= 0), None)
            config = {"type": label, "name": row.get("name") or table_name(reference), "entries": entries,
                      "source": proof, "selection": "conditional", "delay_min": delay,
                      "delay_max": _numeric(row.get("fltMaxSpawnTime")), "trigger": trigger,
                      "count": 1 if "room" in row.cells and len(entries) == 1 else None, "count_label": "Per activation"}
            room = row.get("room") or row.get("cell") or row.get("cellName")
            xyz = row_position(row)
            absolute = room == "world" or row.get("location_type") == "literal"
            row_scene = row.get("planet") or row.get("PLANET") or scene
            for host, binding in bindings.items() if bindings else ((None, default_script),):
                if isinstance(host, tuple):
                    record, current = None, host[1]
                else:
                    record, current = host, row_scene
                if absolute:
                    pos = Position(current or (record.scene if record else None), xyz, frame="world" if xyz else "runtime")
                elif room and room.lower() != "none":
                    pos = Position(current, xyz, room, "cell")
                else:
                    pos = Position(current, xyz)
                point = self._position(proof, pos, record)
                if isinstance(host, tuple):
                    point["anchor_source"] = binding
                scope = "scripted" if (record and record.area) or point["coordinates"] and point["scene"] else (
                    "unregistered" if record else "definition")
                self._keep(point, {**config, "script_source": binding or default_script}, scope)
                if record and point.get("building_key") and pos.frame == "cell" and xyz:
                    self._building_contents(point, entries)

    def _building_contents(self, point, entries):
        if point.get("room"):
            for entry in entries:
                self.spatial.building_spawn_points.append({**point, "spawn_ref": entry["ref"], "mob_id": entry.get("npc")})

    def _script_calls(self):
        for ident, calls in self.scripts.by_script.items():
            for call in calls:
                # Existing specialized interpreters carry more accurate selector,
                # count, timing and condition information for these placements.
                hosts = () if call.development else tuple(self.sites.get(ident, ()))
                if hosts and all(record in self.specialized for record in hosts):
                    continue
                config = {"type": _recipe_type(ident), "name": call.method,
                          "source": call.table or call.path, "script_source": call.path, "caller_source": call.caller,
                          "selection": "conditional", "trigger": call.method}
                for host in hosts or (None,):
                    if host in self.specialized:
                        continue
                    settings = None
                    if host and call.target.startswith("@objvar/"):
                        _, inherited = self.spatial._template_spawn_settings(host.template)
                        settings = {**inherited, **_vars(self.spatial.script_sites[host][1])}
                    options = self._call_entries(call, settings, host.source if host else None, expand=True)
                    if not options:
                        continue
                    point = self._position(call.path, call.position, host)
                    scope = "development" if call.development else "scripted" if (
                        point["coordinates"] and point["scene"] or host and host.area) else "unregistered" if host else "runtime"
                    template = host.template if host and host.template in self.templates else None
                    self._keep(point, {**config, "entries": options}, scope, template)
                    if host and call.position.xyz and point.get("building_key") and call.position.frame == "cell":
                        self._building_contents(point, options)

    def _call_entries(self, call, settings=None, setting_source=None, *, expand=False):
        proof = call.table or call.path
        target = call.target
        if target.startswith("@objvar/"):
            target = (settings or {}).get(target.removeprefix("@objvar/"))
            if not target:
                return []
            proof = setting_source or proof
        if call.mode == "lair":
            lair = table_name(target)
            self.used_lairs.add(lair)
            return list(self.tables.lair(lair))
        if call.mode == "squad":
            return self.tables.ships(target, proof)
        ref = "space/" + target if call.mode == "ship" else target
        entry = self._entry(ref, proof)
        if expand and entry and (template := entry.get("item")):
            return [entry, *({**spawn, "selector_source": proof, "role": f"Spawned through {table_name(template)}"}
                            for spawn in self._template_creatures(template))]
        return [{**entry, "count": 1}] if entry else []

    @lru_cache(maxsize=1024)
    def _template_creatures(self, template):
        scripts, settings = self.spatial._template_spawn_settings(template)
        path = (SERVER.parent / template).with_suffix(".tpf")
        proof = source(path) if self.files.exists(path) else None
        return tuple(entry for ident in sorted(scripts) for call in self.scripts.by_script.get(ident, ())
                     if not call.development for entry in self._call_entries(call, settings, proof) if entry.get("npc"))

    def _definitions(self):
        placed = {record.template for record in self.spatial.script_sites if record.area}
        for template in sorted(self.templates - placed):
            scripts, _ = self.spatial._template_spawn_settings(template)
            path = (SERVER.parent / template).with_suffix(".tpf")
            if not self.files.exists(path):
                continue
            proof = source(path)
            calls = [call for ident in sorted(scripts) for call in self.scripts.by_script.get(ident, ())]
            entries = list(self._template_creatures(template))
            self._keep({"source": proof}, {"type": "Spawner template", "entries": entries, "source": proof,
                                          "configuration_status": "configured" if entries else "scripted",
                                          "script_source": calls[0].path if calls else None}, "definition", template)
        for path in self.files.paths(SERVER / "npc_lair", "*.tab", recursive=True):
            name = path.relative_to(SERVER / "npc_lair").with_suffix("").as_posix()
            if name not in self.used_lairs and (entries := self.tables.lair(name)):
                proof = source(path)
                self._keep({"source": proof}, {"type": "Lair population", "name": path.stem, "entries": list(entries),
                                              "source": proof, "selection": "random"}, "definition")

    def build(self):
        self.specialized = {record for record, config in self.spatial._spawner_details.items() if config.get("entries")}
        self.used_pools = {config["table"] for config in self.spatial._spawner_details.values() if config.get("table")}
        self._register_sites()
        self._space()
        self._known_tables()
        self._infer_tables()
        self._script_calls()
        self._definitions()
        return self
