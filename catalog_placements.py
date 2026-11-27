"""Index every recovered item placement and its map destination."""

from collections import defaultdict
from hashlib import blake2s
import json
import math
from pathlib import PurePosixPath

from catalog_map import area_record


PLACEMENT_SCHEMA = """
    CREATE TABLE placement (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL, scene TEXT NOT NULL,
        sort_name TEXT NOT NULL, search TEXT NOT NULL, data TEXT NOT NULL, marker TEXT
    ) WITHOUT ROWID;
    CREATE INDEX placement_owner ON placement (kind, owner, scene, sort_name COLLATE NOCASE, id);
    CREATE TABLE npc_spawn_placement (
        npc TEXT NOT NULL, placement_id TEXT NOT NULL, scope TEXT NOT NULL, spawn_type TEXT NOT NULL,
        PRIMARY KEY (npc, placement_id)
    ) WITHOUT ROWID;
    CREATE INDEX npc_spawn_scope ON npc_spawn_placement(npc, scope);
    CREATE TABLE spawn_audit (
        source_path TEXT NOT NULL, source_line INTEGER NOT NULL, data TEXT NOT NULL
    );
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _coordinates(point):
    values = point.get("coordinates")
    if values and len(values) == 3 and all(isinstance(n, (int, float)) and math.isfinite(n) for n in (values[0], values[2])):
        return values
    return None


def _point_identity(item, scene, proof, coordinates):
    return item, scene, proof.get("path"), proof.get("line"), proof.get("row"), tuple(coordinates or ())


class PlacementWriter:
    def __init__(self, db, metadata):
        self.db = db
        self.worlds = metadata["locations"]
        self.maps = metadata["maps"]
        self.regions = defaultdict(list)
        self.room_points = set()
        self.pending = []
        cities = {city["target"][1] for chart in self.maps.values() for city in chart.get("cities", ())
                  if city.get("target")}
        for ident, name, scene, raw in db.execute(
            "SELECT id,name,scene,data FROM entity WHERE kind='locations' AND category='Region'"
        ):
            data = json.loads(raw)
            if ident not in cities and not data.get("attributes", {}).get("visible"):
                continue
            if not data.get("map") or data["map"]["scene"] != scene:
                continue
            if record := area_record(ident, name, "Region", data):
                extent = record["extent"]
                left, south, right, north = extent["bounds"]
                area = (right - left) * (north - south)
                self.regions[scene].append((ident not in cities, area, ident, name, extent))
        for regions in self.regions.values():
            regions.sort(key=lambda p: p[:3])

    def site(self, scene, coordinates):
        name = self.worlds.get(scene, {}).get("name", scene)
        if not coordinates:
            return scene, name
        x, _, z = coordinates
        for _, _, ident, label, extent in self.regions[scene]:
            left, south, right, north = extent["bounds"]
            if not (left <= x <= right and south <= z <= north):
                continue
            if extent["shape"] == "circle":
                cx, cz, radius = extent["values"]
                if (x - cx) ** 2 + (z - cz) ** 2 > radius ** 2:
                    continue
            return ident, label
        return scene, name

    def add(self, kind, owner, scene, site, label, method, point, *,
            building=None, room=None, object_index=None, subjects=None, scope=None):
        coordinates = _coordinates(point)
        proof = point.get("source") or {}
        token = blake2s(_json([kind, method, *_point_identity(owner, scene, proof, coordinates),
                               building, room, subjects[:2] if subjects else None,
                               scope, point.get("origin_key"), point.get("anchor_source"), point.get("region_source")
                               ]).encode("utf-8"), digest_size=12).hexdigest()
        site_id, site_name = site
        destination = building if room is not None else site_id if coordinates and scene in self.maps else None
        entry = {
            "id": token, "scene": scene, "place": site_name, "location": label,
            "map_location": destination, "method": method, "coordinates": coordinates,
            "source": proof, "cell": point.get("cell"), "cell_index": room, "object_index": object_index,
            "related": subjects, "event": point.get("required_event"),
        }
        if spawner := point.get("spawner"):
            entry["spawner"] = spawner
        if origin := point.get("spawn_origin"):
            entry["spawn_origin"] = origin
        if scope:
            entry.update(scope=scope, status=point.get("status"), location_id=point.get("location_id"),
                         anchor_source=point.get("anchor_source"), region_source=point.get("region_source"),
                         reference_coordinates=point.get("reference_coordinates"),
                         local_coordinates=point.get("local_coordinates"))
        search = " ".join(filter(None, (label, site_name, scene, self.worlds.get(scene, {}).get("name"),
                                        entry["cell"], (subjects or [None, None, None])[2], method,
                                        (spawner or {}).get("type"), (spawner or {}).get("name")))).casefold()
        marker = {**point, "id": token} if coordinates and destination else None
        if marker and room is not None:
            marker.update(layer="spawn" if point.get("spawner") else "item", context="Recorded room placement")
        self.pending.append((token, kind, owner, scene, label, search, _json(entry), _json(marker) if marker else None))
        if len(self.pending) >= 2048:
            self.flush()
        return token

    def flush(self):
        self.db.executemany("INSERT OR IGNORE INTO placement VALUES (?,?,?,?,?,?,?,?)", self.pending)
        self.pending.clear()

    def buildings(self):
        for ident, name, scene, raw in self.db.execute(
            "SELECT id,name,scene,data FROM entity WHERE kind='locations' AND category='Building' ORDER BY id"
        ):
            building = json.loads(raw)
            site = self.site(scene, building.get("coordinates"))
            marker = {key: building[key] for key in
                      ("name", "coordinates", "source", "template", "enterable", "required_event", "context") if key in building}
            marker.update(target=["locations", ident], layer="building_enterable")
            self.add("locations", ident, scene, site, site[1], "Building", marker)
            for room in building["cells"]:
                for index, item in enumerate(room["contents"]):
                    target = item.get("target")
                    if not target or target[0] != "items":
                        continue
                    point = {**item, "cell": room["name"]}
                    origin = item.get("spawn_origin")
                    method = origin["type"] if origin else "Configured spawn" if item["origin"] == "building_spawn" else (
                        "Placed spawner" if item.get("spawner") else "Placed object")
                    self.add("items", target[1], scene, site, name, method,
                             point, building=ident, room=room["index"], object_index=index)
                    self.room_points.add(_point_identity(target[1], scene, item.get("source") or {}, _coordinates(item)))

    def objects(self):
        for scene, raw in self.db.execute("SELECT scene,data FROM map_marker WHERE json_extract(data,'$.target[0]')='items'"):
            point = json.loads(raw)
            owner = point["target"][1]
            coordinates = _coordinates(point)
            if _point_identity(owner, scene, point.get("source") or {}, coordinates) in self.room_points:
                continue
            site = self.site(scene, coordinates)
            self.add("items", owner, scene, site, site[1], "Placed spawner" if point.get("spawner") else "Placed object", point)

    def quest_rewards(self):
        rows = self.db.execute("""
            SELECT l.id,q.id,q.name,l.detail,q.data FROM link l JOIN entity q ON q.kind='quests' AND q.id=l.target_id
            WHERE l.kind='items' AND l.role='reward_items_quests' AND l.target_kind='quests'
              AND json_extract(l.detail, '$.timing') IN ('legacy_turn_in','script_turn_in')
        """)
        for item, quest, title, raw_reward, raw_quest in rows:
            reward = json.loads(raw_reward)
            proof = reward["source"]
            for start in json.loads(raw_quest)["starts"]:
                if reward["timing"] == "legacy_turn_in":
                    matches = start.get("source") == proof
                else:
                    script = proof.get("path", "").partition("/script/")[2]
                    matches = bool(script) and start.get("script") == str(PurePosixPath(script).with_suffix("")).replace("/", ".")
                point = start.get("placement") or {}
                if not matches or not (scene := point.get("scene")) or not (coordinates := _coordinates(point)):
                    continue
                site = self.site(scene, coordinates)
                npc = start.get("npc") or {}
                marker = {
                    "name": npc.get("text") or title, "coordinates": coordinates,
                    "source": proof, "parent_source": point.get("source"), "target": ["quests", quest],
                    "layer": "quest_start", "context": "Quest turn-in", "summary": title,
                    "cell": (point.get("cell") or {}).get("name"),
                }
                self.add("items", item, scene, site, site[1], "Quest turn-in", marker, subjects=["quests", quest, title])


    def spawns(self, index):
        npcs = {row[0] for row in self.db.execute("SELECT id FROM entity WHERE kind='npcs'")}
        items = {row[0] for row in self.db.execute("SELECT id FROM entity WHERE kind='items'")}
        buildings = dict(self.db.execute("SELECT id,name FROM entity WHERE kind='locations' AND category='Building'"))
        item_points = defaultdict(list)
        for token, template, scene, raw in self.db.execute("""
            SELECT id,owner,scene,data FROM placement WHERE kind='items' AND json_type(data,'$.spawner')='object'
        """):
            point = json.loads(raw)
            proof = point["source"]
            item_points[template, scene, proof.get("path"), proof.get("line")].append((token, point["coordinates"]))

        matches = []
        for point, config, scope, template in index.records:
            coordinates, proof = point.get("coordinates"), point["source"]
            scene = point.get("scene") or ""
            token = None
            if template in items and scope == "placed":
                candidates = item_points[template, scene, proof.get("path"), proof.get("line")]
                same_position = [entry for entry in candidates if coordinates and entry[1] == coordinates]
                if len(same_position or candidates) == 1:
                    token = (same_position or candidates)[0][0]
            if token is None:
                cell = point.get("cell") or {}
                cell_name = cell.get("name") or point.get("room")
                building = f"{scene}/building/{point['building_key']}" if point.get("building_key") else None
                if building not in buildings:
                    building = None
                if location_id := point.get("location_id"):
                    site = location_id, point.get("location") or self.worlds.get(scene, {}).get("name", scene)
                else:
                    site = self.site(scene, coordinates)
                label = buildings.get(building) or point.get("location") or site[1] or config.get("name") or config["type"]
                if not coordinates and scope in {"definition", "runtime", "development"} and config.get("name"):
                    label = config["name"]
                owner = template if template in items else ""
                target = ["items", owner] if owner else ["npcs", config["entries"][0]["npc"]] if (
                    config["entries"] and config["entries"][0].get("npc") in npcs) else None
                marker = {**point, "spawner": config, "cell": cell_name, "target": target,
                          "name": config.get("name") or label, "layer": "spawn", "context": config["type"],
                          "origin_key": [point.get("area_index"), point.get("runtime_object_id"),
                                         config.get("source"), config.get("caller_source"), [entry["ref"] for entry in config["entries"]]]}
                room = cell.get("index") if building else None
                token = self.add("spawns", owner, scene, site, label, config["type"], marker,
                                 building=building, room=room, scope=scope)
            for npc in {entry.get("npc") for entry in config["entries"]} & npcs:
                matches.append((npc, token, scope, config["type"]))
        self.flush()
        self.db.executemany("INSERT OR IGNORE INTO npc_spawn_placement VALUES (?,?,?,?)", matches)
        self.db.executemany("INSERT INTO spawn_audit VALUES (?,?,?)", (
            (row["source"]["path"], row["source"]["line"], _json(row)) for row in index.scripts.unresolved))


def build_placements(db, metadata, spawn_index=None):
    writer = PlacementWriter(db, metadata)
    writer.buildings()
    writer.objects()
    writer.quest_rewards()
    writer.flush()
    if spawn_index is not None:
        writer.spawns(spawn_index)
    db.execute("""
        INSERT OR IGNORE INTO link
        SELECT 'npcs', s.npc, 'spawners', 'items', p.owner, json_object('placements', COUNT(*))
        FROM npc_spawn_placement s JOIN placement p ON p.id=s.placement_id
        WHERE p.owner <> ''
        GROUP BY s.npc, p.owner
    """)
    return dict(db.execute("SELECT kind,count(*) FROM placement GROUP BY kind"))
