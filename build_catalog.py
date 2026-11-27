"""Build the local quest atlas from the pinned manifest and game inputs."""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import tempfile

from archive_config import SourceRepository, add_source_options, select_profile
from archive_quests.manifest import manifest as generate_manifest

from build_map_features import build_map_features
from catalog_db import SCHEMA, Catalog, Writer
from catalog_drops import index_drops
from catalog_loot import index_npc_loot
from catalog_dialogue import DialogueIndex
from catalog_entities import Items, item_ref, load_npcs, load_places, summarize_npc
from catalog_inventory import publish_inventory
from catalog_location_hierarchy import LOCATION_ALIASES, authored_ground_worlds, canonical_location, publish_location_hierarchy
from catalog_locations import LocationIndex
from catalog_lore import add_location_descriptions, load_client_landmarks
from catalog_map import LAYERS
from catalog_models import ModelLibrary
from catalog_npc_models import NpcModels
from catalog_spawns import SpawnIndex
from catalog_sources import (
    GAME_MODEL_PATHS, PLANETS, ROOT, SERVER, ClientStrings, GameFiles, display_id,
    distinct, read_iff_table, string_ref,
)


FAMILIES = {
    "ground": "Ground journal", "space": "Space mission", "legacy_static": "Static NPC",
    "legacy_theme_park": "Theme park", "legacy_epic": "Epic NPC", "legacy_random": "Random NPC",
    "legacy_journal": "Legacy journal", "scripted": "Scripted",
}
SERIES_LINKS = {"requires_completion", "item_start_requires_completion", "legacy_chain_step",
                "legacy_completion_grant", "script_route_requires_completion",
                "task_completion_grant", "space_split", "quest_path_grant"}
SCENE_ID = re.compile(r"[a-z][a-z0-9_]*")


def previous_quests(quest):
    return [link["id"] for link in quest["predecessors"]
            if link["kind"] in SERIES_LINKS and link["target_defined"] and link["id"] != quest["id"]
            and link.get("trigger_condition") not in ("failure", "2")]


def quest_identity_aliases(npcs):
    result = defaultdict(set)
    for npc in npcs.values():
        for script in npc["scripts"]:
            if script.startswith("conversation."):
                result[script].add(npc["id"])
    return result


def row_localizations(record, row, strings, dialogue):
    for key, raw in row.cells.items():
        if raw and any(tag in key.upper() for tag in ("COMM_MESSAGE", "DIALOG", "SPEECH", "CONVERSATION", "PROSE")):
            if string_ref(raw):
                dialogue.direct(record, raw, row.source)
            elif raw.startswith(("conversation/", "spacequest/")) and ":" not in raw:
                dialogue.legacy_table(record, raw, row.source)


def quest_tasks(record, path, files, strings, dialogue):
    steps = []
    for index, row in enumerate(files.tab(path)):
        row_localizations(record["id"], row, strings, dialogue)
        attributes = {key: value for key, value in row.cells.items() if value and key not in (
            "JOURNAL_ENTRY_TITLE", "JOURNAL_ENTRY_DESCRIPTION", "ATTACH_SCRIPT", "TASK_NAME",
            "TASKS_ON_COMPLETE", "TASKS_ON_FAIL", "PREREQUISITE_TASKS", "EXCLUSION_TASKS", "IS_VISIBLE",
        )}
        steps.append({
            "index": index, "id": row.get("TASK_NAME") or str(index), "script": row.get("ATTACH_SCRIPT") or None,
            "title": strings.lookup(row.get("JOURNAL_ENTRY_TITLE")),
            "description": strings.lookup(row.get("JOURNAL_ENTRY_DESCRIPTION")),
            "visible": row.get("IS_VISIBLE", "1") != "0", "scene": row.get("PLANET_NAME") or None,
            "after": row.get("PREREQUISITE_TASKS"), "exclusions": row.get("EXCLUSION_TASKS"),
            "on_complete": row.get("TASKS_ON_COMPLETE"), "on_failure": row.get("TASKS_ON_FAIL"),
            "attributes": attributes, "source": row.source,
        })
    return steps


