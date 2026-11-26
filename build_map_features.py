"""Index source-backed quest, location, NPC and physical world placements."""

from collections import Counter, defaultdict
import json
import re
from pathlib import PurePosixPath

from catalog_locations import CELL_TEMPLATE, IDENTITY, _asset, _compose, _display_coordinates, _map_record, _terrain_width
from catalog_map import chart_counts, map_cities
from catalog_sources import SHARED, display_id, number
from client_map_objects import client_map_objects


SCENE = re.compile(r"[a-z][a-z0-9_-]*")
SCENE_ALIASES = {"hoth": "adventure2", "kashyyyk": "kashyyyk_main", "ord_mantell": "space_ord_mantell"}
SETTLEMENT_LABELS = {
    "@kashyyyk_region_names:kachirho",
    "@clientpoi_n:dead_kerritamba",
    "@clientpoi_n:new_mining_facility",
}
OUTPOST = re.compile(r"\b(?:outpost|stronghold|settlement)$", re.I)


def _scene(value):
    value = (value or "").strip().lower()
    return value if value != "none" and SCENE.fullmatch(value) else None


def _position(values):
    if not values or len(values) != 3:
        return None
    point = [number(value) for value in values]
    if None not in point:
        return point
    return None


def _vector(fields, keys):
    return _position([fields.get(key) for key in keys])


def _cell_context(point):
    cell = point.get("cell")
    if isinstance(cell, str):
        return cell
    if cell:
        return cell.get("name") or ("Cell " + str(cell["index"]) if cell.get("index") else None)
    return "Cell " + str(point["cell_index"]) if point.get("cell_index") else None


def _template_layer(template, category=None):
    if not template:
        return "scenery"
    template = template.removeprefix("object/")
    group = template.split("/", 1)[0]
    if group == "cell":
        return "interior"
    if group in ("mobile", "creature"):
        return "npc"
    if group == "ship" or template.startswith("tangible/space/"):
        return "resource" if any(x in template for x in ("mining", "asteroid", "resource")) else "space"
    if any(x in template for x in ("/spawning/", "/spawner/", "_spawner.", "/lair/")):
        return "spawn"
    if any(x in template for x in ("starport", "shuttleport", "/travel/", "/hyperspace/")):
        return "transit"
    if group in ("building", "installation", "factory") or "/poi/" in template:
        return "structure"
    if any(x in template for x in ("/terminal/", "/vendor/", "/crafting/station", "/bank/", "/elevator/")):
        return "service"
    if any(x in template for x in ("/harvester/", "/survey/", "/mining/", "/asteroid/", "/resource/")):
        return "resource"
    if (category and category not in ("Item template", "Decoration", "Scenery item")) or group in ("weapon", "resource_container") or \
            template.startswith(("tangible/item/", "tangible/loot/", "static/item/")):
        return "item"
    return "scenery"


def _details(point):
    return {
        "source": point.get("source"), "status": point.get("status"),
        "object_id": point.get("runtime_object_id") or point.get("object_id"),
        "template": point.get("template") or point.get("template_crc"), "area": point.get("area"),
        "required_event": point.get("required_event"), "radius": point.get("radius"),
        "cell": _cell_context(point),
        "parent_source": point.get("parent_source"),
        "spawner": point.get("spawner"),
    }


def _place_layer(place):
    return {"Region": "region", "Buildout area": "region", "Transport": "transit"}.get(place["category"], "landmark")


def _city_centers(places):
    """Use visible municipal centers and named travel hubs from game tables."""
    centers = {}
    for ident, place in places.items():
        attributes = place.get("attributes", {})
        municipal = attributes.get("municipal") in (1, "true")
        visible = attributes.get("visible") in (1, "true")
        urban = attributes.get("geographicalType") in ("city", 20)
        if (municipal and visible and urban) or (place.get("name_label") or {}).get("code") in SETTLEMENT_LABELS:
            centers[place["scene"], place["name"].casefold()] = ident
    for ident, place in places.items():
        if place["category"] == "Transport" and OUTPOST.search(place["name"]):
            centers.setdefault((place["scene"], place["name"].casefold()), ident)
    return set(centers.values())


