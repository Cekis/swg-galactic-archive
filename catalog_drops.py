"""Index the game's corpse and starship loot rolls, preserving table multiplicity."""

from collections import defaultdict
from fractions import Fraction
from functools import lru_cache
import json
import math
import re
import struct

from catalog_loot import LootTables, SPACE_LOOT
from catalog_sources import SERVER, number


DROP_SCHEMA = """
CREATE TABLE loot_pool (
    pool TEXT NOT NULL, item TEXT NOT NULL, level_gate INTEGER NOT NULL, chance REAL NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (pool, item, level_gate)
) WITHOUT ROWID;
CREATE TABLE npc_drop_profile (
    npc TEXT NOT NULL, channel TEXT NOT NULL, pool TEXT NOT NULL, rolls INTEGER NOT NULL, gate REAL NOT NULL, proof TEXT NOT NULL,
    PRIMARY KEY (npc, channel, pool)
) WITHOUT ROWID;
CREATE INDEX loot_pool_item ON loot_pool(item, pool);
CREATE INDEX npc_drop_pool ON npc_drop_profile(pool, npc);
CREATE TABLE item_loot_source (
    item TEXT NOT NULL, source_id TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (item, source_id)
) WITHOUT ROWID;
"""
COLLECTION_TABLE = "loot/loot_items/collectible/collection_loot.tab"
SPACE_COLLECTION_TABLE = "space_loot/space_collection_loot.tab"
SCRIPT_ROOT = "dsrc/sku.0/sys.server/compiled/game/script/library/"
ENZYME = "object/tangible/loot/beast/enzyme_1.iff"
DYNAMIC_TYPES = {"weapon": "weapons", "armor": "armor", "clothing": "clothing"}


def _proof(script, line):
    return {"path": SCRIPT_ROOT + script + ".java", "line": line}


def _packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _float32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def gate_probability(limit, sides):
    """The scripts compare an inclusive integer roll with a strict upper threshold."""
    return max(0, min(sides, math.ceil(limit) - 1)) / sides


def _merge(result, children, multiplier, proof=(), minimum=None):
    for (item, level), (probability, sources) in children.items():
        key = item, max(level, minimum or 0)
        previous, evidence = result.get(key, (Fraction(0), set()))
        result[key] = previous + probability * multiplier, evidence | sources | set(proof)


def _key(proof):
    return proof["path"], proof.get("line")


