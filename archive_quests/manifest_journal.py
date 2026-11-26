"""Journal-backed ground and space quest definitions."""

from __future__ import annotations

from functools import lru_cache
import re

from .manifest_scripts import Script
from .manifest_sources import (
    SERVER, SHARED, SCRIPTS, TEST_ID, add_unique, edge, new_record, number, objective,
    present, quest_id, requirement, reward, source, values,
)


def link_records(records, pending):
    for origin, target, relation in pending:
        if origin in records:
            add_unique(records[origin]["follow_ups"], {
                "id": target, "target_defined": target in records, **relation,
            })
        if target in records:
            add_unique(records[target]["predecessors"], {
                "id": origin, "target_defined": origin in records, **relation,
            })


def xp_table(files):
    return {
        number(row.get("LEVEL")): [number(row.get(f"TIER_{n}_EXPERIENCE")) for n in range(1, 7)]
        for row in files.tab(SHARED / "quest/quest_experience.tab")
    }


def journal_rewards(record, row, xp_by_level, *, timing="quest_completion", prefix="", task=None):
    def get(column):
        return row.get(prefix + column)

    def add(kind, **kwargs):
        if task is not None:
            kwargs["task"] = task
        reward(record, kind, timing, row.source, **kwargs)

    if number(get("BANK_CREDITS")) > 0:
        add("credits", amount=number(get("BANK_CREDITS")), destination="bank")
    if present(get("FACTION_NAME")) and number(get("FACTION_AMOUNT")):
        add("faction", faction=get("FACTION_NAME"), amount=number(get("FACTION_AMOUNT")))
    if present(row.get("REWARD_BADGE") or get("BADGE")):
        add("badge", item_id=row.get("REWARD_BADGE") or get("BADGE"))

    xp_type = get("EXPERIENCE_TYPE")
    amount = number(get("EXPERIENCE_AMOUNT"))
    level, tier = number(row.get("LEVEL")), number(row.get("TIER"), -1)
    if xp_type and level >= 1 and tier > 0:
        xp = xp_by_level.get(level)
        if xp is None:
            record["issues"].append({"kind": "missing_xp_level", "level": level, "source": row.source})
        else:
            base = xp[min(6, tier) - 1]
            if base > 0:
                add("experience", xp_type=xp_type, base_amount=base, mode="level_tier",
                    level=level, tier=min(6, tier), configured_amount=amount)
    elif xp_type and amount > 0 and (level < 1 or tier < 0):
        add("experience", xp_type=xp_type, amount=amount)

    for column, count_column, item_type in [
        ("ITEM", "COUNT", "template"),
        ("WEAPON", "COUNT_WEAPON", "weapon"),
        ("ARMOR", "COUNT_ARMOR", "armor"),
    ]:
        item = get(column)
        if present(item):
            count = number(get(count_column), 1)
            add("item", item_id=item, count=1 if count == -1 else count, item_type=item_type)

    for column, max_options, choice in [
        ("LOOT", 3 if prefix else 1, False),
        ("EXCLUSIVE_LOOT", 10 if prefix else 0, True),
    ]:
        for slot in range(1, max_options + 1):
            suffix = "" if slot == 1 else f"_{slot}"
            item = get(column + "_NAME" + suffix)
            if present(item):
                count = number(get(column + "_COUNT" + suffix), 1)
                add("item", item_id=item, count=count, item_type="static_item",
                    selection="choose_one" if choice else "included")
    if prefix and row.get("GRANT_GCW") == "1":
        add("gcw", mode="faction_dependent", override=number(row.get("QUEST_GCW_REWARD_OVERWRITE_AMOUNT")),
            special_forces_modifier=number(row.get("QUEST_GCW_REWARD_OVERWRITE_SF_MODIFIER")))
        for alignment in ("REBEL", "IMPERIAL"):
            item = row.get(f"QUEST_GCW_{alignment}_REWARD_LOOT_NAME")
            count = number(row.get(f"QUEST_REWARD_{alignment}_LOOT_COUNT"))
            if present(item) and count > 0:
                add("item", item_id=item, count=count, item_type="static_item",
                    selection="faction_dependent", faction=alignment.title())


@lru_cache(maxsize=None)
def effective_handler(files, script: str, event: str) -> tuple[str, dict | None]:
    if not re.fullmatch(r"[\w.]+", script):
        return "handler_unknown", None
    base = SCRIPTS.joinpath(*script.split("."))
    path = next((base.with_suffix(ext) for ext in (".java", ".script")
                 if files.exists(base.with_suffix(ext))), None)
    if path is None:
        return "handler_unknown", None
    # All ground task callbacks implement the same event contract. Blank
    # overrides are meaningful, so stop at the first matching handler.
    parsed = Script(path, files.text(path))
    handler = parsed.methods.get(event)
    if handler:
        status = "calls_base_handler" if re.search(r"\bsuper\s*\.\s*" + event + r"\s*\(", handler.body) else "handler_skips_base"
        return status, source(path, handler.line)
    parent = re.search(r"\b(?:inherits|extends)\s+([\w.]+)", parsed.text)
    if script != "quest.task.ground.base_task" and parent and parent[1] != script:
        parent_name = parent[1]
        if parent_name == "base_task":
            parent_name = "quest.task.ground.base_task"
        if parent_name == "quest.task.ground.base_task":
            return "inherits_base_handler", source(path)
        return effective_handler(files, parent_name, event)
    return "handler_unknown", source(path)