def _logical_objects(markers, places, npcs):
    represented = set()
    cities = _city_centers(places)
    for ident, place in sorted(places.items()):
        if position := (place.get("marker") or {}).get("coordinates"):
            scene, layer = place["scene"], _place_layer(place)
            if ident in cities:
                markers.add(scene, "city", place["name"], position, target=["locations", ident],
                            status=place["marker"].get("status"), source=next(iter(place["sources"]), None),
                            context="City or settlement")
                if layer == "region":
                    continue
            markers.add(scene, layer, place["name"], position, target=["locations", ident],
                        status=place["marker"].get("status"), source=next(iter(place["sources"]), None),
                        object_id=place.get("object_id"), template=place.get("template"),
                        context=place["category"],
                        summary=((place.get("client_description") or {}).get("text") or "")[:240] or None)
            if place.get("object_id"):
                represented.add((scene, place["object_id"]))
    for ident, npc in sorted(npcs.items()):
        layer = "spawn" if npc["category"] == "Creature" else "npc"
        for point in npc["locations"]:
            scene = point.get("scene")
            if scene and (location := point.get("coordinates")):
                markers.add(scene, layer, npc["name"], location, target=["npcs", ident],
                            context=npc["category"], level=npc.get("level"), **_details(point))
                if object_id := point.get("runtime_object_id") or point.get("object_id"):
                    represented.add((scene, object_id))
    return represented


def _object_markers(markers, spatial, items, represented, coverage, buildings):
    authored = set()
    associated = {}
    pending = []
    for point in spatial.buildout_objects():
        buildings.record(point, "server_buildout")
        if not point["coordinates"]:
            if point.get("required_parent") and point.get("cell_index") and point.get("local_coordinates"):
                pending.append(point)
            else:
                coverage[point["status"]] += 1
            continue
        identity = (point["scene"], point.get("runtime_object_id") or point.get("object_id"))
        if identity[1]:
            authored.add(identity)
        if identity in represented and not point.get("spawner"):
            coverage["covered_by_linked_marker"] += 1
            continue
        _plot_object(markers, point, items, associated, "server_buildout")
        coverage["server_buildout"] += 1

    expected_parents = {(point["scene"], point["required_parent"]) for point in pending}
    unresolved = {(point["scene"], point.get("runtime_object_id")) for point in pending}
    parents = defaultdict(list)
    deferred = []
    for point in client_map_objects(spatial.clientdata, coverage):
        buildings.record(point, point["origin"])
        identity = point["scene"], point["object_id"]
        if identity in expected_parents and (point.get("template") or "").startswith(CELL_TEMPLATE):
            parents[identity].append(point)
        if identity in authored or identity in represented:
            coverage["covered_by_linked_marker"] += 1
            continue
        if identity in unresolved:
            deferred.append(point)
            continue
        _plot_object(markers, point, items, associated, point["origin"])
        coverage[point["origin"]] += 1

    for point in pending:
        candidates = parents[point["scene"], point["required_parent"]]
        if len(candidates) != 1 or candidates[0]["cell_index"] != point["cell_index"]:
            coverage[point["status"]] += 1
            continue
        parent = candidates[0]
        world = _compose(parent["world_transform"], point["local_coordinates"], IDENTITY)
        point.update(coordinates=_display_coordinates(world[0]), status="transformed_cell",
                      parent_source=parent["source"], world_transform=world)
        buildings.record(point, "server_buildout_client_parent")
        identity = point["scene"], point.get("runtime_object_id")
        authored.add(identity)
        if identity in represented and not point.get("spawner"):
            coverage["covered_by_linked_marker"] += 1
            continue
        _plot_object(markers, point, items, associated, "server_buildout_client_parent")
        coverage["server_buildout_client_parent"] += 1
    for point in deferred:
        if (point["scene"], point["object_id"]) in authored:
            coverage["covered_by_linked_marker"] += 1
            continue
        _plot_object(markers, point, items, associated, point["origin"])
        coverage[point["origin"]] += 1


