"""Agent-readable context and exact lookups over a generated Archive catalog."""

import argparse
from contextlib import closing, contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
from tempfile import NamedTemporaryFile
from urllib.parse import quote, urlencode
from zipfile import ZIP_DEFLATED, ZipFile

from archive_config import DEFAULT_CONFIG, PROJECT, load_profile
from catalog_db import Catalog, KINDS, packed
from catalog_drops import summarize_drop_chances


FORMAT = 1
MANIFEST_ROUTE = "/api/agent/manifest"
LOOT_ROUTE = "/api/agent/loot"
MAX_CANDIDATES = 32
MAX_DROPS = 20
LOOT_BASIS = ("Chance of at least one copy per eligible defeat at the NPC's authored base level "
              "under standard server settings. Rates that depend on additional scripted conditions "
              "are recorded separately.")
ENTITY_CONTENTS = {
    "quests": "Quest givers, prerequisites, objectives, rewards, quest chains, and dialogue references.",
    "npcs": "Creature and character profiles, base levels, equipment, spawn sources, and model references.",
    "locations": "Worlds, regions, towns, buildings, interior cells, map bounds, and location descriptions.",
    "items": "Item IDs, templates, acquisition sources, storage, and model references.",
    "schematics": "Crafting outputs, ingredients, requirements, yields, and experimental stats.",
    "resources": "Resource classes, quality ranges, world distribution, and icon references.",
    "ships": "Player ship variants, component slots, deeds, and model references.",
    "conversations": "Original string codes, resolved English lines, script captions, and source records.",
}

# Expand the catalog's JSON columns without including any executable or native assets.
EXPORT_TABLES = (
    ("link", ("detail",), "Typed links between (kind, id) and (target_kind, target_id), with evidence."),
    ("scene_filter", (), "Scene membership for catalog records."),
    ("placement", ("data", "marker"), "Authored object and building placements and map destinations."),
    ("npc_spawn_placement", (), "NPC placement IDs, spawner types, and whether coordinates are resolved."),
    ("spawn_audit", ("data",), "Spawn expressions the parser could not resolve."),
    ("loot_pool", ("data",), "Item probabilities per pool roll, with table evidence and minimum level."),
    ("npc_drop_profile", ("proof",), "NPC loot pools, roll counts, gates, and conditional rules."),
    ("npc_loot_table", ("proof",), "Source datatable columns and loot selectors for each NPC."),
    ("item_loot_source", ("data",), "Non-NPC loot sources, including conditional and encounter sources."),
    ("map_marker", ("data",), "Map markers, coordinates, and references to their catalog records."),
    ("map_object_area", ("data",), "Map extents and geometry for regions, no-build zones, and buildings."),
    ("source_path", (), "Logical source paths mapped to paths in the configured GitHub repository."),
)


def snapshot(metadata):
    return {"source_revision": metadata["revision"], "catalog_schema": metadata["schema"],
            "source_key": metadata.get("source_key"), "repositories": metadata.get("sources", {})}


def entity_url(kind, ident):
    return f"/{kind}/{quote(ident, safe='')}"