def legacy_descriptions(manifest, clientdata, strings):
    reference = manifest.get("legacy_quest_table")
    if not reference:
        return {}
    path = clientdata / reference["path"]
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != reference.get("sha256"):
        return {}
    _, _, rows = read_iff_table(path)
    return {"legacy/journal/" + row["NAME"]: strings.lookup(row["JOURNAL_ENTRY_DETAILED"]) for row in rows}


def collect_quest_detail(quest, files, strings, dialogue, legacy):
    ident = quest["id"]
    detail = {"description": legacy.get(ident, {}), "completion_summary": {}, "steps": [], "authored_mission": {}}
    for evidence in quest["sources"]:
        if evidence.get("scope") == "supplied_clientdata":
            continue
        path = ROOT / evidence["path"]
        if path.suffix != ".tab" or not files.exists(path):
            continue
        name = evidence["path"]
        if "/questtask/" in name:
            detail["steps"] = quest_tasks(quest, path, files, strings, dialogue)
            continue
        rows = files.tab(path)
        if not rows:
            continue
        row = rows[0]
        if "/questlist/" in name:
            detail["description"] = strings.lookup(row.get("JOURNAL_ENTRY_DESCRIPTION"))
            detail["completion_summary"] = strings.lookup(row.get("JOURNAL_ENTRY_COMPLETION_SUMMARY"))
            row_localizations(ident, row, strings, dialogue)
        elif "/spacequest/" in name:
            detail["authored_mission"] = {"source": row.source,
                                          "fields": {key: value for key, value in row.cells.items() if value}}
            row_localizations(ident, row, strings, dialogue)
        elif quest["family"].startswith("legacy_") and "line" in evidence:
            selected = next((value for value in rows if value.line == evidence["line"]), row)
            detail["authored_mission"] = {"source": selected.source,
                                          "fields": {key: value for key, value in selected.cells.items() if value}}
            if selected.get("convo"):
                dialogue.legacy_table(ident, selected.get("convo"), selected.source)
            elif quest["family"] == "legacy_epic":
                for start in quest["starts"]:
                    if start.get("conversation_table"):
                        dialogue.legacy_table(ident, start["conversation_table"], start["source"])
    return detail


def lookup_start(spatial, start, ident):
    scene = start.get("scene")
    match = None
    for evidence in (start.get("placement_source"), start.get("source")):
        if isinstance(evidence, dict):
            match = spatial.lookup(evidence, scene=scene, object_id=start.get("anchor_object_id"))
            if match and match.get("coordinates"):
                return dict(match)
    candidate = [point for point in spatial.by_mob.get(ident, ()) if point.get("scene") == scene]
    if len(candidate) == 1:
        return dict(candidate[0])
    if start.get("coordinate_frame") == "world" and start.get("position"):
        return {"scene": scene, "coordinates": [float(value) for value in start["position"]],
                "status": "authored_world", "source": start.get("source")}
    return dict(match) if match else None


def placement_key(entry):
    proof = entry.get("source") or {}
    if coordinates := entry.get("coordinates"):
        return entry.get("scene"), tuple(round(value, 5) if value is not None else None for value in coordinates)
    return entry.get("scene"), entry.get("object_id"), proof.get("path"), proof.get("line")


def consolidated_locations(entries):
    result = {}
    for entry in entries:
        marker = placement_key(entry)
        if marker not in result:
            result[marker] = dict(entry)
        else:
            first = result[marker]
            extra = entry.get("source")
            if extra and extra != first.get("source") and extra not in first.get("additional_sources", []):
                first.setdefault("additional_sources", []).append(extra)
    return sorted(result.values(), key=lambda point: (
        point.get("scene") or "", not bool(point.get("coordinates")), str(point.get("object_id") or ""),
    ))


