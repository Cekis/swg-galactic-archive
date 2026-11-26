"""Older NPC quest tables and named quests outside the journal tables."""

from __future__ import annotations

import json
import hashlib
import re

from .manifest_sources import (
    SERVER, SCRIPTS, edge, new_record, number, objective, place, planet,
    present, requirement, reward, source,
)

# Use the existing DTII reader shared by the asset audits.
from catalog_iff import read_iff_table


def legacy_journal(records, pending, strings, path):
    if path is None:
        return None
    columns, _, rows = read_iff_table(path)
    required = {"NAME", "PARENT", "ATTACH_SCRIPT", "JOURNAL_ENTRY_SUMMARY", "CATEGORY",
                "TASK_ON_COMPLETE", "TASK_ON_FAIL", "IS_VISIBLE"}
    if missing := required.difference(columns):
        raise ValueError(f"invalid legacy quest table, missing columns: {', '.join(sorted(missing))}")
    asset = "datatables/player/quests.iff"
    labels = [row[column] for row in rows for column in ("JOURNAL_ENTRY_SUMMARY", "CATEGORY")]
    strings.prefetch_labels(labels)
    for index, row in enumerate(rows, 1):
        ident = "legacy/journal/" + row["NAME"]
        if ident in records or not row["NAME"]:
            raise ValueError(f"invalid or duplicate legacy quest name in row {index}")
        evidence = {"path": asset, "scope": "supplied_clientdata", "row": index}
        record = records[ident] = new_record(
            ident, "legacy_journal", strings.label(row["JOURNAL_ENTRY_SUMMARY"]), [evidence])
        if record["source_status"] == "authored":
            record["source_status"] = "reference_asset"
        record["journal"] = {"category": strings.label(row["CATEGORY"]), "visible": bool(row["IS_VISIBLE"]),
                             "repeatable": False}
        record["legacy"] = {"attachment": row["ATTACH_SCRIPT"]}
        if row["PARENT"]:
            previous = "legacy/journal/" + row["PARENT"]
            requirement(record, "completed_quest", evidence, id=previous)
            edge(pending, previous, ident, "requires_completion", evidence)
        for column, name in (("TASK_ON_COMPLETE", "legacy_completion_grant"), ("TASK_ON_FAIL", "legacy_failure_grant")):
            for target in row[column].split(";"):
                if target.strip():
                    edge(pending, ident, "legacy/journal/" + target.strip(), name, evidence)
    return {"path": asset, "scope": "supplied_clientdata", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "rows": len(rows)}


def legacy_name(strings, ident):
    npc = strings.npc(ident)
    if not npc["text"]:
        npc["text"] = ident.replace("_", " ").title()
        npc["name_basis"] = "formatted_source_id"
    return npc


def legacy_site(strings, locations, npc_id, row, conversation, *, random=False):
    if npc_id in locations and not random:
        return locations[npc_id]
    npc = legacy_name(strings, npc_id)
    parts = conversation.split("/")
    scene_hint = parts[1] if len(parts) > 2 and parts[0] == "static_npc" else None
    extra = {"npc": npc, "conversation_table": conversation or None}
    if planet(scene_hint):
        extra["planet_hint"] = planet(scene_hint)
    return [place("npc_archetype" if random else "npc", row.source, **extra)]


def regular_legacy(records, pending, strings, locations, files):
    roots = [
        ("legacy_static", "legacy/static", SERVER / "spawning/static_npc"),
        ("legacy_static", "legacy/static_archive", SERVER / "npc/static_quest"),
        ("legacy_theme_park", "legacy/theme_park", SERVER / "theme_park"),
        ("legacy_random", "legacy/random", SERVER / "npc/random_quest"),
    ]
    for family, prefix, root in roots:
        for path in files.paths(root, "*.tab", recursive=True):
            rows = [row for row in files.tab(path) if present(row.get("quest_type"))]
            relative = path.relative_to(root).with_suffix("").as_posix()
            previous = None
            for ordinal, row in enumerate(rows, 1):
                ident = f"{prefix}/{relative}/{ordinal}"
                conversation = row.get("convo").lower().replace("\\", "/")
                npc_id = conversation.split("/")[-1] if conversation else path.stem
                template = legacy_name(strings, npc_id)
                label = f"{template['text']} · {ordinal} ({row.get('quest_type')})"
                record = records[ident] = new_record(ident, family, {"text": label, "basis": "generated_label"}, [row.source])
                if prefix.endswith("_archive"):
                    record["source_status"] = "historical_definition"
                if path.stem in {"test", "rowtest"}:
                    record["source_status"] = "development"
                record["legacy"] = {"ordinal": ordinal, "objective_type": row.get("quest_type"),
                                    "state_key": row.get("overall_objvar") or None}
                record["starts"] = legacy_site(strings, locations, npc_id, row, conversation,
                                               random=family == "legacy_random")
                objective(record, row.get("quest_planet_loc"), row.source)
                legacy_requirements(record, row)
                legacy_rewards(record, row)
                if family != "legacy_random" and previous and previous["legacy"]["state_key"] == record["legacy"]["state_key"]:
                    edge(pending, previous["id"], ident, "legacy_chain_step", row.source)
                previous = record