def load_journal(records, pending, strings, files):
    directories = {
        "journal": SHARED / "questlist",
        "tasks": SHARED / "questtask",
        "space_server": SERVER / "spacequest",
    }
    definition_files = {}
    for part, directory in directories.items():
        for path in files.paths(directory, "*.tab", recursive=True):
            ident = path.relative_to(directory).with_suffix("").as_posix()
            if part == "space_server":
                ident = "spacequest/" + ident
            definition_files.setdefault(ident, {})[part] = path

    summaries = {ident: files.tab(paths["journal"]) for ident, paths in definition_files.items() if "journal" in paths}
    strings.prefetch_labels(rows[0].get(column) for rows in summaries.values() if rows
                            for column in ("JOURNAL_ENTRY_TITLE", "CATEGORY"))
    xp_by_level = xp_table(files)
    for ident, paths in sorted(definition_files.items()):
        family = "space" if ident.startswith("spacequest/") else "ground"
        rows = {part: files.tab(path) for part, path in paths.items() if part != "journal"}
        rows["journal"] = summaries.get(ident, [])
        summary = (rows["journal"] or [None])[0]
        details = (rows.get("space_server") or [None])[0]
        sources = [source(path) for path in paths.values()]
        title = strings.label(summary.get("JOURNAL_ENTRY_TITLE") if summary else "")
        record = records[ident] = new_record(ident, family, title, sources)
        if ident == "testquest":
            record["source_status"] = "development"
        record["definition_parts"] = sorted(paths)
        record["task_count"] = len(rows.get("tasks", []))
        for part in ("journal", "tasks", "space_server") if family == "space" else ("journal", "tasks"):
            if not rows.get(part):
                record["issues"].append({"kind": "missing_definition_part", "part": part})

        if summary:
            record["journal"] = {
                "recommended_level": number(summary.get("LEVEL")) or None,
                "tier": number(summary.get("TIER")) if summary.get("TIER") else None,
                "type": summary.get("TYPE") or None,
                "category": strings.label(summary.get("CATEGORY")),
                "repeatable": summary.get("ALLOW_REPEATS") == "1",
                "visible": summary.get("VISIBLE", "1") != "0",
            }
            for raw in values(summary.get("PREREQUISITE_QUESTS")):
                previous = quest_id(raw)
                requirement(record, "completed_quest", summary.source, id=previous)
                edge(pending, previous, ident, "requires_completion", summary.source)
            for raw in values(summary.get("EXCLUSION_QUESTS")):
                requirement(record, "exclude_active_or_completed_quest", summary.source, id=quest_id(raw))
            if family == "ground":
                journal_rewards(record, summary, xp_by_level, prefix="QUEST_REWARD_")
                if summary.get("CONDITIONAL_QUEST_GRANT_QUEST"):
                    target = quest_id(summary.get("CONDITIONAL_QUEST_GRANT_QUEST"))
                    conditions = [quest_id(raw) for raw in values(
                        summary.get("CONDITIONAL_QUEST_GRANT_QUEST_LIST_OF_COMPLETED_QUESTS"))]
                    edge(pending, ident, target, "conditional_quest_grant", summary.source,
                         completed_quests=conditions)

        for task, row in enumerate(rows.get("tasks", [])):
            ground_task(record, row, task, pending, xp_by_level, files)
        if details:
            space_definition(record, details, pending)
        if TEST_ID.search(ident) or any(part in ("test", "_debug") for part in ident.split("/")):
            record["source_status"] = "development"


def ground_task(record, row, task, pending, xp_by_level, files):
    ident = record["id"]
    script = row.get("ATTACH_SCRIPT")
    objective(record, row.get("PLANET_NAME"), row.source)
    for column, event, kind in [
        ("GRANT_QUEST_ON_COMPLETE", "OnTaskCompleted", "task_completion_grant"),
        ("GRANT_QUEST_ON_FAIL", "OnTaskFailed", "task_failure_grant"),
    ]:
        raw = row.get(column)
        if raw:
            dispatch, handler = effective_handler(files, script, event)
            edge(pending, ident, quest_id(raw), kind, row.source, task=task,
                 dispatch=dispatch, handler=handler)
    if script == "quest.task.ground.reward":
        journal_rewards(record, row, xp_by_level, timing="task_completion", task=task)
    if number(row.get("REWARD_CREDITS")) > 0:
        reward(record, "credits", "task_completion", row.source, amount=number(row.get("REWARD_CREDITS")),
               task=task, mode="configured_task_field", handler=script or None)
    for column in ("PREREQUISITE_TASKS", "EXCLUSION_TASKS", "TASKS_ON_COMPLETE", "TASKS_ON_FAIL"):
        if any(not part.isdecimal() for part in values(row.get(column))):
            record["issues"].append({"kind": "non_numeric_task_edge", "task": task,
                                     "column": column, "value": row.get(column), "source": row.source})


def space_definition(record, row, pending):
    ident = record["id"]
    objective(record, row.get("questZone"), row.source)
    if row.get("questSeries"):
        record["series"] = row.get("questSeries")
    credits = number(row.get("autoReward"))
    if credits > 0:
        reward(record, "credits", "automatic_space_completion", row.source, amount=credits, destination="bank")
    for column, unit in (
        ("reward", "base payout rate"),
        ("killReward", "kill bonus rate"),
        ("arrivalReward", "arrival bonus rate"),
        ("perfectReward", "perfect-run bonus rate"),
    ):
        amount = number(row.get(column))
        if amount > 0:
            reward(record, "credits", "space_mission_performance", row.source,
                   amount=amount, mode="performance_rate", unit=unit, column=column)
    if row.get("triggerEvent") in {"splitquest", "1"}:
        trigger = row.get("triggerArg")
        kind, separator, name = trigger.partition(":")
        if separator and kind and name:
            edge(pending, ident, f"spacequest/{kind}/{name}", "space_split", row.source,
                 trigger_condition=row.get("triggerSplitCondition"), trigger_on=row.get("triggerOn"),
                 trigger_delay=number(row.get("triggerDelay")))