def _plot_object(markers, point, items, associated, origin):
    template = point.get("template")
    if template not in associated:
        item_id = items.resolve_item(template) if template else None
        item = items.records.get(item_id)
        layer = _template_layer(template, item["category"] if item else None)
        basename = PurePosixPath(template).stem.removeprefix("shared_") if template else None
        name = item["name"] if item else (display_id(basename) if basename else None)
        associated[template] = layer, name, ["items", item_id] if item else None
    layer, name, target = associated[template]
    if spawner := point.get("spawner"):
        layer = "spawn"
        name = display_id(spawner["name"]) if spawner.get("name") else name
    if layer == "interior" and (cell := _cell_context(point)):
        name = cell
    markers.add(point["scene"], layer, name or ("Object " + (point.get("template_crc") or "")),
                point["coordinates"], target=target, origin=origin, **_details(point))


def _quest_locations(db, markers):
    for ident, title, data in db.execute("SELECT id,name,data FROM entity WHERE kind='quests' ORDER BY id"):
        quest = json.loads(data)
        seen = set()

        def plot(scene, label, location, layer, proof, **details):
            if not scene or not location:
                return
            key = scene, tuple(location), layer
            if key in seen:
                return
            seen.add(key)
            markers.add(scene, layer, title, location, target=["quests", ident],
                        source=proof, context=label, **details)

        for entry in quest["starts"]:
            point = entry.get("placement") or {}
            coords = point.get("coordinates")
            if not coords and entry.get("coordinate_frame") == "world":
                coords = _position(entry.get("position"))
            giver = (entry.get("npc") or {}).get("text")
            plot(_scene(point.get("scene") or entry.get("scene")), giver or "Quest start", coords,
                 "quest_start", point.get("source") or entry.get("source"),
                 status=point.get("status"), cell=_cell_context(point), radius=point.get("radius"))
        for task in quest["detail"]["steps"]:
            fields = task["attributes"]
            scene = _scene(task.get("scene"))
            label = task["title"].get("text") or "Objective " + str(task["index"] + 1)
            if fields.get("WAYPOINT_BUILDING_CELL_NAME", "").casefold() in ("none", "null", ""):
                if position := _vector(fields, ("LOCATION_X", "LOCATION_Y", "LOCATION_Z")):
                    if any(position):
                        plot(scene, label, position, "quest_objective", task["source"])
                for column in fields:
                    if column.startswith("LOCATION_X_POINT_"):
                        index = column.removeprefix("LOCATION_X_POINT_")
                        position = _vector(fields, tuple(f"LOCATION_{axis}_POINT_{index}" for axis in "XYZ"))
                        if position and any(position):
                            plot(scene, label + " · point " + index, position, "quest_objective", task["source"])
            if fields.get("CREATE_ENTRANCE_WAYPOINT", "").casefold() in ("1", "true"):
                if entrance := _vector(fields, ("ENTRANCE_LOCATION_X", "ENTRANCE_LOCATION_Y", "ENTRANCE_LOCATION_Z")):
                    if any(entrance):
                        plot(scene, label + " · entrance", entrance, "quest_objective", task["source"])
        legacy = quest["detail"]["authored_mission"]
        fields = legacy.get("fields", {})
        if location := _vector(fields, ("quest_x_loc", "quest_y_loc", "quest_z_loc")):
            if any(location):
                plot(_scene(fields.get("quest_planet_loc")), "Mission location", location,
                     "quest_objective", legacy["source"])


def _space_locations(files, strings, markers):
    table = SHARED / "space/hyperspace/hyperspace_locations.tab"
    if files.exists(table):
        for row in files.tab(table):
            ident = row.get("HYPERSPACE_POINT_NAME")
            if ident:
                name = strings.name("hyperspace_points_n", ident) or display_id(ident)
                markers.add(_scene(row.get("SCENE")), "transit", name,
                            _vector(row.cells, ("X", "Y", "Z")), context="Hyperspace arrival", source=row.source)
    for path in files.paths(SHARED / "space/asteroidfield", "space_*.tab"):
        for row in files.tab(path):
            field = row.get("Name") or ("Asteroid field " + str(row.line - 2))
            if row.get("Type") in ("cube", "spline"):
                coords = _vector(row.cells, ("CenterLocationX", "CenterLocationY", "CenterLocationZ"))
                track = row.get("SplineControlPoints").strip('"').split(":")
                route = [point for component in track if (point := _position(component.split(",")))]
                markers.add(_scene(path.stem), "resource", field, coords, radius=number(row.get("Radius")),
                            status="field_center", context="Asteroid field", source=row.source, route=route or None)


