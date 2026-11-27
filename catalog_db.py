"""Local normalized index and read-only queries for the browser."""

from contextlib import contextmanager
from functools import cached_property
import json
import sqlite3

from catalog_location_hierarchy import canonical_location
from catalog_map import (
    AREA_LAYERS, BUILDING_LAYERS, MAP_INDEX, MAP_LOOKUP, MapWriter, area_record, area_window, extent_intersects,
    map_list, map_window, object_area_counts, object_area_window,
)
from catalog_drops import creature_drops, has_item_drops, item_drops
from catalog_placements import PLACEMENT_SCHEMA, build_placements
from catalog_quest_locations import publish_location_quests


KINDS = ("quests", "npcs", "locations", "items", "schematics", "resources", "ships", "conversations")
SCHEMA = 10
SEARCH_INDEX = """CREATE INDEX IF NOT EXISTS entity_search ON entity (kind, search, name COLLATE NOCASE, id)"""
BROWSE_INDEX = """CREATE INDEX IF NOT EXISTS entity_browse
    ON entity (kind, name COLLATE NOCASE, id, category, status, level, search, card)"""
BROWSE_SORT = {
    "name": "name COLLATE NOCASE",
    "world": """COALESCE((SELECT w.name FROM entity w WHERE w.kind='locations' AND w.id=entity.scene),
                         NULLIF(scene, '')) COLLATE NOCASE""",
    "category": "category COLLATE NOCASE",
    "level": "level",
    "giver": "json_extract(card, '$.givers[0]') COLLATE NOCASE",
    "difficulty": "COALESCE(json_extract(card, '$.difficulty'), category) COLLATE NOCASE",
    "map": "COALESCE(json_extract(card, '$.has_map'), 0)",
    "template": "json_extract(card, '$.template') COLLATE NOCASE",
    "station": "json_extract(card, '$.station') COLLATE NOCASE",
    "slots": "json_extract(card, '$.slots')",
    "products": "json_extract(card, '$.products')",
    "parent": "json_extract(card, '$.parent') COLLATE NOCASE",
    "worlds": "COALESCE(json_extract(card, '$.worlds'), 0)",
    "lines": "json_extract(card, '$.line_count')",
    "different": "COALESCE(json_extract(card, '$.different'), 0)",
    "missing": "COALESCE(json_extract(card, '$.missing'), 0)",
}
PLACEMENT_SORT = {
    "location": "sort_name COLLATE NOCASE",
    "world": """COALESCE((SELECT w.name FROM entity w WHERE w.kind='locations' AND w.id=placement.scene),
                         NULLIF(scene, '')) COLLATE NOCASE""",
    "method": "json_extract(data, '$.method') COLLATE NOCASE",
    "position": "json_extract(data, '$.coordinates[0]')",
    "source": "json_extract(data, '$.source.path') COLLATE NOCASE",
}
DIALOGUE_SORT = {
    "code": lambda line: line["code"],
    "text": lambda line: line["text"],
    "caption": lambda line: next((use["caption"] for use in line["usages"] if use.get("caption")), None),
    "alignment": lambda line: line["status"],
}


