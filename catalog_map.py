"""Bounded queries for the archive's world-coordinate map overlays."""

from collections import Counter
import json
import math


LAYERS = {
    "city": {"label": "Major cities", "symbol": "⌂", "default": True},
    "landmark": {"label": "Landmarks", "symbol": "◆", "default": True},
    "region": {"label": "Region and area pins", "symbol": "◇", "default": True},
    "transit": {"label": "Travel and transit", "symbol": "↗", "default": True},
    "quest_start": {"label": "Quest starts", "symbol": "!", "default": True},
    "quest_objective": {"label": "Quest objectives", "symbol": "◎", "default": True},
    "npc": {"label": "NPCs", "symbol": "●", "default": True},
    "spawn": {"label": "Creatures and spawn points", "symbol": "✦", "default": False},
    "service": {"label": "Terminals and services", "symbol": "■", "default": True},
    "resource": {"label": "Mining and resource sites", "symbol": "⬢", "default": True},
    "structure": {"label": "Buildings and structures", "symbol": "⌂", "default": False},
    "item": {"label": "Placed items", "symbol": "✚", "default": False},
    "space": {"label": "Space objects", "symbol": "✧", "default": True},
    "scenery": {"label": "Scenery and other objects", "symbol": "•", "default": False},
    "interior": {"label": "Interior cells", "symbol": "◫", "default": False},
}
AREA_LAYERS = {
    "region_bounds": {"label": "Region boundaries", "symbol": "□", "default": True, "area": True},
    "no_build": {"label": "No-build zones", "symbol": "▨", "default": True, "area": True},
    "buildout_bounds": {"label": "Buildout boundaries", "symbol": "◇", "default": False, "area": True},
}
BUILDING_LAYERS = {
    "building_enterable": {"label": "Buildings with interiors", "symbol": "▣", "default": True, "area": True, "close_zoom": True},
    "building_filler": {"label": "Scenery buildings", "symbol": "□", "default": True, "area": True, "close_zoom": True},
}
AREA_LAYERS.update(BUILDING_LAYERS)
MAP_INDEX = """
    CREATE TABLE map_marker (
        id INTEGER PRIMARY KEY, scene TEXT NOT NULL, layer TEXT NOT NULL,
        x REAL NOT NULL, y REAL, z REAL NOT NULL, name TEXT NOT NULL,
        search TEXT NOT NULL, data TEXT NOT NULL
    );
    CREATE TABLE map_object_area (
        scene TEXT NOT NULL, layer TEXT NOT NULL, xmin REAL NOT NULL, zmin REAL NOT NULL,
        xmax REAL NOT NULL, zmax REAL NOT NULL, search TEXT NOT NULL, data TEXT NOT NULL
    );
"""
MAP_LOOKUP = """
    CREATE INDEX map_marker_layer_x ON map_marker (scene, layer, x, z);
    CREATE INDEX map_marker_scene_x ON map_marker (scene, x, z);
    CREATE INDEX map_object_area_scene_x ON map_object_area (scene, xmin);
"""


def bounds_from_query(value, chart, lists=False):
    """Keep a visible window inside the selected chart's coordinate extent."""
    if not value:
        return list(chart)
    try:
        result = [float(part) for part in value.split(",")]
    except ValueError:
        result = []
    if len(result) != 4 or not all(math.isfinite(item) for item in result):
        raise ValueError("Supply four finite map boundaries.")
    x1, z1, x2, z2 = result
    if x1 > x2 or z1 > z2 or (not lists and (x1 == x2 or z1 == z2)):
        raise ValueError("Supply the minimum and maximum map coordinates in order.")
    return [max(x1, chart[0]), max(z1, chart[1]), min(x2, chart[2]), min(z2, chart[3])]


def _condition(scene, layers, bounds, query):
    minimum_x, minimum_z, maximum_x, maximum_z = bounds
    terms = query.casefold().split()[:10]
    selected = ",".join("?" for _ in layers)
    conditions = [f"scene=? AND layer IN ({selected})", "x BETWEEN ? AND ?", "z BETWEEN ? AND ?"]
    conditions.extend("instr(search, ?) > 0" for _ in terms)
    return " AND ".join(conditions), [scene, *layers, minimum_x, maximum_x, minimum_z, maximum_z, *terms]