def legacy_requirements(record, row):
    if present(row.get("gating_object")):
        requirement(record, "possess_item", row.source, item_id=row.get("gating_object"))
    if present(row.get("gating_objvar")):
        requirement(record, "state_threshold", row.source, key=row.get("gating_objvar"),
                    threshold=number(row.get("gating_objvar_amt")))
    if present(row.get("gating_faction")):
        requirement(record, "faction_standing", row.source, faction=row.get("gating_faction"),
                    threshold=number(row.get("gating_faction_amt")))
    if number(row.get("gating_level_amt")) > 0:
        requirement(record, "minimum_level", row.source, amount=number(row.get("gating_level_amt")))


def legacy_rewards(record, row):
    timing = "legacy_turn_in"
    if number(row.get("credits")) > 0:
        reward(record, "credits", timing, row.source, amount=number(row.get("credits")))
    for slot in range(1, 5):
        suffix = "" if slot == 1 else str(slot)
        item = row.get("reward" + suffix)
        if present(item):
            reward(record, "item", timing, row.source, item_id=item, count=1, item_type="template")
        faction_column = "faction reward" if slot == 1 else f"faction_reward{slot}"
        faction = row.get(faction_column)
        amount = number(row.get(f"faction_reward{suffix}_amount"))
        if present(faction) and amount:
            reward(record, "faction", timing, row.source, faction=faction, amount=amount)
    if number(row.get("badge_reward")) > 0:
        reward(record, "badge", timing, row.source, item_id=row.get("badge_reward"))


def epic_legacy(records, pending, strings, locations, files):
    for path in files.paths(SERVER / "npc/epic_quest", "*.tab"):
        rows = {number(row.get("Index").split(" ")[0], -1): row for row in files.tab(path) if row.get("Index")}
        if 0 not in rows:
            continue
        previous = None
        for slot, action in rows[0].cells.items():
            if not re.fullmatch(r"quest\d+", slot) or not present(action):
                continue
            ordinal = number(slot.removeprefix("quest"))
            ident = f"legacy/epic/{path.stem}/{ordinal}"
            npc = legacy_name(strings, path.stem)
            record = records[ident] = new_record(
                ident, "legacy_epic", {"text": f"{npc['text']} · {ordinal} ({action})", "basis": "generated_label"},
                [rows[0].source],
            )
            record["legacy"] = {"ordinal": ordinal, "objective_type": action, "state_key": rows[2].get(slot)}
            record["starts"] = legacy_site(strings, locations, path.stem, rows[1], rows[1].get(slot))
            if present(rows[12].get(slot)):
                requirement(record, "possess_item", rows[12].source, item_id=rows[12].get(slot))
            if previous:
                edge(pending, previous, ident, "legacy_chain_step", rows[0].source)
            previous = ident
            item = rows[4].get(slot)
            if item == "credits":
                payout = source(SCRIPTS / "npc/epic_quest/quest_convo.script", 264)
                reward(record, "credits", "legacy_turn_in", payout, amount=25, destination="bank",
                       configured_by=rows[4].source)
            elif present(item):
                reward(record, "item", "legacy_turn_in", rows[4].source, item_id=item, count=1, item_type="template")
            if present(rows[5].get(slot)):
                reward(record, "faction", "legacy_turn_in", rows[5].source,
                       faction=rows[5].get(slot), mode="script_amount")
            if present(rows[13].get(slot)):
                reward(record, "item", "legacy_turn_in", rows[13].source,
                       item_id=rows[13].get(slot), count=1, item_type="quest_gate_item")


def load_scripted(records, pending, strings, annotations):
    curated = json.loads(annotations) if annotations is not None else {}
    if not isinstance(curated, dict) or any(not isinstance(curated.get(key, []), list)
                                             for key in ("quests", "annotations", "systems")):
        raise ValueError("Quest annotations must be an object with quests, annotations and systems arrays.")
    for entry in curated.get("quests", []) + curated.get("annotations", []):
        ident = entry["id"]
        if "title" in entry:
            if ident in records:
                raise ValueError(f"duplicate quest definition: {ident}")
            record = records[ident] = new_record(ident, "scripted", strings.label(entry["title"]), entry["sources"])
        elif ident in records:
            record = records[ident]
            record["sources"].extend(entry.get("sources", []))
        else:
            continue
        for start in entry.get("starts", []):
            fields = {key: value for key, value in start.items() if key not in {"kind", "source", "scene", "npc"}}
            if "npc" in start:
                fields["npc"] = {"id": start["npc"], **strings.label(start["npc_name"])}
                fields.pop("npc_name")
            record["starts"].append(place(start["kind"], start["source"], start.get("scene"), **fields))
        record["requirements"].extend(entry.get("requirements", []))
        record["rewards"].extend(entry.get("rewards", []))
        record["issues"].extend(entry.get("issues", []))
        for predecessor in entry.get("predecessors", []):
            edge(pending, predecessor["id"], ident, "requires_completion", predecessor["source"])
    return curated.get("systems", [])
