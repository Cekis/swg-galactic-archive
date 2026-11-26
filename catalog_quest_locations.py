"""Link authored quest starts to their scene, exact site, and containing locations."""

from collections import defaultdict
from functools import lru_cache
import json

from catalog_location_hierarchy import canonical_location
from catalog_map import area_record, extent_intersects


def source_key(proof):
    return (proof["path"], proof["line"]) if proof and proof.get("path") and proof.get("line") else None


def publish_location_quests(db):
    known = set()
    areas = defaultdict(list)
    objects = defaultdict(set)
    building_sources = defaultdict(set)
    building_cells = defaultdict(set)
    for ident, name, scene, category, raw in db.execute("""
        SELECT id, name, scene, category, data FROM entity
        WHERE kind='locations'
    """):
        known.add(ident)
        if category not in {"Region", "Buildout area", "Building", "Landmark", "Transport"}:
            continue
        place = json.loads(raw)
        if category in {"Region", "Buildout area"}:
            if record := area_record(ident, name, category, place):
                areas[scene].append((ident, record["extent"]))
            continue
        if uid := place.get("object_id"):
            objects[scene, str(uid)].add(ident)
        if category == "Building":
            if proof := source_key(place.get("source")):
                building_sources[scene, proof].add(ident)
            for cell in place.get("cells", ()):
                if proof := source_key(cell.get("source")):
                    building_sources[scene, proof].add(ident)
                if uid := cell.get("object_id"):
                    building_cells[scene, str(uid)].add(ident)

    parents = dict(db.execute("""
        SELECT id, target_id FROM link WHERE kind='locations' AND role='parent' AND target_kind='locations'
    """))
    aliases = dict(db.execute("""
        SELECT id, target_id FROM link WHERE kind='locations' AND role='interior' AND target_kind='locations'
    """))

    @lru_cache(maxsize=None)
    def npc_point(npc, scene):
        row = db.execute("SELECT data FROM entity WHERE kind='npcs' AND id=?", (npc,)).fetchone()
        points = {(point["scene"], tuple(point["coordinates"])): point
                  for point in json.loads(row[0]).get("locations", [])
                  if point.get("scene") and point.get("coordinates") and
                  (not scene or canonical_location(point["scene"]) == canonical_location(scene))} if row else {}
        return next(iter(points.values())) if len(points) == 1 else {}

    links = {}

    def link(quest, place, detail, role):
        if place in known:
            proof = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
            links.setdefault(("quests", quest, role, "locations", place), proof)
            links.setdefault(("locations", place, "quests", "quests", quest), proof)

    for quest, raw in db.execute("SELECT id, json_extract(data,'$.starts') FROM entity WHERE kind='quests'"):
        for index, start in enumerate(json.loads(raw or "[]")):
            npc = start.get("npc_id")
            scene = start.get("scene")
            point = start.get("placement") or {}
            position = start.get("position") if start.get("coordinate_frame") == "world" else None
            if not point.get("coordinates") and not position and npc:
                point = npc_point(npc, scene) or point
            scene = point.get("scene") or scene
            if not scene:
                continue
            proof = point.get("source") or start.get("origin_evidence") or start.get("source")
            link(quest, scene, proof or {}, "origins")
            link(quest, canonical_location(scene), proof or {}, "origins")
            coordinates = point.get("coordinates") or ([float(value) for value in position] if position else None)
            if not coordinates:
                continue
            x, _, z = coordinates
            covered = {ident for ident, extent in areas[scene] if extent_intersects(extent, (x, z, x, z))}
            sites = {point["location_id"]} if point.get("location_id") in known else set()
            if key := point.get("building_key"):
                sites.add(f"{scene}/building/{key}")
            cell = point.get("cell") or {}
            for source in [cell.get("source"), *point.get("parent_sources", ()), point.get("source")]:
                matches = building_sources.get((scene, source_key(source)), set())
                if len(matches) == 1:
                    sites.update(matches)
            if uid := cell.get("object_id"):
                matches = building_cells.get((scene, str(uid)), set())
                if len(matches) == 1:
                    sites.update(matches)
            if uid := point.get("runtime_object_id") or point.get("object_id"):
                sites.update(objects.get((scene, str(uid)), ()))
            for site in tuple(sites):
                if "/building/" in site and (uid := site.rsplit("/", 1)[-1]):
                    sites.update(objects.get((scene, uid), ()))
                elif site in aliases:
                    sites.add(aliases[site])
            detail = {"start_index": index, "coordinates": coordinates, "source": proof}
            for site in sites & known:
                link(quest, site, detail, "origin_sites")
            for site in covered | (sites & known):
                visited = {site}
                while (parent := parents.get(site)) and parent not in visited:
                    visited.add(parent)
                    site = parent
                for area in visited - sites - {scene, canonical_location(scene)}:
                    link(quest, area, proof or {}, "origin_areas")

    db.executemany("INSERT OR IGNORE INTO link VALUES (?,?,?,?,?,?)",
                   ((*record, proof) for record, proof in links.items()))
