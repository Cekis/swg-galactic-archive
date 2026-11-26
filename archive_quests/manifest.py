"""Generate quest records from the selected data and English strings."""

from __future__ import annotations

from collections import Counter

from .manifest_journal import link_records, load_journal
from .manifest_legacy import epic_legacy, legacy_journal, load_scripted, regular_legacy
from .manifest_scripts import bindings, scan_scripts, script_paths
from .manifest_sources import PLANETS, ROOT, SERVER, edge, place, quest_id, requirement


PLANET_BY_CATEGORY = {name.casefold(): ident for ident, name in PLANETS.items()}


def additional_ground_tables(records, pending, files):
    directory = SERVER / "quest/ground"
    for row in files.tab(directory / "disabled.tab"):
        ident = quest_id(row.get("disabled_quests"))
        if ident in records:
            records[ident]["source_status"] = "disabled_in_ground_table"
            records[ident]["issues"].append({"kind": "disabled_in_ground_table", "source": row.source})

    for row in files.tab(directory / "quest_giver_object.tab"):
        ident = quest_id(row.get("quest_name"))
        template = row.get("quest_giver_object_template")
        if ident not in records or not template:
            continue
        record = records[ident]
        record["starts"].append(place("item", row.source, template=template))
        if row.get("pre_quest_name"):
            prerequisite = quest_id(row.get("pre_quest_name"))
            requirement(record, "completed_quest_for_item_start", row.source, id=prerequisite)
            edge(pending, prerequisite, ident, "item_start_requires_completion", row.source)
        for column, faction in (("imperial_only", "Imperial"), ("rebel_only", "Rebel")):
            if row.get(column) == "1":
                requirement(record, "faction_for_item_start", row.source, faction=faction)

    for row in files.tab(directory / "quest_path_list.tab"):
        previous = quest_id(row.get("quest_list"))
        for column, kind in (("next_quest", "quest_path_next"), ("grant_quest", "quest_path_grant")):
            if row.get(column):
                edge(pending, previous, quest_id(row.get(column)), kind, row.source)


def summarize(records):
    return {
        "quests": len(records),
        "by_family": dict(sorted(Counter(record["family"] for record in records).items())),
        "by_source_status": dict(sorted(Counter(record["source_status"] for record in records).items())),
        "resolved_titles": sum(bool(record["title"].get("key") and record["title"]["text"]) for record in records),
        "with_planet_of_origin": sum(any(start["planet"] for start in record["starts"]) for record in records),
        "with_planet_hint_only": sum(not any(start["planet"] for start in record["starts"])
                                     and bool(planet_hints(record)) for record in records),
        "with_named_npc": sum(any(start.get("npc", {}).get("text") for start in record["starts"]) for record in records),
        "with_requirements": sum(bool(record["requirements"]) for record in records),
        "with_rewards": sum(bool(record["rewards"]) for record in records),
        "with_predecessor": sum(bool(record["predecessors"]) for record in records),
        "with_missing_definition_parts": sum(any(issue["kind"] == "missing_definition_part"
                                                 for issue in record["issues"]) for record in records),
        "unresolved_links": sum(not relation["target_defined"] for record in records
                                for field in ("predecessors", "follow_ups") for relation in record[field]),
    }


def manifest(strings, files, legacy_table, annotations=None):
    records = {}
    pending = []
    load_journal(records, pending, strings, files)
    additional_ground_tables(records, pending, files)
    scripts = script_paths(files)
    sites, locations = bindings(scripts, strings, files)
    regular_legacy(records, pending, strings, locations, files)
    epic_legacy(records, pending, strings, locations, files)
    reference = legacy_journal(records, pending, strings, legacy_table)
    systems = load_scripted(records, pending, strings, annotations)
    for system in systems:
        if system["id"] == "mission_terminals":
            paths = files.paths(SERVER / "missions", "*.tab", recursive=True)
        elif system["id"] == "crafting_contracts":
            paths = [SERVER / "quest/crafting/crafting_quests.tab"]
        else:
            continue
        system["configuration"] = [
            {"path": path.relative_to(ROOT).as_posix(), "rows": len(files.tab(path))}
            for path in paths
        ]
    script_inventory = scan_scripts(records, pending, strings, scripts, sites, files)
    link_records(records, pending)
    quests = [records[ident] for ident in sorted(records)]
    for record in quests:
        category = record.get("journal", {}).get("category", {}).get("text")
        if category and category.strip().casefold() in PLANET_BY_CATEGORY:
            record["journal"]["category_planet_hint"] = PLANET_BY_CATEGORY[category.strip().casefold()]
        for entry in record["rewards"]:
            if entry["kind"] == "item" and entry.get("item_type") == "static_item":
                entry["item"] = strings.label("@static_item_n:" + entry["item_id"])
    validate(records)
    return {
        "schema_version": 1,
        "locale": "en" if strings.roots else None,
        "source_revision": files.revision,
        "scope": "Selected game sources",
        "legacy_quest_table": reference,
        "counts": summarize(quests),
        "script_inventory": script_inventory,
        "string_errors": sorted(strings.errors),
        "other_quest_systems": systems,
        "quests": quests,
    }


def validate(records):
    for ident, record in records.items():
        if not record["sources"]:
            raise ValueError(f"{ident}: no definition source")
        for relation in record["follow_ups"]:
            target = records.get(relation["id"])
            if target:
                reverse = {**relation, "id": ident, "target_defined": True}
                if reverse not in target["predecessors"]:
                    raise ValueError(f"{ident}: asymmetric follow-up {relation['id']}")
        for start in record["starts"]:
            if start["planet"] and start["planet"] not in PLANETS:
                raise ValueError(f"{ident}: unknown origin planet {start['planet']}")


def planet_hints(record):
    from_starts = {site["planet_hint"] for site in record["starts"] if site.get("planet_hint")}
    from_category = record.get("journal", {}).get("category_planet_hint")
    return from_starts or ({from_category} if from_category else set())
