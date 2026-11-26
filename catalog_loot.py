"""Resolve the authored ground and space loot selectors used by NPCs."""

from dataclasses import dataclass
import json
from pathlib import PurePosixPath

from catalog_sources import SERVER


NPC_LOOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS npc_loot_table (
    path TEXT NOT NULL, column_name TEXT NOT NULL, npc_id TEXT NOT NULL,
    selector TEXT NOT NULL, proof TEXT NOT NULL,
    PRIMARY KEY (path, column_name, npc_id, selector)
) WITHOUT ROWID
"""
SPACE_LOOT = "space_loot/loot_items.tab"


@dataclass(frozen=True)
class LootEntry:
    value: str
    source: dict
    column: str
    required: bool = False


class LootTables:
    def __init__(self, files):
        self.files = files
        self.tables = {}

    def column(self, relative, column, *, required=False):
        name = PurePosixPath(relative)
        if name.is_absolute() or ".." in name.parts:
            return
        if relative not in self.tables:
            path = SERVER.joinpath(*name.parts)
            self.tables[relative] = self.files.tab(path) if self.files.exists(path) else ()
        for row in self.tables[relative]:
            if value := row.get(column):
                yield LootEntry(value, row.source, column, required)

    def nested_items(self, reference, ancestors=()):
        table, separator, column = reference.partition(":")
        key = (table, column)
        if not separator or key in ancestors or len(ancestors) >= 8:
            return
        for entry in self.column(f"loot/{table}.tab", column):
            yield from self.expand(entry, (*ancestors, key))

    def expand(self, entry, ancestors=()):
        if entry.value.startswith("loot_items/"):
            yield from self.nested_items(entry.value, ancestors)
        else:
            yield entry

    def ground_items(self, selector):
        # loot.makeLootInContainer selects loot_types and then the named item column.
        name, _, column = selector.partition(":")
        root = f"loot/loot_types/{name}.tab"
        groups = list(self.column(root, column or "strItems"))
        for group in groups:
            table, _, item_column = group.value.partition(":")
            for entry in self.column(f"loot/loot_items/{table}.tab", item_column or "strItemType"):
                yield from self.expand(entry)
        if groups:
            for entry in self.column(root, "strRequiredItems", required=True):
                yield from self.expand(entry)

    def space_columns(self, selector):
        # space_combat.createLoot selects a column of loot_lookup, then loot_items.
        for column in sorted({row.value for row in self.column("space_loot/loot_lookup.tab", selector)}):
            if entry := next(self.column(SPACE_LOOT, column), None):
                yield entry.source["path"], entry.column


def index_npc_loot(db, files):
    db.execute(NPC_LOOT_SCHEMA)
    db.execute("DELETE FROM npc_loot_table")
    npcs = {row[0] for row in db.execute("SELECT id FROM entity WHERE kind='npcs'")}
    loot = LootTables(files)
    ground_columns = {}
    space_columns = {}
    for filename, key, field, prefix in (
        ("mob/creatures.tab", "creatureName", "lootTable", ""),
        ("space_mobile/space_mobile.tab", "strIndex", "strLootLookup", "space/"),
    ):
        path = SERVER / filename
        if not files.exists(path):
            continue
        seen = set()
        for row in files.tab(path):
            ident = prefix + row.get(key)
            if ident in seen or ident not in npcs:
                continue
            seen.add(ident)
            selector = row.get(field)
            if not selector:
                continue
            if prefix:
                if selector not in space_columns:
                    space_columns[selector] = tuple(loot.space_columns(selector))
                matches = space_columns[selector]
            else:
                if selector not in ground_columns:
                    ground_columns[selector] = {(entry.source["path"], entry.column)
                                                for entry in loot.ground_items(selector)}
                matches = sorted(ground_columns[selector])
            proof = json.dumps(row.source, separators=(",", ":"))
            db.executemany("INSERT INTO npc_loot_table VALUES (?,?,?,?,?)",
                           ((table, column, ident, selector, proof) for table, column in matches))
    return db.execute("SELECT COUNT(*), COUNT(DISTINCT npc_id) FROM npc_loot_table").fetchone()