def _spawn_locations(markers, spawns):
    seen = set()
    for point, config, scope, template in spawns.records:
        proof = point["source"]
        if (scope != "scripted" and "/space_zones/buildout/" not in proof["path"]) or not point.get("coordinates"):
            continue
        scene, coordinates = point["scene"], point["coordinates"]
        targets = [entry for entry in config["entries"] if entry.get("npc")]
        key = (scene, tuple(coordinates), proof["path"], proof.get("line"), tuple(entry["npc"] for entry in targets))
        if key in seen or not targets:
            continue
        seen.add(key)
        target = ["npcs", targets[0]["npc"]] if len(targets) == 1 else ["items", template] if template else None
        label = config.get("name") or config["type"]
        name = targets[0].get("name") or display_id(targets[0]["ref"]) if len(targets) == 1 else display_id(label)
        markers.add(scene, "spawn", name, coordinates, target=target, context=config["type"], spawner=config,
                    source=proof, status=point["status"], cell=_cell_context(point), radius=config.get("radius"),
                    template=template, required_event=point.get("required_event"))


def _map_extent(extent):
    xmin, zmin, xmax, zmax = extent
    margin = max(125, (xmax - xmin) / 16, (zmax - zmin) / 16)
    return [round(xmin - margin, 2), round(zmin - margin, 2),
            round(xmax + margin, 2), round(zmax + margin, 2)]


def _finish_charts(markers, spatial, places, world):
    markers.flush()
    for scene in sorted(set(markers.extents) | {ident for ident, place in places.items() if ident == place["scene"]}):
        if scene in SCENE_ALIASES and scene not in markers.extents:
            continue
        entry = world(scene)
        extent = markers.extents.get(scene)
        if scene not in spatial.maps:
            terrain = f"terrain/{scene}.trn"
            width = _terrain_width(_asset(spatial.clientdata, terrain))
            bounds = entry.get("bounds")
            if width:
                bounds = [-width / 2, -width / 2, width / 2, width / 2]
            elif not bounds and extent:
                bounds = _map_extent(extent)
            if not bounds or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
                continue
            proof = {"path": terrain, "asset_root": "clientdata"} if width else next(iter(entry["sources"]), None)
            spatial.maps[scene] = _map_record(scene, scene, bounds, proof, None, None)
        chart = spatial.maps[scene]
        if extent and any((extent[0] < chart["bounds"][0], extent[1] < chart["bounds"][1],
                           extent[2] > chart["bounds"][2], extent[3] > chart["bounds"][3])):
            padded = _map_extent(extent)
            chart["grid_bounds"] = [min(chart["bounds"][0], padded[0]), min(chart["bounds"][1], padded[1]),
                                    max(chart["bounds"][2], padded[2]), max(chart["bounds"][3], padded[3])]

    for ident, chart in spatial.maps.items():
        chart["layers"] = chart_counts(markers.db, chart["scene"], chart["bounds"])
        if grid := chart.get("grid_bounds"):
            chart["grid_layers"] = chart_counts(markers.db, chart["scene"], grid)
        if (chart.get("grid_layers") or chart["layers"]).get("city"):
            chart["cities"] = map_cities(markers.db, chart["scene"], chart.get("grid_bounds", chart["bounds"]))
    for ident, place in places.items():
        scene = place["scene"]
        if not place.get("map"):
            chart_scene = SCENE_ALIASES.get(scene, scene)
            place["map"] = spatial.maps.get(chart_scene)
            if chart_scene != scene and place["map"]:
                place["map_scene"] = chart_scene


def build_map_features(writer, files, strings, spatial, places, world, npcs, items, spawns):
    from catalog_buildings import BuildingIndex

    markers = writer.markers
    coverage = Counter()
    represented = _logical_objects(markers, places, npcs)
    buildings = BuildingIndex(spatial, items, npcs)
    _object_markers(markers, spatial, items, represented, coverage, buildings)
    _quest_locations(markers.db, markers)
    _space_locations(files, strings, markers)
    _spawn_locations(markers, spawns)
    _finish_charts(markers, spatial, places, world)
    building_data = buildings.publish(writer)
    return {"marker_counts": dict(markers.counts), "placement_sources": dict(coverage),
            "buildings": building_data}