def agent_manifest(metadata):
    return {
        "format": FORMAT,
        "title": metadata.get("title", "Star Wars Galaxies Galactic Archive"),
        "snapshot": snapshot(metadata),
        "counts": {kind: metadata["counts"].get(kind, 0) for kind in KINDS},
        "collections": {kind: {"description": description, "browse": f"/api/list?type={kind}",
                               "detail": f"/api/entity?type={kind}&id={{id}}"}
                        for kind, description in ENTITY_CONTENTS.items()},
        "routes": {
            "search": "/api/search?q={query}",
            "related": "/api/related?type={kind}&id={id}&role={role}&page={page}&size={size}",
            "placements": "/api/placements?type={kind}&id={id}&scene={scene}&page={page}",
            "placement": "/api/placement?id={id}",
            "dialogue": "/api/dialogue?table={id}&page={page}&size={size}",
            "item_drop_ranking": LOOT_ROUTE + "?q={name}",
            "exact_item_drop_ranking": LOOT_ROUTE + "?item={id}",
            "item_drop_evidence": "/api/item-loot-npcs?type=items&id={id}&sort=chance-desc&page={page}",
            "npc_drops": "/api/npc-drops?type=npcs&id={id}&sort=chance-desc&page={page}",
            "map": "/api/map/{map_id}",
            "map_image": "/api/map/{map_id}/image",
            "map_markers": "/api/map/{map_id}/markers?layers={layers}&areas={areas}&bounds={bounds}",
            "npc_model": "/api/model?npc={id}",
            "ship_model": "/api/model?ship={id}",
            "item_model": "/api/model?item={id}",
            "building_model": "/api/model?building={id}",
            "resource_image": "/api/resource-image?class={id}",
            "source": "/source?path={path}&line={line}",
            "catalog_metadata": "/api/meta",
        },
        "loot": {"basis": LOOT_BASIS, "chance_unit": "fraction from 0 to 1",
                 "chance_percent_unit": "percentage from 0 to 100", "max_compared_items": MAX_CANDIDATES,
                 "max_rows_per_item": MAX_DROPS},
    }


def agent_guide(metadata, *, files=None):
    title = metadata.get("title", "Star Wars Galaxies Galactic Archive")
    introduction = f"""# {title}

> The configured Star Wars Galaxies catalog covers quests, characters, locations, items, schematics, resources, ships, and dialogue.

Game source revision `{metadata["revision"]}`.

Use each record's stable ID and source evidence in answers. Different records can share a display name. Follow a location's alias to its canonical ID before counting places. An empty result means the Archive did not recover a matching record from this snapshot.
"""
    if files is not None:
        return introduction + """
Start with `manifest.json` for the file list and `names.jsonl` for names and stable IDs. The full entry is in the matching collection under `entities/`. Follow relationships in `link.jsonl` using `(kind, id)` and `(target_kind, target_id)`. Use `placement.jsonl` for object positions and join `npc_spawn_placement.jsonl` to it by placement ID for NPC positions. Dialogue entries contain their original string codes, English lines, and script captions. The map tables identify the scene, layer, and geometry of each feature.

With the generated SQLite catalog, the local `catalog_agent.py` command can search names, open records, page through links and placements, and filter dialogue. The `loot` subcommand ranks NPCs by item ID. To calculate a standard NPC drop rate from these files, filter `loot_pool` by item ID, join to `npc_drop_profile` by pool, and use each NPC's base level from `entities/npcs.jsonl`. For each NPC and pool, sum the per-roll fractions whose level gate is satisfied and cap the sum at one. The probability of at least one drop is `1 - (1 - gate * probability) ** rolls` for that pool. Combine pools with one minus the product of their miss probabilities. Report script channels and non-NPC loot sources with their own conditions.

## Catalog files

- [Manifest](manifest.json)
""" + "".join(f"- [{record['label']}]({path})\n" for path, record in files.items())

    return introduction + f"""
Find an ID by name with `/api/search?q=...`, or browse and filter a collection with `/api/list`. Open the ID in `/api/entity` for its authored detail. The entity's `related` keys name the roles you can page through using `/api/related`. Use those links to trace prerequisites and rewards, recipes and ingredients, location hierarchies, or NPCs and their quests.

Use `/api/placements` for NPC, item, or building positions, with an optional scene filter. Use `/api/dialogue` for the text and captions behind a conversation ID. Map routes provide extents and marker layers, and the asset routes provide map images, resource icons, and model previews.

{LOOT_BASIS} Pass an item name as `q` or a stable item ID as `item` to `{LOOT_ROUTE}`. `chance` is a fraction and `chance_percent` is a percentage. `highest` compares items in `selection`. Cite the variant ID when the selection is `related_variants`. `tied_npcs` counts all NPCs tied at the highest rate, even when `size` limits the returned rows. Refine the name or pass an ID if the result says `refine_query`.

## Catalog routes

- [Machine-readable route manifest]({MANIFEST_ROUTE})
- [Quests](/api/list?type=quests)
- [NPCs](/api/list?type=npcs)
- [Locations](/api/list?type=locations)
- [Items](/api/list?type=items)
- [Schematics](/api/list?type=schematics)
- [Resources](/api/list?type=resources)
- [Ships](/api/list?type=ships)
- [Dialogue](/api/list?type=conversations)
- [Catalog metadata](/api/meta)
"""


