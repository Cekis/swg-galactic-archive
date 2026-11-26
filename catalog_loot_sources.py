"""Index item sources with loot rolls that are independent of a creature defeat."""

from collections import defaultdict
from dataclasses import dataclass
import re

from catalog_drops import _hit, _packed
from catalog_sources import SERVER, source


SCRIPTS = SERVER.parent / "script"
DRUM = SCRIPTS / "systems/treasure_map/base/dummy_treasure_drum.java"
MAPS = SERVER / "treasure_map/treasure_map.tab"
LOOT = SCRIPTS / "library/loot.java"
STEALTH = SCRIPTS / "library/stealth.java"
CONSTANT = re.compile(r'\bstatic final (?:int|String) (\w+)\s*=\s*(?:"([^"]+)"|(\d+))\s*;')


@dataclass(frozen=True)
class LootChoice:
    selector: str
    weight: float
    rolls: int
    rule: dict
    label: str


def _table_results(tables, items, selection, level):
    regular = defaultdict(float)
    required = defaultdict(float)
    evidence = defaultdict(list)
    for channel, probabilities, rolls, label in (
        ("ground", regular, selection.rolls, selection.label),
        ("required", required, 1, "Required loot entry"),
    ):
        entries = tables.distribution(channel, selection.selector)
        for (raw, gate), (probability, proofs) in sorted(entries.items()):
            item = items.resolve_item(raw) or raw
            if item not in items.records or level < gate:
                continue
            probabilities[item] += float(probability)
            evidence[item].append({
                "channel": label, "selector": selection.selector, "table_chance": float(probability),
                "rolls": rolls, "gate": selection.weight, "rule": selection.rule, "table_selection": True,
                "minimum_level": gate or None, "sources": [{"path": path, "line": line} for path, line in sorted(proofs)],
            })
    return {
        item: (1 - (1 - _hit(min(1, regular[item]), selection.rolls)) * (1 - min(1, required[item])), details)
        for item, details in evidence.items()
    }


def _draw(tables, items, choices, level):
    """A single bonus draw selects exactly one table before rolling in it."""
    chances = defaultdict(float)
    evidence = defaultdict(list)
    for choice in choices:
        if choice.weight <= 0:
            continue
        for item, (chance, details) in _table_results(tables, items, choice, level).items():
            chances[item] += choice.weight * chance
            evidence[item].extend(details)
    return chances, evidence


def _treasure_maps(tables, items):
    files = tables.files
    if not files.exists(DRUM) or not files.exists(MAPS):
        return
    constants = {key: value or int(count) for key, value, count in CONSTANT.findall(files.text(DRUM))}
    names = ("LOOT_TABLE_81_90", "BONUS_LOOT_TABLE_81_90", "RARE_BONUS_LOOT_TABLE_81_90", "TREASURE_LOOT_ITEMS",
             "NUMBER_OF_BONUS_LOOT_ITEMS", "FOUR_GROUP_PERCENT_CHANCE_FOR_RARE",
             "EIGHT_GROUP_PERCENT_CHANCE_FOR_RARE", "EIGHT_GROUP_PERCENT_CHANCE_FOR_SUPER_RARE")
    if not all(name in constants for name in names):
        return
    high, rare, super_rare, rolls, bonus_rolls, four_rare, eight_rare, eight_super = (constants[name] for name in names)
    for row in files.tab(MAPS):
        selector = row.get("loot_table")
        lo, hi = row.get("map_level_min"), row.get("map_level_max")
        if not selector or not lo.isdigit() or not hi.isdigit():
            continue
        base, base_sources = _draw(tables, items, [LootChoice(selector, 1, rolls, source(DRUM, 314), "Base chest loot")], int(hi))
        cases = [(0, "Per chest", ())]
        if selector == high:
            cases = [
                (0, "Per chest with fewer than 4 participants", ()),
                (1, "Per chest with 4–7 participants", (
                    LootChoice(rare, four_rare / 100, bonus_rolls, source(DRUM, 395), "Rare bonus"),
                    LootChoice(high, 1 - four_rare / 100, bonus_rolls, source(DRUM, 404), "Standard bonus"),
                )),
                (2, "Per chest with 8 participants", (
                    LootChoice(super_rare, eight_super / 100, bonus_rolls, source(DRUM, 362), "Super-rare bonus"),
                    LootChoice(rare, (eight_rare - eight_super) / 100, bonus_rolls, source(DRUM, 372), "Rare bonus"),
                    LootChoice(high, 1 - eight_rare / 100, bonus_rolls, source(DRUM, 381), "Standard bonus"),
                )),
            ]
        for count, condition, choices in cases:
            bonus, bonus_sources = _draw(tables, items, choices, int(hi))
            ident = f"treasure-map/{lo}-{hi}/{count}"
            for item in base.keys() | bonus.keys():
                chance = base[item] + (1 - base[item]) * _hit(min(1, bonus[item]), count)
                yield item, ident, {
                    "name": "Treasure map chest", "level": int(lo), "max_level": int(hi), "order": count,
                    "chance": chance, "condition": condition, "source": row.source,
                    "details": base_sources[item] + bonus_sources[item],
                }


def _rare_pickpocket(tables, items):
    if not tables.files.exists(LOOT) or not tables.files.exists(STEALTH):
        return
    rare = tables.files.text(LOOT).partition("public static boolean generateTheftLootRare(")[2]
    branch = re.search(r'if\s*\(intLevel <= (\d+)\)\s*\{\s*treasureLevel\s*=\s*"([0-9_]+)"', rare)
    if not branch or 'strTreasureTable = "treasure/treasure_";' not in rare or "strTable, 1, intLevel" not in rare:
        return
    max_level, suffix = branch.groups()
    rule = source(LOOT, 889)
    choices = [LootChoice("treasure/treasure_" + suffix, 1, 1, rule, "Rare pickpocket table")]
    minimum, _ = _draw(tables, items, choices, 1)
    maximum, evidence = _draw(tables, items, choices, int(max_level))
    for item, chance in maximum.items():
        yield item, "rare-pickpocket", {
            "name": "Rare pickpocket loot", "level": None, "order": 0, "chance": chance,
            "minimum_chance": minimum[item], "condition": f"Per rare loot roll on an eligible mark up to level {max_level}",
            "source": source(STEALTH, 2029), "details": evidence[item],
        }


def index_loot_sources(db, tables, items):
    for index in (_treasure_maps, _rare_pickpocket):
        db.executemany("INSERT INTO item_loot_source VALUES (?,?,?)", (
            (item, ident, _packed(detail)) for item, ident, detail in index(tables, items)
        ))
    return db.execute("SELECT COUNT(DISTINCT item) FROM item_loot_source").fetchone()[0]