class DropTables(LootTables):
    @lru_cache(maxsize=2048)
    def _defaults(self, relative):
        path = SERVER / relative
        if not self.files.exists(path):
            return {}
        lines = self.files.text(path).splitlines()
        if len(lines) < 2:
            return {}
        return {name: definition[2:-1] for name, definition in zip(lines[0].split("\t"), lines[1].split("\t"))
                if definition.startswith("s[") and definition.endswith("]")}

    def column(self, relative, column, *, required=False):
        for entry in super().column(relative, column, required=required):
            if entry.value.strip('"') != self._defaults(relative).get(column, ""):
                yield entry

    def _choose(self, entries, resolve, ancestors=()):
        entries = list(entries)
        result = {}
        if entries:
            probability = Fraction(1, len(entries))
            for entry in entries:
                _merge(result, resolve(entry.value, ancestors), probability, (_key(entry.source),))
        return result

    def _resolve(self, item, ancestors=()):
        if item.startswith("loot_items/"):
            path, separator, column = item.partition(":")
            if not separator or item in ancestors or len(ancestors) > 8:
                return {(item, 0): (Fraction(1), set())}
            rows = tuple(self.column("loot/" + path + ".tab", column))
            return self._choose(rows, self._resolve, (*ancestors, item)) if rows else {(item, 0): (Fraction(1), set())}
        if item.startswith("dynamic_"):
            return self._dynamic(item)
        return {(item, 0): (Fraction(1), set())}

    @lru_cache(maxsize=1024)
    def _dynamic(self, item):
        group = DYNAMIC_TYPES.get(item.split("_")[1])
        level = 22 if group == "armor" else 0
        path = SERVER / "item/dynamic_item/types" / f"{group}.tab"
        rows = self.files.tab(path) if group and self.files.exists(path) else ()
        row = next((row for row in rows if next(iter(row.cells.values()), "") == item), None)
        appearance = row.get("strBaseAppearance") if row else ""
        if appearance.endswith(".iff"):
            return {(appearance, level): (Fraction(1), {_key(row.source)})}
        if appearance:
            choices = self._choose(self.column(f"item/dynamic_item/appearances/{appearance}.tab", "strTemplate"), self._resolve)
            if choices:
                result = {}
                _merge(result, choices, Fraction(1), (_key(row.source),), level)
                return result
        return {(item, level): (Fraction(1), set())}

    def _ground_group(self, group, ancestors):
        table, _, column = group.partition(":")
        return self._choose(self.column(f"loot/loot_items/{table}.tab", column or "strItemType"), self._resolve, ancestors)

    def _space_group(self, column, ancestors):
        return self._choose(self.column(SPACE_LOOT, column), self._resolve, ancestors)

    def _collection_group(self, table, column, ancestors):
        return self._choose(self.column(table, column), self._resolve, ancestors)

    @lru_cache(maxsize=1024)
    def distribution(self, channel, selector):
        if channel == "space":
            return self._choose(self.column("space_loot/loot_lookup.tab", selector), self._space_group)
        if channel == "enzyme":
            return {(ENZYME, 0): (Fraction(1), {_key(_proof("loot", 1813))})}
        if channel in {"collection", "space_collection"}:
            result = {}
            table = COLLECTION_TABLE if channel == "collection" else SPACE_COLLECTION_TABLE
            columns = [value.strip() for value in selector.split(",") if value.strip()]
            for column in columns:
                _merge(result, self._collection_group(table, column, ()), Fraction(1, len(columns)))
            return result
        name, _, header = selector.partition(":")
        root = f"loot/loot_types/{name}.tab"
        groups = tuple(self.column(root, header or "strItems"))
        if channel == "ground":
            return self._choose(groups, self._ground_group)
        if channel == "required" and groups:
            # Every authored required row is attempted independently. If
            # several required groups can select the same item, take their union.
            result = {}
            for entry in self.column(root, "strRequiredItems"):
                for key, (chance, sources) in self._resolve(entry.value).items():
                    before, evidence = result.get(key, (Fraction(0), set()))
                    result[key] = 1 - (1 - before) * (1 - chance), evidence | sources | {_key(entry.source)}
            return result
        return {}


def _threshold(row, key, fallback="0"):
    value = number(row.get(key) or fallback)
    return value if isinstance(value, (int, float)) and math.isfinite(value) else 0


def _default_fields(files, path):
    if not files.exists(path):
        return {}
    lines = files.text(path).splitlines()
    return {key: spec.partition("[")[2].rstrip("]") for key, spec in zip(lines[0].split("\t"), lines[1].split("\t"))
            if spec.startswith(("i[", "f["))}


def _script_enzyme_gate(files):
    path = SERVER.parent / "script/library/loot.java"
    match = re.search(r"\bBASE_CHANCE_FOR_ENZYME\s*=\s*(\d+)\s*;", files.text(path)) if files.exists(path) else None
    return gate_probability(int(match[1]), 100) if match else None