def placement_areas(places):
    by_scene = defaultdict(list)
    for ident, place in places.items():
        boundary = place.get("boundary")
        if boundary and boundary.get("shape") == "circle" and len(boundary.get("values") or []) == 3:
            by_scene[place["scene"]].append((ident, boundary["values"]))
        elif bounds := place.get("bounds") or (boundary or {}).get("values"):
            if place["scene"] != ident and len(bounds) == 4:
                by_scene[place["scene"]].append((ident, bounds))
    return by_scene


def nearby_places(point, areas):
    position = point.get("coordinates")
    if not position:
        return
    x, _, z = position
    for ident, bounds in areas.get(point["scene"], ()):
        if len(bounds) == 3:
            center_x, center_z, radius = bounds
            contains = (x - center_x) ** 2 + (z - center_z) ** 2 <= radius ** 2
        else:
            minimum_x, minimum_z, maximum_x, maximum_z = bounds
            contains = minimum_x <= x <= maximum_x and minimum_z <= z <= maximum_z
        if contains:
            yield ident


def exact_item(writer, items, quest, value, relation, proof):
    ident = item_ref(value)
    if not ident:
        return
    items.ensure(ident)
    writer.link("quests", quest, relation, "items", ident, proof)
    writer.link("items", ident, relation + "_quests", "quests", quest, proof)