def matching_items(db, query, item_id):
    """Resolve a stable ID first, then compare names with or without a leading article."""
    exact = db.execute("SELECT id,name FROM entity WHERE kind='items' AND id=?", (item_id or query,)).fetchone()
    if exact or item_id:
        return "item_id", [exact] if exact else [], ""
    title = re.sub(r"^(?:a|an|the)\s+", "", query, flags=re.I).lower()
    rows = db.execute("""
        SELECT id,name FROM entity WHERE kind='items' AND name COLLATE NOCASE IN (?, ?, ?, ?)
        ORDER BY name COLLATE NOCASE,id
    """, item_labels(title)).fetchall()
    return "exact_name", rows, title


def item_labels(title):
    return (title, "a " + title, "an " + title, "the " + title)


def related_drop_items(db, title):
    return db.execute("""
        SELECT id,name FROM entity
        WHERE kind='items' AND instr(lower(name),?)>0 AND name COLLATE NOCASE NOT IN (?, ?, ?, ?)
        AND EXISTS (SELECT 1 FROM loot_pool JOIN npc_drop_profile USING(pool) WHERE item=entity.id)
        ORDER BY name COLLATE NOCASE,id
    """, (title, *item_labels(title))).fetchall()


def candidate(item):
    ident, name = item
    query = urlencode({"type": "items", "id": ident, "sort": "chance-desc"})
    return {"id": ident, "name": name, "url": entity_url("items", ident),
            "evidence": "/api/item-loot-npcs?" + query}


def rank_item(catalog, item, limit):
    ident, _ = item
    result = catalog.item_loot_npcs(ident, size=limit, summarize=True)
    stats = result.get("summary") or summarize_drop_chances(())
    drops = [{"id": row["id"], "name": row["name"], "level": row["level"], "chance": row["chance"],
              "chance_percent": row["chance"] * 100, "url": entity_url("npcs", row["id"])}
             for row in result["rows"] if row["chance"] is not None and row["chance"] > 0]
    return {**candidate(item), "ranking": {**stats, "npc_sources": result["total"],
            "other_sources": len(result["other_sources"]), "rows": drops}}


def loot_ranking(catalog, *, query="", item_id="", limit=3, metadata=None):
    query, item_id = " ".join(query.split()), item_id.strip()
    if bool(query) == bool(item_id):
        raise ValueError("Supply one item name or item ID.")
    if not 1 <= limit <= MAX_DROPS:
        raise ValueError(f"Return between 1 and {MAX_DROPS} drops per item.")
    with catalog.connect() as db:
        selection, exact, title = matching_items(db, query, item_id)

    result = {"query": item_id or query, "selection": selection, "status": "complete", "highest": [],
              "basis": LOOT_BASIS, "snapshot": snapshot(metadata or catalog.metadata()), "rows_per_item": limit,
              "exact_count": len(exact), "related_count": 0, "exact_items": [], "related_items": []}
    if len(exact) > MAX_CANDIDATES:
        result.update(status="refine_query", exact_items=[candidate(item) for item in exact[:MAX_CANDIDATES]])
        return result

    result["exact_items"] = [rank_item(catalog, item, limit) for item in exact]
    ranked = [item for item in result["exact_items"] if item["ranking"]["rows"]]
    variants = []
    if not ranked and title and catalog.has_npc_drops:
        with catalog.connect() as db:
            variants = related_drop_items(db, title)
    if variants:
        result.update(selection="related_variants", related_count=len(variants))
        if len(variants) > MAX_CANDIDATES:
            result.update(status="refine_query",
                          related_items=[candidate(item) for item in variants[:MAX_CANDIDATES]])
            return result
        result["related_items"] = [rank_item(catalog, item, limit) for item in variants]
        ranked = [item for item in result["related_items"] if item["ranking"]["rows"]]
    elif not exact:
        result["selection"] = "no_match"

    if ranked:
        largest = max(item["ranking"]["highest_chance"] for item in ranked)
        result["highest"] = [
            {"item_id": item["id"], "item_name": item["name"], "item_url": item["url"],
             "tied_npcs": item["ranking"]["tied_npcs"], "evidence": item["evidence"],
             "npc": item["ranking"]["rows"][0]}
            for item in ranked if math.isclose(item["ranking"]["highest_chance"], largest, rel_tol=1e-12)
        ]
    return result