def index_drops(db, files, items, npcs=None, spawn_index=None):
    db.executescript(DROP_SCHEMA)
    tables = DropTables(files)
    pools = set()
    enzyme_gate = _script_enzyme_gate(files)
    seen = set()

    def add(npc, row, channel, selector, rolls, gate, rule):
        distribution = tables.distribution(channel, selector)
        if not distribution:
            return
        pool = channel + "/" + selector
        if pool not in pools:
            normalized = {}
            for (item, level), (chance, proofs) in distribution.items():
                item_id = items.resolve_item(item)
                if item_id and item_id in items.records:
                    item = item_id
                previous, evidence = normalized.get((item, level), (0, set()))
                normalized[item, level] = previous + chance, evidence | proofs
            db.executemany("INSERT INTO loot_pool VALUES (?,?,?,?,?)", (
                (pool, item, level, float(chance), _packed({"selector": selector,
                 "sources": [{"path": path, "line": line} for path, line in sorted(proofs)]}))
                for (item, level), (chance, proofs) in normalized.items()))
            pools.add(pool)
        db.execute("INSERT INTO npc_drop_profile VALUES (?,?,?,?,?,?)", (
            npc, channel, pool, rolls, gate, _packed({"profile": row.source, "rule": rule})))

    for space, filename, key in ((False, "mob/creatures.tab", "creatureName"),
                                 (True, "space_mobile/space_mobile.tab", "strIndex")):
        path = SERVER / filename
        if not files.exists(path):
            continue
        defaults = _default_fields(files, path)
        for row in files.tab(path):
            name = row.get(key)
            ident = ("space/" if space else "") + name
            if not name or ident in seen:
                continue
            seen.add(ident)
            selector = row.get("strLootLookup" if space else "lootTable")
            roll_field, percent_field = ("intRolls", "fltItemChance") if space else ("intLootRolls", "intRollPercent")
            rolls = int(max(0, _threshold(row, roll_field, defaults.get(roll_field, "0"))))
            threshold = _threshold(row, percent_field, defaults.get(percent_field, "0"))
            if space:
                threshold = _float32(_float32(threshold) * 100)
            gate = gate_probability(threshold, 100 if space else 99)
            if selector:
                add(ident, row, "space" if space else "ground", selector, rolls, gate,
                    _proof("space_create", 106) if space else _proof("create", 905))
                if not space:
                    add(ident, row, "required", selector, 1, 1, _proof("loot", 1629))
            collection = row.get("collectionLoot").strip('"')
            if collection and collection != "no_loot":
                add(ident, row, "space_collection" if space else "collection", collection, 1,
                    max(0, min(100, _threshold(row, "collectionRoll", defaults.get("collectionRoll", "0")))) / 100,
                    _proof("space_combat", 1375) if space else _proof("loot", 1840))
            if not space and enzyme_gate is not None and row.get("niche").lower() in {"monster", "herbivore", "carnivore", "predator"}:
                add(ident, row, "enzyme", "beast", 1, enzyme_gate, _proof("loot", 1796))
    from catalog_loot_sources import index_loot_sources
    extra_sources = index_loot_sources(db, tables, items)
    tables.distribution.cache_clear()
    tables._defaults.cache_clear()
    tables._dynamic.cache_clear()
    if npcs is not None:
        from catalog_script_drops import scripted_drops
        for npc, item, script, chance, proof, binding, condition in scripted_drops(files, items, npcs, spawn_index):
            pool = f"script/{script}/{proof['line']}/{item}"
            if pool not in pools:
                db.execute("INSERT INTO loot_pool VALUES (?,?,?,?,?)",
                           (pool, item, 0, chance or 0, _packed({"selector": script, "sources": [proof]})))
                pools.add(pool)
            db.execute("INSERT OR IGNORE INTO npc_drop_profile VALUES (?,?,?,?,?,?)",
                       (npc, "script", pool, 1, 1, _packed({"profile": binding, "condition": condition,
                                                          "conditional_probability": chance, "rule": proof})))
    return {"pools": len(pools), "creatures": db.execute("SELECT COUNT(DISTINCT npc) FROM npc_drop_profile").fetchone()[0],
            "other_source_items": extra_sources}


CHANNELS = {
    "ground": "Standard loot table", "space": "Space loot table", "required": "Required loot entry",
    "collection": "Collection loot", "space_collection": "Space collection loot", "enzyme": "Beast enzyme",
    "script": "Scripted corpse loot",
}


def _hit(probability, count):
    if probability <= 0 or count <= 0:
        return 0
    if probability >= 1:
        return 1
    return -math.expm1(count * math.log1p(-probability))


def has_item_drops(db, ident):
    return db.execute("""
        SELECT 1 FROM loot_pool JOIN npc_drop_profile USING(pool) WHERE item=?
        UNION ALL
        SELECT 1 FROM item_loot_source WHERE item=? LIMIT 1
    """, (ident, ident)).fetchone() is not None