def collect_quests(writer, files, strings, spatial, manifest, dialogue, npcs, items, world, legacy):
    all_quests = {record["id"]: record for record in manifest["quests"]}
    parents = {ident: previous_quests(record) for ident, record in all_quests.items()}
    scripts_to_npcs = quest_identity_aliases(npcs)
    offers_by_npc = defaultdict(set)
    origin_count = 0
    quest_fields = []
    for ident, quest in all_quests.items():
        scenes = set()
        givers = []
        known_scripts = set()
        for start in quest["starts"]:
            scene = start.get("scene")
            token = start.get("npc") or {}
            npc_id = token.get("id")
            script = start.get("script")
            if script:
                known_scripts.add(script)
            candidates = scripts_to_npcs.get(script, ())
            if len(candidates) == 1 and (not npc_id or npc_id.startswith("conversation.")):
                npc_id = next(iter(candidates))
            if npc_id:
                placed = [point for point in spatial.by_mob.get(npc_id, ())
                          if point.get("coordinates") and point.get("scene")]
                placement_scenes = {point["scene"] for point in placed}
                if not scene and len(placement_scenes) == 1:
                    scene = next(iter(placement_scenes))
                    start["scene"] = scene
                    start["origin_basis"] = "authored_npc_placement"
                    start["origin_evidence"] = placed[0]["source"]
                    if scene in PLANETS:
                        start["planet"] = scene
                if npc_id not in npcs:
                    npcs[npc_id] = summarize_npc(npc_id, strings, "Quest contact", name=token.get("text"),
                                                 template=start.get("template"),
                                                 scripts=[script] if script else [],
                                                 sources=[start["source"]] if start.get("source") else [])
                npc = npcs[npc_id]
                offers_by_npc[npc_id].add(ident)
                givers.append(npc["name"])
                start["npc_id"] = npc_id
                writer.link("quests", ident, "quest_givers", "npcs", npc_id, start.get("source"))
                if point := lookup_start(spatial, start, npc_id):
                    start["placement"] = point
                    npc["locations"].append(point)
            if scene and SCENE_ID.fullmatch(scene):
                world(scene)
                scenes.add(scene)
            if start.get("planet"):
                scenes.add(start["planet"])
            if start.get("template") and start["kind"] == "item":
                exact_item(writer, items, ident, start["template"], "starter_items", start)
            if start.get("conversation_table"):
                dialogue.legacy_table(ident, start["conversation_table"], start["source"])
        for script in known_scripts:
            for table in sorted(dialogue.script_tables[script]):
                dialogue.link(ident, table, "quest-giver script", next(
                    start["source"] for start in quest["starts"] if start.get("script") == script
                ))
        for reward in quest["rewards"]:
            if reward["kind"] == "item":
                exact_item(writer, items, ident, reward.get("item_id"), "reward_items", reward)
        for rule in quest["requirements"]:
            if rule["kind"] == "possess_item":
                exact_item(writer, items, ident, rule.get("item_id"), "required_items", rule)
        for field in ("predecessors", "follow_ups"):
            for link in quest[field]:
                if link["target_defined"]:
                    writer.link("quests", ident, field, "quests", link["id"], link)
        quest["detail"] = collect_quest_detail(quest, files, strings, dialogue, legacy)
        for task in quest["detail"]["steps"]:
            for key in ("TARGET", "TARGET_SERVER_TEMPLATE", "ITEM_TEMPLATE", "QUEST_ITEM", "LOOT_ITEM_NAME"):
                if value := task["attributes"].get(key):
                    exact_item(writer, items, ident, value, "objective_items", task["source"])
        for objective in quest["objective_scenes"]:
            scene = objective["scene"]
            if SCENE_ID.fullmatch(scene):
                world(scene)
                writer.link("quests", ident, "objectives", "locations", scene, objective["source"])
        origin_count += bool(scenes)
        quest_fields.append((ident, quest, sorted(scenes), list(distinct(givers))))

    for npc, quests in offers_by_npc.items():
        for ident in sorted(quests):
            earlier = [other for other in parents[ident] if ident not in parents[other]]
            role = "continuation_quests" if earlier else "series_starts"
            writer.link("npcs", npc, role, "quests", ident, {"previous": earlier})

    for ident, quest, scenes, giver_names in quest_fields:
        for table, relation in sorted(dialogue.quest_tables[ident].items()):
            writer.link("quests", ident, "dialogue", "conversations", table, relation)
            writer.link("conversations", table, "quests", "quests", ident, relation)
        journal = quest.get("journal", {})
        family = FAMILIES.get(quest["family"], quest["family"])
        title = quest["title"]["text"] or display_id(ident)
        credits = sorted({entry["amount"] for entry in quest["rewards"] if entry["kind"] == "credits"
                          and entry.get("amount") and entry.get("mode") != "performance_rate"})
        experience = sorted({entry.get("base_amount") or entry.get("amount") for entry in quest["rewards"]
                             if entry["kind"] == "experience" and (entry.get("base_amount") or entry.get("amount"))})
        min_levels = [rule["amount"] for rule in quest["requirements"] if rule["kind"] == "minimum_level"
                      and "amount" in rule]
        if min_levels:
            quest["minimum_level"] = max(min_levels)
        category = journal.get("category", {}).get("text")
        primary = next((start.get("planet") or start.get("scene") for start in quest["starts"]
                        if start.get("planet") or start.get("scene")), None)
        writer.entity("quests", ident, title, quest, category=family, scene=primary, scenes=scenes,
                      level=journal.get("recommended_level"), status=quest["source_status"],
                      search=[category, *giver_names],
                      card={"chapter": category, "givers": giver_names, "min_level": quest.get("minimum_level"),
                            "reward_credits": credits, "reward_experience": experience,
                            "reward_items": sum(entry["kind"] == "item" for entry in quest["rewards"]),
                            "dialogues": len(dialogue.quest_tables[ident])})
    return origin_count


def publish_npcs(writer, dialogue, npcs, items, areas, world):
    for ident, npc in sorted(npcs.items()):
        scripts = sorted(set(npc["scripts"]))
        npc["scripts"] = scripts
        npc["locations"] = consolidated_locations(npc["locations"])
        scenes = sorted({point["scene"] for point in npc["locations"] if point.get("scene")})
        for scene in scenes:
            world(scene)
            writer.link("npcs", ident, "located_in", "locations", scene)
            writer.link("locations", scene, "npcs", "npcs", ident)
        for point in npc["locations"]:
            for area in nearby_places(point, areas):
                writer.link("npcs", ident, "placed_in", "locations", area, point.get("source"))
                writer.link("locations", area, "npcs", "npcs", ident, point.get("source"))
        for script in scripts:
            for table in sorted(dialogue.script_tables[script]):
                writer.link("npcs", ident, "dialogue", "conversations", table, {"script": script})
                writer.link("conversations", table, "npcs", "npcs", ident, {"script": script})
        for arm in npc["equipment"]:
            for item_id in arm["items"]:
                items.ensure(item_id)
                writer.link("npcs", ident, "weapons", "items", item_id, {"slot": arm["slot"], "status": arm["status"]})
                writer.link("items", item_id, "equipment_npcs", "npcs", ident,
                            {"slot": arm["slot"], "status": arm["status"]})
        writer.entity("npcs", ident, npc["name"], npc, category=npc["category"], scenes=scenes,
                      scene=scenes[0] if scenes else None, level=npc["level"], search=[npc["where_hint"]]
                      if npc.get("where_hint") else (),
                      card={"difficulty": npc.get("difficulty"), "placements": len(npc["locations"]),
                            "template": npc.get("template")})


