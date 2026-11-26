"""Location containment from scene membership and authored map boundaries."""

from collections import Counter, defaultdict
import json
from math import dist
import re

from catalog_map import area_record


LOCATION_ALIASES = {"adventure2": "hoth"}
PHYSICAL_LOCATIONS = {"Building", "Landmark", "Transport"}
SHARED_SCENE_LOCATIONS = {"dungeon1/area/nova_orion_station": "space_nova_orion"}


def canonical_location(ident):
    return LOCATION_ALIASES.get(ident, ident)


def _is_city(place):
    attributes = place.get("attributes", {})
    return place["category"] == "Region" and (
        attributes.get("geographicalType") == "city" or attributes.get("municipal") == 1
    )


def _named_buildout(place):
    name = place["name"].casefold()
    return (place["category"] == "Buildout area" and not name.startswith("snapshot ")
            and not re.fullmatch(re.escape(place["scene"]) + r" \d+ \d+(?: .*)?", name))


def _footprint(place):
    bounds = place.get("bounds")
    if bounds and len(bounds) == 4:
        return bounds
    coordinates = (place.get("marker") or {}).get("coordinates")
    if coordinates:
        x, _, z = coordinates
        return x, z, x, z
    return None


def _contains(extent, child):
    west, south, east, north = child
    left, bottom, right, top = extent["bounds"]
    if not (left <= west <= east <= right and bottom <= south <= north <= top):
        return False
    if extent["shape"] == "circle":
        x, z, radius = extent["values"]
        return max(abs(west - x), abs(east - x)) ** 2 + max(abs(south - z), abs(north - z)) ** 2 <= radius ** 2
    return True


def _child_rank(place):
    if place["category"] == "World":
        return 0
    if _is_city(place):
        return 1
    if _named_buildout(place) or place["category"] in ("Landmark", "Transport"):
        return 2
    if place["category"] == "Space zone":
        return 3
    if place["category"] == "Building":
        return 4
    return 5


def _transport_aliases(places, entries):
    for ident, place in places.items():
        if place["category"] != "Transport" or not place.get("object_id"):
            continue
        building_id = f"{place['scene']}/building/{place['object_id']}"
        building = entries.get(building_id)
        if not building or place["name"].casefold() != building["name"].casefold():
            continue
        point, origin = place["marker"]["coordinates"], building["marker"]["coordinates"]
        if point and origin and dist(point, origin) <= 0.01:
            yield ident, building_id


def publish_location_hierarchy(writer, places):
    db = writer.connection
    entries = dict(places)
    for ident, name, scene, bounds, coordinates in db.execute("""
        SELECT id, name, scene, json_extract(data, '$.bounds'), json_extract(data, '$.coordinates')
        FROM entity WHERE kind='locations' AND category='Building'
    """):
        entries[ident] = {
            "name": name, "scene": scene, "category": "Building",
            "bounds": json.loads(bounds) if bounds else None,
            "marker": {"coordinates": json.loads(coordinates) if coordinates else None},
        }

    areas = defaultdict(list)
    for ident, place in places.items():
        city = _is_city(place)
        if not (city or _named_buildout(place)):
            continue
        record = area_record(ident, place["name"], place["category"], place)
        if record:
            extent = record["extent"]
            left, bottom, right, top = extent["bounds"]
            if right > left and top > bottom:
                areas[place["scene"]].append((not city, (right - left) * (top - bottom), ident, extent))
    for values in areas.values():
        values.sort(key=lambda area: area[:3])

    travel_aliases = dict(_transport_aliases(places, entries))
    parents = {}
    for ident, place in entries.items():
        if ident in LOCATION_ALIASES or ident in travel_aliases:
            continue
        scene = place["scene"]
        parent = None
        if ident == scene:
            parent = place.get("lore_parent")
            if not parent and scene.startswith("space_"):
                parent = re.sub(r"_\d+$", "", scene.removeprefix("space_"))
        else:
            parent = SHARED_SCENE_LOCATIONS.get(ident) or canonical_location(scene)
            if place["category"] in PHYSICAL_LOCATIONS and (child := _footprint(place)):
                parent = next((area_id for _, _, area_id, extent in areas[scene]
                               if _contains(extent, child)), parent)
        if parent and parent in entries and parent != ident:
            parents[ident] = parent

    shared_names = Counter((parent, entries[ident]["name"].casefold()) for ident, parent in parents.items())
    scene_membership = []
    for ident, parent in sorted(parents.items()):
        place = entries[ident]
        details = {"rank": _child_rank(place), "shared_name": shared_names[parent, place["name"].casefold()] > 1}
        writer.link("locations", parent, "places", "locations", ident, details)
        writer.link("locations", ident, "parent", "locations", parent)
        visited = {ident}
        while parent:
            if parent in visited:
                raise ValueError(f"Location containment cycle involving {ident} and {parent}")
            visited.add(parent)
            if entries[parent]["scene"] == parent:
                scene_membership.append(("locations", ident, parent))
            parent = parents.get(parent)
    db.executemany("INSERT OR IGNORE INTO scene_filter VALUES (?,?,?)", scene_membership)

    for travel, building in sorted(travel_aliases.items()):
        db.execute("UPDATE entity SET card=json_set(card,'$.alias_for',?) WHERE kind='locations' AND id=?",
                   (building, travel))
        writer.link("locations", building, "travel", "locations", travel)
        writer.link("locations", travel, "interior", "locations", building)
        db.execute("""
            INSERT OR IGNORE INTO scene_filter
            SELECT kind, ?, scene FROM scene_filter WHERE kind='locations' AND id=?
        """, (travel, building))

    for alias, canonical in LOCATION_ALIASES.items():
        if canonical not in entries:
            continue
        db.execute("""
            INSERT OR IGNORE INTO link
            SELECT kind, ?, role, target_kind, target_id, detail
            FROM link WHERE kind='locations' AND id=? AND role NOT IN ('parent','places')
        """, (canonical, alias))