def _drop_rows(db, ident, *, by_item=False):
    # Both views read the same pools and profile rolls. For the item view,
    # limit the join before expanding the NPC profiles.
    subject = "npc.id,npc.name,npc.category" if by_item else "pool.item,item.name,NULL"
    predicate = "pool.item=?" if by_item else "profile.npc=?"
    rows = db.execute(f"""
        SELECT {subject},npc.level,profile.channel,profile.pool,profile.rolls,profile.gate,profile.proof,
               pool.level_gate,pool.chance,pool.data
        FROM loot_pool pool JOIN npc_drop_profile profile USING (pool)
        JOIN entity npc ON npc.kind='npcs' AND npc.id=profile.npc
        LEFT JOIN entity item ON item.kind='items' AND item.id=pool.item
        WHERE {predicate}
    """, (ident,))
    entries = {}
    grouped = defaultdict(list)
    for target, name, category, level, channel, pool, rolls, gate, proof, level_gate, probability, evidence in rows:
        entry = entries.setdefault(target, {"id": target, "name": name, "details": [], "chance": 0, "qualified": False})
        if by_item:
            entry.update(level=level, category=category)
        evidence = json.loads(evidence)
        eligible = not level_gate or level is not None and level >= level_gate
        contribution = {"channel": CHANNELS[channel], "selector": evidence["selector"], "table_chance": probability,
                        "rolls": rolls, "gate": gate, "minimum_level": level_gate or None, "base_level": level,
                        "sources": evidence["sources"], **json.loads(proof)}
        entry["details"].append(contribution)
        if channel == "script":
            contribution["table_chance"] = None
        else:
            grouped[target, pool].append((probability, eligible, rolls, gate))
        entry["qualified"] |= not eligible or not name
    for (target, _), chances in grouped.items():
        active = [chance for chance in chances if chance[1]]
        if not active:
            continue
        _, _, rolls, gate = active[0]
        p = min(1, sum(chance[0] for chance in active))
        hit = _hit(gate * p, rolls)
        entry = entries[target]
        entry["chance"] += (1 - entry["chance"]) * hit
    for entry in entries.values():
        if not entry["name"]:
            entry["chance"] = None
        if all(detail.get("condition") for detail in entry["details"]):
            entry["chance"] = None
        entry["details"].sort(key=lambda data: (data["channel"], data["minimum_level"] or 0, data["selector"]))
    return entries.values()


def _drop_page(entries, query, page, size, sort):
    terms = query.casefold().split()[:10]
    entries = [entry for entry in entries if all(term in
               (entry["id"] + " " + (entry["name"] or "")).casefold() for term in terms)]
    descending = sort.endswith("-desc")
    column = sort.removesuffix("-desc")
    entries.sort(key=lambda entry: entry["id"])
    entries.sort(key=lambda entry: (entry["name"] or entry["id"]).casefold(), reverse=sort == "name-desc")
    if column != "name":
        direction = -1 if descending else 1
        entries.sort(key=lambda entry: (entry.get(column) is None, direction * (entry.get(column) or 0)))
    pages = max(1, (len(entries) + size - 1) // size)
    page = min(page, pages)
    return {"total": len(entries), "page": page, "size": size, "pages": pages, "rows": entries[(page-1)*size:page*size]}


def creature_drops(db, ident, query, page, size, sort="chance-desc"):
    return _drop_page(_drop_rows(db, ident), query, page, size, sort)


def summarize_drop_chances(rows):
    positive = [row["chance"] for row in rows if row["chance"] is not None and row["chance"] > 0]
    highest = max(positive, default=None)
    return {"highest_chance": highest, "ranked_npcs": len(positive),
            "tied_npcs": sum(math.isclose(chance, highest, rel_tol=1e-12) for chance in positive),
            "unranked_npcs": sum(row["chance"] is None for row in rows),
            "zero_chance_npcs": sum(row["chance"] == 0 for row in rows)}


def item_drops(db, ident, query, page, size, sort="chance-desc", *, summarize=False):
    rows = (row for row in _drop_rows(db, ident, by_item=True) if row["chance"] != 0 or row["qualified"])
    if summarize:
        rows = list(rows)
    result = _drop_page(rows, query, page, size, sort)
    if summarize:
        result["summary"] = summarize_drop_chances(rows)
    result["other_sources"] = [json.loads(raw) for raw, in db.execute(
        "SELECT data FROM item_loot_source WHERE item=?", (ident,)
    )]
    result["other_sources"].sort(key=lambda entry: (entry["name"], entry.get("level") or 0, entry["order"]))
    return result