def write_catalog(writer, profile):
    clientdata, client_tools = profile.root("clientdata"), profile.root("client-tools")
    strings = ClientStrings(clientdata, client_tools)
    if not strings.roots:
        raise ValueError(f"English STF files not found in {clientdata} or {client_tools}")
    if profile.key:
        print("Reading the configured game sources and generating the quest manifest", flush=True)
        files = GameFiles(profile.repositories["dsrc"])
        legacy_table = clientdata / "datatables/player/quests.iff"
        manifest = generate_manifest(strings, files, legacy_table if legacy_table.is_file() else None,
                                     profile.quest_annotations)
        for role, repo in profile.repositories.items():
            if repo.github and role != "game-data":
                prefix = repo.subdirectory + "/" if repo.subdirectory else ""
                writer.connection.executemany("INSERT INTO source_path VALUES (?,?)",
                                              ((role + "/" + path.removeprefix(prefix), path) for path, _ in repo.tree()))
    else:
        game = profile.legacy_game
        manifest = json.loads((game / "docs/quests/manifest.json").read_text(encoding="utf-8"))
        files = GameFiles(SourceRepository(game, "dsrc", manifest["source_revision"]))
    strings.prefetch(ref[0] for record in manifest["quests"] if (key := record["title"].get("key"))
                     and (ref := string_ref(key)))
    print("Indexing authored placements and maps", flush=True)
    spatial = LocationIndex(files, clientdata)
    npcs = load_npcs(files, strings, spatial)
    items = Items(files, strings)
    places, world = load_places(files, strings, spatial, include_known_worlds=not profile.key)
    print("Tracing creature spawners, spawn tables and scripts", flush=True)
    spawns = SpawnIndex(files, spatial, npcs, places).build()
    for point, config, _, template in spawns.records:
        if template:
            items.ensure(template)
        if point.get("scene"):
            world(point["scene"])
        for entry in config["entries"]:
            if item_id := entry.get("item"):
                items.ensure(item_id)
            if (npc_id := entry.get("npc")) and npc_id not in npcs:
                npcs[npc_id] = summarize_npc(npc_id, strings, "Placement", template=entry["ref"],
                                             sources=[entry["source"]] if entry.get("source") else [])
    client_landmarks = load_client_landmarks(clientdata, strings, spatial, places, world)
    areas = placement_areas(places)
    print("Resolving conversation scripts and English lines", flush=True)
    dialogue = DialogueIndex(strings, (quest["id"] for quest in manifest["quests"]))
    dialogue.scan(files)
    legacy = legacy_descriptions(manifest, clientdata, strings)
    origin_count = collect_quests(writer, files, strings, spatial, manifest, dialogue, npcs, items, world, legacy)
    print("Resolving NPC model templates against the game client", flush=True)
    models = ModelLibrary(clientdata, client_tools, (profile.root("game-data") / path for path in GAME_MODEL_PATHS),
                          asset_overlay=profile.overlay)
    model_coverage = NpcModels(files, models).index(npcs)
    publish_npcs(writer, dialogue, npcs, items, areas, world)
    for template, config in spatial.scripted_spawn_definitions():
        items.ensure(template)
        for entry in config["entries"]:
            if entry["npc"]:
                writer.link("npcs", entry["npc"], "spawners", "items", template, config)
    print("Indexing item definitions, client templates and acquisition tables", flush=True)
    inventory = publish_inventory(writer, files, strings, items, world, clientdata, models)
    index_npc_loot(writer.connection, files)
    print("Calculating creature and space loot chances", flush=True)
    loot_coverage = index_drops(writer.connection, files, items, npcs, spawns)
    print("Indexing location-map overlays from buildouts, snapshots and quest sources", flush=True)
    map_data = build_map_features(writer, files, strings, spatial, places, world, npcs, items, spawns)
    map_data.update(client_landmarks=client_landmarks, descriptions=add_location_descriptions(places, strings))
    for ident, place in sorted(places.items()):
        scene = place["scene"]
        canonical = canonical_location(ident)
        aliases = tuple(key for key, value in LOCATION_ALIASES.items() if value == ident)
        if scene != ident:
            writer.link("locations", ident, "world", "locations", scene)
        card = {"has_map": bool(place.get("map"))}
        if canonical != ident:
            card["alias_for"] = canonical
        writer.entity("locations", ident, place["name"], place, category=place["category"], scene=scene,
                      search=aliases, card=card)
    publish_location_hierarchy(writer, places)

    dialogue_stats = Counter()
    for table in sorted(dialogue.tables):
        entry = dialogue.conversation(table)
        dialogue_stats.update(entry["counts"])
        writer.entity("conversations", table, entry["name"], entry, category=table.split("/")[0],
                      card={"line_count": len(entry["lines"]), "different": entry["counts"]["different"],
                            "missing": entry["counts"]["missing"], "has_client_strings": bool(entry["asset"])})
    filters = {ident: {"name": place["name"], "has_map": bool(place.get("map")),
                       "map_id": (place.get("map") or {}).get("id"),
                       **({"alias_for": LOCATION_ALIASES[ident]} if ident in LOCATION_ALIASES else {})}
               for ident, place in sorted(places.items()) if place["scene"] == ident}
    provenance = {"source_key": profile.key, "sources": profile.metadata()} if profile.key else {}
    return writer.finish({**inventory, **provenance, "title": "Star Wars Galaxies Galactic Archive", "revision": files.revision,
                          "quest_source_counts": manifest["counts"], "origin_scene_records": origin_count,
                          "locations": filters, "ground_worlds": authored_ground_worlds(files.tab(SERVER / "region/planets.tab"), places),
                          "maps": spatial.maps, "map_layers": LAYERS, "map_data": map_data,
                          "dialogue": dict(dialogue_stats),
                          "string_errors": strings.errors,
                          "quests_with_linked_dialogue": sum(bool(value) for value in dialogue.quest_tables.values()),
                          "npcs_with_world_coordinates": sum(any(point.get("coordinates") for point in npc["locations"])
                                                            for npc in npcs.values()),
                          "legacy_descriptions": len(legacy),
                          "spawn_coverage": {**spawns.stats, **spawns.scripts.stats},
                          "model_coverage": model_coverage, "loot_coverage": loot_coverage,
                          "systems": manifest.get("other_quest_systems", [])}, spawns)


def build(path, profile):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="index-", dir=path.parent) as work:
        candidate = Path(work) / "catalog.sqlite3"
        writer = Writer(candidate)
        try:
            metadata = write_catalog(writer, profile)
        finally:
            writer.connection.close()
        candidate.replace(path)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_source_options(parser)
    parser.add_argument("--if-needed", action="store_true", help="Reuse a catalog built from the selected sources.")
    args = parser.parse_args()
    try:
        profile = select_profile(args)
        database = args.database or profile.database
        if args.if_needed and database.is_file():
            recorded = Catalog(database).metadata()
            if recorded.get("source_key") == profile.key and recorded.get("schema") == SCHEMA:
                return
        metadata = build(database, profile)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({key: metadata[key] for key in ("counts", "inventory", "dialogue", "map_data")}, indent=2))


if __name__ == "__main__":
    main()