def packed(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def sql_sort(fields, sort):
    direction = "DESC" if sort.endswith("-desc") else "ASC"
    return f"{fields[sort.removesuffix('-desc')]} {direction} NULLS LAST"


class Writer:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.executescript("""
            CREATE TABLE entity (
                kind TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, category TEXT,
                scene TEXT, level INTEGER, status TEXT, search TEXT NOT NULL,
                card TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (kind, id)
            ) WITHOUT ROWID;
            CREATE TABLE scene_filter (
                kind TEXT, id TEXT, scene TEXT, PRIMARY KEY (kind, id, scene)
            ) WITHOUT ROWID;
            CREATE TABLE link (
                kind TEXT, id TEXT, role TEXT, target_kind TEXT, target_id TEXT, detail TEXT,
                PRIMARY KEY (kind, id, role, target_kind, target_id)
            ) WITHOUT ROWID;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE source_path (path TEXT PRIMARY KEY, remote_path TEXT NOT NULL) WITHOUT ROWID;
        """)
        self.connection.executescript(MAP_INDEX + MAP_LOOKUP + PLACEMENT_SCHEMA)
        self.markers = MapWriter(self.connection)

    def object_area(self, scene, feature):
        bounds = feature["extent"]["bounds"]
        search = (feature["name"] + " " + feature["template"]).casefold()
        self.connection.execute("INSERT INTO map_object_area VALUES (?,?,?,?,?,?,?,?)",
                                (scene, feature["layer"], *bounds, search, packed(feature)))

    def entity(self, kind, ident, name, data, *, category=None, scene=None, scenes=(), level=None,
               status=None, search=(), card=None):
        search_text = " ".join(str(value) for value in (ident, name, category, *search) if value).casefold()
        summary = {"id": ident, "name": name, "category": category, "scene": scene, "level": level,
                   "status": status, **(card or {})}
        self.connection.execute(
            "INSERT INTO entity VALUES (?,?,?,?,?,?,?,?,?,?)",
            (kind, ident, name, category, scene, level, status, search_text, packed(summary), packed(data)),
        )
        self.connection.executemany("INSERT INTO scene_filter VALUES (?,?,?)",
                                   [(kind, ident, value) for value in sorted({scene, *scenes} - {None, ""})])

    def link(self, kind, ident, role, target_kind, target_id, detail=None):
        self.connection.execute("INSERT OR IGNORE INTO link VALUES (?,?,?,?,?,?)",
                                (kind, ident, role, target_kind, target_id, packed(detail or {})))

    def finish(self, metadata, spawn_index=None):
        publish_location_quests(self.connection)
        counts = dict(self.connection.execute("""
            SELECT kind,COUNT(*) FROM entity
            WHERE kind<>'locations' OR json_extract(card, '$.alias_for') IS NULL
            GROUP BY kind
        """))
        placements = build_placements(self.connection, metadata, spawn_index)
        metadata = {**metadata, "schema": SCHEMA, "counts": counts, "placement_counts": placements}
        self.connection.execute("INSERT INTO metadata VALUES (?,?)", ("catalog", packed(metadata)))
        self.connection.executescript("""
            CREATE INDEX entity_name ON entity (kind, name COLLATE NOCASE, id);
            CREATE INDEX entity_category ON entity (kind, category);
            CREATE INDEX scene_filter_lookup ON scene_filter (scene, kind, id);
            CREATE INDEX link_reverse ON link (target_kind, target_id, role);
        """)
        self.connection.execute(SEARCH_INDEX)
        self.connection.execute(BROWSE_INDEX)
        self.connection.commit()
        return metadata


class Catalog:
    def __init__(self, path):
        self.path = path.resolve()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            yield connection
        finally:
            connection.close()

    def metadata(self):
        with self.connect() as db:
            row = db.execute("SELECT data FROM metadata WHERE key='catalog'").fetchone()
        return json.loads(row[0])

    def source_path(self, path):
        with self.connect() as db:
            row = db.execute("SELECT remote_path FROM source_path WHERE path=?", (path,)).fetchone()
        return row[0] if row else None

    def meta(self):
        with self.connect() as db:
            row = db.execute("SELECT data FROM metadata WHERE key='catalog'").fetchone()
            filters = {
                kind: [entry[0] for entry in db.execute(
                    "SELECT DISTINCT category FROM entity WHERE kind=? AND category IS NOT NULL ORDER BY category", (kind,))]
                for kind in KINDS
            }
            statuses = {
                kind: [entry[0] for entry in db.execute(
                    "SELECT DISTINCT status FROM entity WHERE kind=? AND status IS NOT NULL ORDER BY status", (kind,))]
                for kind in KINDS
            }
            return {**json.loads(row[0]), "categories": filters, "statuses": statuses}

    @cached_property
    def browse_source(self):
        # Keep filtered browse queries on the compact cards, away from detail JSON.
        with self.connect() as db:
            indexed = db.execute("SELECT 1 FROM sqlite_schema WHERE type='index' AND name='entity_browse'").fetchone()
        return "entity INDEXED BY entity_browse" if indexed else "entity"

    def list(self, kind, *, query="", scene="", category="", status="", min_level=None, max_level=None,
             acquisition="", storage="", page=1, size=30, sort="name"):
        conditions = ["kind=?"]
        values = [kind]
        if kind == "locations":
            if category != "Transport":
                conditions.append("json_extract(card, '$.alias_for') IS NULL")
            scene = canonical_location(scene)
        for term in query.casefold().split():
            conditions.append("instr(search, ?) > 0")
            values.append(term)
        if scene:
            conditions.append("EXISTS (SELECT 1 FROM scene_filter s WHERE s.kind=entity.kind AND s.id=entity.id AND s.scene=?)")
            values.append(scene)
        for column, operator, value in [("category", "=", category), ("status", "=", status),
                                        ("level", ">=", min_level), ("level", "<=", max_level)]:
            if value is not None and value != "":
                conditions.append(f"{column}{operator}?")
                values.append(value)
        if kind == "items" and acquisition:
            if acquisition in ("crafted", "quest"):
                role = "crafted_by" if acquisition == "crafted" else "reward_items_quests"
                conditions.append(
                    "EXISTS (SELECT 1 FROM link l WHERE l.kind=entity.kind AND l.id=entity.id AND l.role=?)")
                values.append(role)
            elif acquisition in ("vendor", "loot", "script"):
                conditions.append("json_extract(card, ?) > 0")
                values.append("$.acquisition." + acquisition)
        if kind == "items":
            if storage in ("datapad", "inventory"):
                conditions.append("json_extract(card, ?) > 0")
                values.append("$.storage." + storage)
            elif storage in ("physical", "intangible"):
                conditions.append("json_extract(card, '$.form') = ?")
                values.append(storage)
        where = " AND ".join(conditions)
        rank_args = ()
        if sort == "relevance":
            primary_order = """CASE WHEN lower(id)=? THEN 0 WHEN lower(name)=? THEN 1
                WHEN instr(lower(name),?)=1 THEN 2 WHEN instr(lower(name),?)>0 THEN 3 ELSE 4 END"""
            term = query.strip().lower()
            rank_args = (term, term, term, term)
        else:
            primary_order = sql_sort(BROWSE_SORT, sort)
        order = primary_order + (", id" if sort == "name" else ", name COLLATE NOCASE, id")
        source = self.browse_source
        with self.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM {source} WHERE {where}", values).fetchone()[0]
            pages = max(1, (total + size - 1) // size)
            page = min(page, pages)
            rows = db.execute(
                f"SELECT card FROM {source} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*values, *rank_args, size, (page - 1) * size),
            )
            return {"rows": [json.loads(row[0]) for row in rows], "total": total, "page": page,
                    "pages": pages, "size": size}

    def search(self, query, limit=5):
        terms = query.casefold().split()
        if not terms:
            return {}
        query = query.strip().lower()
        predicate = " AND ".join("instr(search, ?) > 0" for _ in terms)
        ordering = """CASE WHEN lower(id)=? THEN 0 WHEN lower(name)=? THEN 1
            WHEN instr(lower(name),?)=1 THEN 2 WHEN instr(lower(name),?)>0 THEN 3 ELSE 4 END"""
        found = {}
        with self.connect() as db:
            for kind in KINDS:
                primary = " AND json_extract(card, '$.alias_for') IS NULL" if kind == "locations" else ""
                ids = db.execute(f"""SELECT id FROM entity INDEXED BY entity_search WHERE kind=? AND {predicate}{primary}
                    ORDER BY {ordering}, name COLLATE NOCASE, id LIMIT ?""", (kind, *terms, *([query] * 4), limit))
                found[kind] = [json.loads(db.execute("SELECT card FROM entity WHERE kind=? AND id=?", (kind, ident)).fetchone()[0])
                               for ident, in ids]
        return found

    def item_loot_npcs(self, ident, page=1, size=30, *, query="", sort="chance-desc", summarize=False):
        if not self.has_npc_drops:
            return {"rows": [], "total": 0, "page": 1, "pages": 1, "size": size, "other_sources": []}
        with self.connect() as db:
            result = item_drops(db, ident, query, page, size, sort, summarize=summarize)
            page_rows = {row["id"]: row for row in result["rows"]}
            if not page_rows:
                return result
            # Keep exact leaf-column references in the existing API alongside
            # the roll details and source lines from the probability index.
            for row in page_rows.values():
                row["matches"] = []
            for npc, path, column, selector, proof in db.execute("""
                WITH item_tables AS (
                    SELECT DISTINCT json_extract(entry.value, '$.source.path') AS path,
                                    json_extract(entry.value, '$.column') AS column_name
                    FROM entity, json_each(entity.data, '$.acquisitions') AS entry
                    WHERE entity.kind='items' AND entity.id=? AND json_extract(entry.value, '$.kind')='loot'
                )
                SELECT npc_id, path, column_name, selector, proof
                FROM item_tables JOIN npc_loot_table USING(path,column_name)
                WHERE npc_id IN (SELECT value FROM json_each(?))
                ORDER BY path,column_name,selector
            """, (ident, packed(list(page_rows)))):
                page_rows[npc]["matches"].append({"path": path, "column": column, "selector": selector,
                                                 "source": json.loads(proof)})
            return result

    def model_profile(self, kind, ident):
        with self.connect() as db:
            row = db.execute("SELECT category, data FROM entity WHERE kind=? AND id=?", (kind, ident)).fetchone()
        if not row or kind == "locations" and row[0] != "Building":
            return None
        return json.loads(row[1])

    def entity(self, kind, ident, *, include_dialogue=False):
        if kind == "locations":
            ident = canonical_location(ident)
        with self.connect() as db:
            row = db.execute("SELECT card,data FROM entity WHERE kind=? AND id=?", (kind, ident)).fetchone()
            if not row:
                return None
            card, data = map(json.loads, row)
            grouped = {}
            for role, count in db.execute("SELECT role,COUNT(*) FROM link WHERE kind=? AND id=? GROUP BY role",
                                          (kind, ident)):
                grouped[role] = {"count": count, "rows": self._links(db, kind, ident, role, 1, 60)}
            if kind == "conversations" and not include_dialogue:
                data.pop("lines", None)
            result = {"card": card, "data": data, "related": grouped}
            if kind == "locations":
                parents = db.execute("""
                    WITH RECURSIVE ancestors(id, depth) AS (
                        VALUES (?,0)
                        UNION ALL
                        SELECT l.target_id, a.depth+1
                        FROM ancestors a JOIN link l ON l.kind='locations' AND l.id=a.id
                            AND l.role='parent' AND l.target_kind='locations'
                        WHERE a.depth<12
                    )
                    SELECT e.id, e.name, e.category
                    FROM ancestors a JOIN entity e ON e.kind='locations' AND e.id=a.id
                    WHERE a.depth>0 ORDER BY a.depth DESC
                """, (card.get("alias_for") or ident,))
                result["ancestors"] = [{"id": parent, "name": name, "category": category}
                                       for parent, name, category in parents]
            if kind == "items" or kind == "locations" and card.get("category") == "Building":
                result["placement_count"] = db.execute(
                    "SELECT COUNT(*) FROM placement WHERE kind=? AND owner=?", (kind, ident)).fetchone()[0]
                if kind == "items" and self.has_npc_drops:
                    result["has_loot"] = has_item_drops(db, ident)
            elif kind == "npcs" and self.has_npc_spawn_placements:
                result["placement_count"] = db.execute(
                    "SELECT COUNT(*) FROM npc_spawn_placement WHERE npc=? AND scope IN ('placed','scripted','region')",
                    (ident,)).fetchone()[0]
                result["spawn_source_count"] = db.execute(
                    "SELECT COUNT(*) FROM npc_spawn_placement WHERE npc=?", (ident,)).fetchone()[0]
                if self.has_npc_drops:
                    result["has_loot"] = db.execute("SELECT 1 FROM npc_drop_profile WHERE npc=? LIMIT 1", (ident,)).fetchone() is not None
            return result

    @cached_property
    def has_npc_drops(self):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM sqlite_schema WHERE name='npc_drop_profile'").fetchone() is not None

    def npc_drops(self, ident, query="", page=1, size=30, sort="chance-desc"):
        if not self.has_npc_drops:
            return {"total": 0, "page": 1, "size": size, "pages": 1, "rows": []}
        with self.connect() as db:
            return creature_drops(db, ident, query, page, size, sort)

    @cached_property
    def has_npc_spawn_placements(self):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='npc_spawn_placement'").fetchone() is not None

    def placements(self, kind, ident, *, scene="", query="", spawn_type="", page=1, size=30, sort=""):
        if kind == "npcs":
            if not self.has_npc_spawn_placements:
                return {"scenes": [], "total": 0, "page": 1, "size": size, "pages": 1, "rows": []}
            table = "npc_spawn_placement JOIN placement ON placement_id=id"
            base = "npc=?"
            values = [ident]
        else:
            table = "placement"
            base = "kind=? AND owner=?"
            values = [kind, ident]
        base_values = tuple(values)
        conditions = [base]
        if scene:
            conditions.append("scene=?")
            values.append(scene)
        if kind == "npcs" and spawn_type:
            conditions.append("spawn_type=?")
            values.append(spawn_type)
        for term in query.casefold().split()[:10]:
            conditions.append("instr(search,?)>0")
            values.append(term)
        where = " AND ".join(conditions)
        with self.connect() as db:
            scenes = [row[0] for row in db.execute(f"SELECT DISTINCT scene FROM {table} WHERE {base} AND scene<>'' ORDER BY scene", base_values)]
            total = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", values).fetchone()[0]
            pages = max(1, (total + size - 1) // size)
            page = min(page, pages)
            spawn_columns = ",scope,spawn_type" if kind == "npcs" else ""
            order = "scene, sort_name COLLATE NOCASE, id"
            if sort:
                fields = {**PLACEMENT_SORT, "method": "spawn_type COLLATE NOCASE"} if kind == "npcs" else PLACEMENT_SORT
                order = sql_sort(fields, sort) + ", " + order
            elif kind == "npcs":
                order = "CASE scope WHEN 'placed' THEN 0 WHEN 'scripted' THEN 1 WHEN 'region' THEN 2 ELSE 3 END, " + order
            rows = db.execute(f"""SELECT data,owner{spawn_columns} FROM {table} WHERE {where}
                ORDER BY {order} LIMIT ? OFFSET ?""", (*values, size, (page - 1) * size))
            types = [row[0] for row in db.execute(
                "SELECT DISTINCT spawn_type FROM npc_spawn_placement WHERE npc=? ORDER BY spawn_type", (ident,)
            )] if kind == "npcs" else []
            return {"scenes": scenes, "total": total, "page": page, "size": size, "pages": pages,
                    "types": types, "rows": [{**json.loads(data), "spawner_template": owner or None, "scope": tags[0],
                                              "spawn_type": tags[1], "subject": ident} if tags else json.loads(data)
                                             for data, owner, *tags in rows]}

    def placement(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT kind,owner,data,marker FROM placement WHERE id=?", (ident,)).fetchone()
            if not row:
                return None
            kind, owner, data, marker = row
            result = json.loads(data)
            point = json.loads(marker) if marker else None
            if point and kind == "locations":
                shape = db.execute("""SELECT json_extract(data,'$.extent'), json_extract(data,'$.geometry_kind'),
                    json_extract(data,'$.geometry_source') FROM entity WHERE kind=? AND id=?""", (kind, owner)).fetchone()
                if shape and shape[0]:
                    point.update(extent=json.loads(shape[0]), geometry_kind=shape[1],
                                 geometry_source=json.loads(shape[2]) if shape[2] else None)
            return {**result, "marker": point, "owner_kind": kind, "owner": owner}

    @staticmethod
    def _links(db, kind, ident, role, page, size):
        rank = "json_extract(l.detail, '$.rank'), " if kind == "locations" and role == "places" else ""
        rows = db.execute(f"""
            SELECT l.target_kind,l.target_id,l.detail,e.card
            FROM link l LEFT JOIN entity e ON e.kind=l.target_kind AND e.id=l.target_id
            WHERE l.kind=? AND l.id=? AND l.role=?
            ORDER BY {rank}e.name COLLATE NOCASE,l.target_id LIMIT ? OFFSET ?
        """, (kind, ident, role, size, (page - 1) * size))
        return [{"kind": target_kind, "id": target_id, "detail": json.loads(detail),
                 "card": json.loads(card) if card else None}
                for target_kind, target_id, detail, card in rows]

    def links(self, kind, ident, role, page, size):
        if kind == "locations":
            ident = canonical_location(ident)
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM link WHERE kind=? AND id=? AND role=?",
                               (kind, ident, role)).fetchone()[0]
            return {"rows": self._links(db, kind, ident, role, page, size), "total": total,
                    "page": page, "size": size}

    def map_markers(self, scene, layers, bounds, query, columns, rows):
        with self.connect() as db:
            result = map_window(db, scene, layers, bounds, query, columns, rows)
        return {**result, "groups": self.with_map_details(scene, result["groups"])}

    def map_list(self, scene, layers, bounds, query, page, size):
        with self.connect() as db:
            result = map_list(db, scene, layers, bounds, query, page, size)
        return {**result, "rows": self.with_map_details(scene, result["rows"])}

    @cached_property
    def areas(self):
        scenes = {}
        with self.connect() as db:
            rows = db.execute("""SELECT id, name, scene, category, data FROM entity
                WHERE kind='locations' AND category IN ('Region','Buildout area')""")
            for ident, name, scene, category, data in rows:
                if area := area_record(ident, name, category, json.loads(data)):
                    scenes.setdefault(scene, {})[ident] = area
        return scenes

    @cached_property
    def building_profiles(self):
        with self.connect() as db:
            records = db.execute("SELECT id, name, card FROM entity WHERE kind='locations' AND category='Building'")
            return {ident: (name, json.loads(card).get("cells", 0)) for ident, name, card in records}

    def with_map_details(self, scene, markers):
        areas = self.areas.get(scene, {})
        result = []
        for marker in markers:
            target = marker.get("target")
            if target and target[0] == "locations" and (area := areas.get(target[1])):
                marker = {**marker, **{key: area[key] for key in ("extent", "description", "no_build")}}
            elif (marker.get("template", "").startswith("object/building/") and
                  (object_id := marker.get("object_id")) and
                  (profile := self.building_profiles.get(f"{scene}/building/{object_id}"))):
                name, count = profile
                marker = {**marker, "name": name, "enterable": True, "cell_count": count,
                          "target": ["locations", f"{scene}/building/{object_id}"],
                          "context": "Building with interior cells"}
            result.append(marker)
        return result

    def map_areas(self, scene, layers, bounds, query):
        footprints = set(layers) & ({"no_build"} | BUILDING_LAYERS.keys())
        result = area_window(self.areas.get(scene, {}).values(), layers, bounds, query)
        if footprints:
            with self.connect() as db:
                result.extend(object_area_window(db, scene, footprints, bounds, query))
        return result

    def map_area_counts(self, scene, bounds):
        with self.connect() as db:
            counts = object_area_counts(db, scene, bounds)
        for area in self.areas.get(scene, {}).values():
            if extent_intersects(area["extent"], bounds):
                for layer in area["layers"]:
                    counts[layer] = counts.get(layer, 0) + 1
        return {layer: counts[layer] for layer in AREA_LAYERS if layer in counts}

    def dialogue(self, table, query, status, page, size, sort=""):
        with self.connect() as db:
            row = db.execute("SELECT data FROM entity WHERE kind='conversations' AND id=?", (table,)).fetchone()
            if not row:
                return None
            npc_count = db.execute(
                "SELECT COUNT(*) FROM link WHERE kind='conversations' AND id=? AND role='npcs'", (table,)
            ).fetchone()[0]
            npc_rows = self._links(db, "conversations", table, "npcs", 1, 8) if npc_count else []
        data = json.loads(row[0])
        lines = data["lines"]
        if query:
            term = query.casefold()
            lines = [line for line in lines if term in (
                line["code"] + " " + (line["text"] or "") + " " + " ".join(
                    use.get("caption") or "" for use in line["usages"])
            ).casefold()]
        if status:
            lines = [line for line in lines if line["status"] == status]
        if sort:
            value = DIALOGUE_SORT[sort.removesuffix("-desc")]
            lines.sort(key=lambda line: (value(line) or "").casefold(), reverse=sort.endswith("-desc"))
            lines.sort(key=lambda line: not value(line))
        offset = (page - 1) * size
        return {"rows": lines[offset:offset + size], "total": len(lines), "page": page, "size": size,
                "npcs": {"count": npc_count, "rows": npc_rows}}