@contextmanager
def staged_file(output):
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output.parent, prefix="agent-", suffix=".pending", delete=False) as stream:
        pending = Path(stream.name)
    try:
        yield pending
        pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)


def write_jsonl(bundle, filename, cursor, json_columns=(), *, with_url=False):
    names = [column[0] for column in cursor.description]
    total = 0
    with bundle.open(filename, "w", force_zip64=True) as stream:
        for values in cursor:
            record = dict(zip(names, values))
            for column in json_columns:
                if record[column] is not None:
                    record[column] = json.loads(record[column])
            if with_url:
                record["url"] = entity_url(record["kind"], record["id"])
            stream.write((packed(record) + "\n").encode("utf-8"))
            total += 1
    return total


def export_catalog(catalog, output, *, progress=None):
    """Export normalized records without expanding every NPC against every loot pool."""
    output = output.resolve()
    if output == catalog.path:
        raise ValueError("Choose a ZIP path separate from the catalog database.")
    files = {}
    report = progress or (lambda filename: None)
    with catalog.connect() as db, staged_file(output) as pending:
        db.execute("BEGIN")
        metadata = json.loads(db.execute("SELECT data FROM metadata WHERE key='catalog'").fetchone()[0])
        tables = {name for name, in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        with ZipFile(pending, "w", compression=ZIP_DEFLATED, compresslevel=4) as bundle:
            bundle.writestr("metadata.json", packed(metadata))
            files["metadata.json"] = {"label": "Catalog metadata", "records": 1,
                                      "description": "Source revisions, maps, resource icons, and coverage."}
            report("names.jsonl")
            with closing(db.execute("""
                SELECT kind,id,name,category,scene,level,json_extract(card,'$.alias_for') AS alias_for
                FROM entity ORDER BY kind,id
            """)) as names:
                count = write_jsonl(bundle, "names.jsonl", names, with_url=True)
            files["names.jsonl"] = {"label": "Entity name index", "records": count,
                                    "description": "Names, stable IDs and location aliases for all collections."}

            for kind, description in ENTITY_CONTENTS.items():
                filename = f"entities/{kind}.jsonl"
                report(filename)
                with closing(db.execute("SELECT * FROM entity WHERE kind=? ORDER BY id", (kind,))) as cursor:
                    count = write_jsonl(bundle, filename, cursor, ("card", "data"), with_url=True)
                files[filename] = {"label": kind.capitalize(), "records": count, "description": description}
            for table, json_columns, description in EXPORT_TABLES:
                if table not in tables:
                    continue
                filename = f"{table}.jsonl"
                report(filename)
                # The table names are defined in EXPORT_TABLES.
                with closing(db.execute(f"SELECT * FROM {table}")) as cursor:
                    count = write_jsonl(bundle, filename, cursor, json_columns)
                files[filename] = {"label": table.replace("_", " ").capitalize(), "records": count,
                                   "description": description}

            manifest = {**agent_manifest(metadata), "file_format": "UTF-8 JSON Lines and JSON", "files": files,
                        "unavailable_tables": [name for name, _, _ in EXPORT_TABLES if name not in tables]}
            bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            bundle.writestr("llms.txt", agent_guide(metadata, files=files))
    return {"output": str(output), "bytes": output.stat().st_size, "snapshot": manifest["snapshot"],
            "files": {filename: record["records"] for filename, record in files.items()}}


def open_catalog(args):
    if args.database:
        database, profile = args.database, None
    elif config := args.sources or (DEFAULT_CONFIG if DEFAULT_CONFIG.is_file() else None):
        profile = load_profile(config)
        database = profile.database
    else:
        database, profile = PROJECT / ".local/catalog.sqlite3", None
    if not database.is_file():
        raise ValueError("Generate the catalog first or pass the generated SQLite file with --database.")
    catalog = Catalog(database)
    if profile and catalog.metadata().get("source_key") != profile.key:
        raise ValueError("Rebuild the catalog for the selected source configuration.")
    return catalog


def pagination(command):
    command.add_argument("--page", type=int, default=1)
    command.add_argument("--size", type=int, default=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--sources", type=Path, help="Use a configured Archive catalog.")
    source.add_argument("--database", type=Path, help="Read a generated SQLite catalog directly.")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Write the catalog context as a JSONL ZIP.")
    export.add_argument("--output", type=Path)
    loot = commands.add_parser("loot", help="Rank modeled NPC drops by item name or exact ID.")
    loot.add_argument("query", nargs="?", default="")
    loot.add_argument("--item", default="", help="Look up a stable item ID with no name matching.")
    loot.add_argument("--size", type=int, default=3)
    loot.add_argument("--output", type=Path, help="Also save the JSON answer to this file.")
    search = commands.add_parser("search", help="Search all catalog collections for an ID or name.")
    search.add_argument("query")
    entity = commands.add_parser("entity", help="Read one catalog detail record.")
    entity.add_argument("kind", choices=KINDS)
    entity.add_argument("id")
    browse = commands.add_parser("list", help="Browse and filter a collection.")
    browse.add_argument("kind", choices=KINDS)
    browse.add_argument("--query", default="")
    browse.add_argument("--scene", default="")
    browse.add_argument("--category", default="")
    pagination(browse)
    related = commands.add_parser("related", help="Page through one entity's related records.")
    related.add_argument("kind", choices=KINDS)
    related.add_argument("id")
    related.add_argument("role", help="A relationship key from the entity response.")
    pagination(related)
    positions = commands.add_parser("placements", help="Read NPC, item, or building placements.")
    positions.add_argument("kind", choices=("npcs", "items", "locations"))
    positions.add_argument("id")
    positions.add_argument("--scene", default="")
    pagination(positions)
    dialogue = commands.add_parser("dialogue", help="Read or search a conversation's lines.")
    dialogue.add_argument("id")
    dialogue.add_argument("--query", default="")
    pagination(dialogue)
    args = parser.parse_args()

    if hasattr(args, "page") and (args.page < 1 or args.size < 1):
        parser.error("Page and size must be positive.")

    try:
        catalog = open_catalog(args)
        if args.command == "export":
            output = args.output or (catalog.path.parent / "agent-data.zip")
            if output.suffix.lower() != ".zip":
                parser.error("Export output must be a ZIP file.")
            result = export_catalog(catalog, output,
                                    progress=lambda name: print(f"Exporting {name}", file=sys.stderr, flush=True))
        elif args.command == "loot":
            result = loot_ranking(catalog, query=args.query, item_id=args.item, limit=args.size)
            if args.output:
                if args.output.suffix.lower() != ".json" or args.output.resolve() == catalog.path:
                    parser.error("Choose a JSON output path separate from the catalog database.")
                with staged_file(args.output) as pending:
                    pending.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif args.command == "search":
            result = catalog.search(args.query)
        elif args.command == "list":
            result = catalog.list(args.kind, query=args.query, scene=args.scene, category=args.category,
                                  page=args.page, size=args.size)
        elif args.command == "related":
            result = catalog.links(args.kind, args.id, args.role, args.page, args.size)
        elif args.command == "placements":
            result = catalog.placements(args.kind, args.id, scene=args.scene, page=args.page, size=args.size)
        elif args.command == "dialogue":
            result = catalog.dialogue(args.id, args.query, "", args.page, args.size)
        else:
            result = catalog.entity(args.kind, args.id, include_dialogue=True)
        if result is None:
            parser.error("No catalog record found for that ID.")
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