def _details(db, ids):
    if not ids:
        return {}
    params = ",".join("?" for _ in ids)
    return {ident: json.loads(data) for ident, data in db.execute(
        f"SELECT id,data FROM map_marker WHERE id IN ({params})", ids)}


def map_cities(db, scene, bounds, query=""):
    where, values = _condition(scene, ("city",), bounds, query)
    return [{"id": ident, **json.loads(data)} for ident, data in db.execute(
        f"SELECT id,data FROM map_marker WHERE {where} ORDER BY name COLLATE NOCASE,id", values)]


def map_window(db, scene, layers, bounds, query, columns, rows):
    if not layers or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        return {"groups": [], "total": 0}
    cities = map_cities(db, scene, bounds, query) if "city" in layers else []
    grouped_layers = tuple(layer for layer in layers if layer != "city")
    if not grouped_layers:
        return {"groups": cities, "total": len(cities)}
    where, values = _condition(scene, grouped_layers, bounds, query)
    xmin, zmin, xmax, zmax = bounds
    grid = [columns - 1, xmin, (xmax - xmin) / columns, rows - 1, zmin, (zmax - zmin) / rows]
    # Within a screen cell, collapse coincident or very close points. The
    # actual bounds and count let the browser list every member on demand.
    grouped = db.execute(f"""
        SELECT MIN(id), COUNT(*), AVG(x), AVG(z), MIN(x), MIN(z), MAX(x), MAX(z),
               MIN(layer), MAX(layer)
        FROM map_marker WHERE {where}
        GROUP BY CAST(MIN(?, (x - ?) / ?) AS INTEGER), CAST(MIN(?, (z - ?) / ?) AS INTEGER)
    """, (*values, *grid)).fetchall()
    markers = _details(db, [row[0] for row in grouped if row[1] == 1])
    groups = []
    for ident, count, x, z, low_x, low_z, high_x, high_z, first_layer, last_layer in grouped:
        if count == 1:
            groups.append({"id": ident, **markers[ident]})
        else:
            groups.append({"id": f"group-{ident}", "count": count, "coordinates": [x, None, z],
                           "bounds": [low_x, low_z, high_x, high_z],
                           "layer": first_layer if first_layer == last_layer else None})
    return {"groups": groups + cities, "total": sum(row[1] for row in grouped) + len(cities)}


def map_list(db, scene, layers, bounds, query, page, size):
    if not layers or bounds[0] > bounds[2] or bounds[1] > bounds[3]:
        return {"rows": [], "total": 0}
    where, values = _condition(scene, layers, bounds, query)
    total = db.execute(f"SELECT COUNT(*) FROM map_marker WHERE {where}", values).fetchone()[0]
    entries = db.execute(f"""SELECT id,data FROM map_marker WHERE {where}
        ORDER BY name COLLATE NOCASE,id LIMIT ? OFFSET ?""", (*values, size, (page - 1) * size))
    return {"rows": [{"id": ident, **json.loads(data)} for ident, data in entries], "total": total}


def chart_counts(db, scene, bounds):
    where, values = _condition(scene, tuple(LAYERS), bounds, "")
    return dict(db.execute(f"SELECT layer,COUNT(*) FROM map_marker WHERE {where} GROUP BY layer", values))


def _object_area_extent(scene, bounds):
    xmin, zmin, xmax, zmax = bounds
    return "scene=? AND xmin<=? AND xmax>=? AND zmin<=? AND zmax>=?", (scene, xmax, xmin, zmax, zmin)


def object_area_counts(db, scene, bounds):
    where, values = _object_area_extent(scene, bounds)
    return dict(db.execute(f"SELECT layer, COUNT(*) FROM map_object_area WHERE {where} GROUP BY layer", values))


def object_area_window(db, scene, layers, bounds, query):
    if not layers or bounds[0] > bounds[2] or bounds[1] > bounds[3]:
        return []
    where, values = _object_area_extent(scene, bounds)
    selected = ",".join("?" for _ in layers)
    terms = query.casefold().split()[:10]
    clauses = [where, f"layer IN ({selected})", *("instr(search, ?) > 0" for _ in terms)]
    result = []
    for ident, data in db.execute(f"SELECT rowid, data FROM map_object_area WHERE {' AND '.join(clauses)}",
                                  (*values, *layers, *terms)):
        feature = json.loads(data)
        if extent_intersects(feature["extent"], bounds):
            result.append({"id": f"object-area-{ident}", **feature})
    return result


def area_record(ident, name, category, data):
    boundary = data.get("boundary") or {}
    shape = boundary.get("shape", "rectangle")
    values = boundary.get("values") or data.get("bounds") or []
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
        return None
    if shape == "circle" and len(values) == 3 and values[2] > 0:
        x, z, radius = values
        bounds = [x - radius, z - radius, x + radius, z + radius]
    elif shape == "rectangle" and len(values) == 4 and values[0] < values[2] and values[1] < values[3]:
        bounds = values
        x, z = (values[0] + values[2]) / 2, (values[1] + values[3]) / 2
    else:
        return None
    no_build = category == "Region" and data.get("attributes", {}).get("buildable") in (False, 0, "0", "false")
    layers = ["region_bounds" if category == "Region" else "buildout_bounds"]
    if no_build:
        layers.append("no_build")
    return {
        "name": name, "target": ["locations", ident], "coordinates": [x, None, z],
        "extent": {"shape": shape, "values": values, "bounds": bounds}, "no_build": no_build, "layers": layers,
        "context": category, "description": (data.get("description") or {}).get("text"),
        "source": next(iter(data.get("sources") or []), None),
    }


def extent_intersects(extent, bounds):
    left, bottom, right, top = extent["bounds"]
    xmin, zmin, xmax, zmax = bounds
    if xmax < left or xmin > right or zmax < bottom or zmin > top or xmax < xmin or zmax < zmin:
        return False
    if extent["shape"] != "circle":
        return True
    x, z, radius = extent["values"]
    dx = max(xmin - x, x - xmax, 0)
    dz = max(zmin - z, z - zmax, 0)
    return dx * dx + dz * dz <= radius * radius


def area_window(areas, layers, bounds, query):
    if not layers:
        return []
    terms = query.casefold().split()[:10]
    result = []
    for area in areas:
        # A no-build region belongs to both layers, and receives the more
        # specific styling when both are visible.
        layer = next((key for key in reversed(area["layers"]) if key in layers), None)
        if not layer or not extent_intersects(area["extent"], bounds):
            continue
        search = (area["name"] + " " + area["target"][1]).casefold()
        if all(term in search for term in terms):
            result.append({**area, "layer": layer})
    return result


class MapWriter:
    def __init__(self, db):
        self.db = db
        self.counts = Counter()
        self.extents = {}
        self.pending = []

    def add(self, scene, layer, name, point, **details):
        if not scene or layer not in LAYERS or not point or len(point) != 3:
            return
        x, y, z = point
        if not (math.isfinite(x) and math.isfinite(z) and (y is None or math.isfinite(y))):
            return
        full = {"layer": layer, "name": name, "coordinates": point,
                **{key: value for key, value in details.items() if value is not None and value != ""}}
        search = " ".join(str(value) for value in
                          (name, details.get("template"), details.get("object_id"), details.get("context"))
                          if value).casefold()
        self.pending.append((scene, layer, x, y, z, name, search,
                             json.dumps(full, ensure_ascii=False, separators=(",", ":"))))
        if len(self.pending) >= 2000:
            self.flush()
        self.counts[layer] += 1
        if scene not in self.extents:
            self.extents[scene] = [x, z, x, z]
        else:
            extent = self.extents[scene]
            extent[0] = min(extent[0], x)
            extent[1] = min(extent[1], z)
            extent[2] = max(extent[2], x)
            extent[3] = max(extent[3], z)

    def flush(self):
        self.db.executemany("INSERT INTO map_marker(scene,layer,x,y,z,name,search,data) VALUES (?,?,?,?,?,?,?,?)",
                            self.pending)
        self.pending.clear()
