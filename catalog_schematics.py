"""Index SWG draft schematics and crafting recipes from their authored sources."""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import struct

from catalog_client_assets import resolve_asset_path
from catalog_sources import (
    SCRIPTS, SERVER, SERVER_OBJECTS, SHARED, SHARED_OBJECTS, display_id, number, source,
)


DRAFT = "object/draft_schematic/"
SCHEMATIC_FIELDS = frozenset({
    "category", "craftedObjectTemplate", "craftedSharedTemplate", "crateObjectTemplate",
    "slots", "skillCommands", "destroyIngredients", "manufactureScripts", "attributes",
    "experimentalAttributes", "itemsPerContainer", "manufactureTime", "prototypeTime",
    "objectName", "detailedDescription", "complexity", "xpPoints", "sharedTemplate",
})
TOKEN = re.compile(
    r'//[^\r\n]*|/\*[\s\S]*?\*/|"(?:[^"\\]|\\.)*"|'
    r'\.\.|[+-]?(?:0x[0-9a-fA-F]+|\d+(?:\.(?!\.)\d*)?(?:[Ee][+-]?\d+)?|\.\d+)|'
    r'[@A-Za-z_][A-Za-z0-9_]*|[^\s]'
)
BASE = re.compile(r'^\s*@base\s+["]?([^"\s]+)', re.M)
OBJECT = re.compile(r"object/[a-z0-9_./-]+\.iff")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TABLE_RECIPE = ("weapon", "armor")
RESOURCE_TYPES = frozenset({
    "it_resource", "it_resourceclass", "it_resourcename", "it_resourcetype",
    "resource", "resourceclass", "resourcename", "resourcetype", "4",
})
NATIVE_OBJECTS = "src/engine/server/library/serverGame/src/shared/object/"
OUTPUT_CREATION = {"path": NATIVE_OBJECTS + "ManufactureSchematicObject.cpp", "line": 2303}
TIME_RULES = {
    "prototype": {"path": NATIVE_OBJECTS + "PlayerObject.cpp", "line": 2954},
    "manufacture": {"path": NATIVE_OBJECTS + "ManufactureInstallationObject.cpp", "line": 498},
}
STATION_TYPES = re.compile(r'"crafting\.type"\s*=\s*((?:CT_[A-Za-z]+(?:\s*\|\s*)?)+)')
COMPLEXITY_LIMITS = re.compile(r"\bCOMPLEXITY_LIMIT\s*=\s*\{\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
SCRIPT_PARENT = re.compile(r"(?m)^\s*inherits\s+([\w.]+)\s*;")
SCRIPT_COUNT = re.compile(r"\bsetCount\s*\(\s*prototype\s*,\s*(?P<count>[^;]+)\)\s*;")
ATTRIBUTE_COUNT = re.compile(
    r'\bgetAsciiId\(\)\.equals\(\s*"(?P<attribute>\w+)"\s*\)\s*\)\s*\{\s*'
    r'setCount\s*\(\s*prototype\s*,\s*\(int\)\s*itemAttributes\[i\]\.currentValue\s*\)\s*;'
)


@dataclass(frozen=True, slots=True)
class Field:
    value: object
    source: dict
    merge: bool = False
    entry_sources: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class StringID:
    table: str
    key: str


def object_id(value):
    if not isinstance(value, str):
        return None
    ident = value.strip(' "').replace("\\", "/").lower().removesuffix(".tpf")
    if not ident.endswith(".iff"):
        ident += ".iff"
    return ident if OBJECT.fullmatch(ident) and ".." not in PurePosixPath(ident).parts else None


def canonical_schematic(value):
    ident = object_id(value)
    if not ident or not ident.startswith(DRAFT):
        return None
    path = PurePosixPath(ident)
    return path.with_name(path.name.removeprefix("shared_")).as_posix()


def _unquote(value):
    return re.sub(r'\\(["\\])', r"\1", value[1:-1]) if value.startswith('"') else value


def _as_scalar(token):
    if token.startswith('"'):
        return _unquote(token)
    if token in ("true", "false"):
        return token == "true"
    if re.fullmatch(r"[+-]?0x[0-9a-fA-F]+", token):
        return int(token, 16)
    parsed = number(token)
    return parsed if parsed is not None else token


class TemplateSyntax(ValueError):
    pass


class TemplateParser:
    """Read TPF scalar, string ID, array, and nested record values with source lines."""

    def __init__(self, text, path):
        self.text = text
        self.path = path
        self.offsets = [match.start() for match in re.finditer("\n", text)]
        self.tokens = [match for match in TOKEN.finditer(text)
                       if not match[0].startswith(("//", "/*"))]
        self.index = 0

    def at(self, ahead=0):
        position = self.index + ahead
        return self.tokens[position][0] if position < len(self.tokens) else ""

    def take(self):
        token = self.at()
        if not token:
            raise TemplateSyntax("Unexpected end of template")
        self.index += 1
        return token

    def require(self, value):
        if self.take() != value:
            raise TemplateSyntax(f"Expected {value}")

    def value(self):
        token = self.take()
        if token == "[":
            if self.at() == "]":
                self.take()
                return []
            if IDENTIFIER.fullmatch(self.at()) and self.at(1) == "=":
                result = {}
                while self.at() != "]":
                    key = self.take()
                    self.require("=")
                    result[key] = self.value()
                    if self.at() != "]":
                        self.require(",")
                self.take()
                return result
            result = []
            while self.at() != "]":
                result.append(self.value())
                if self.at() != "]":
                    self.require(",")
            self.take()
            return result
        if token.startswith('"') and self.at().startswith('"'):
            return StringID(_unquote(token), _unquote(self.take()))
        if self.at() == "..":
            self.take()
            return [_as_scalar(token), _as_scalar(self.take())]
        return _as_scalar(token)

    def parse(self, wanted):
        fields = {}
        errors = []
        while self.at():
            start = self.index
            key = self.take()
            if key.startswith("@") or key == "#":
                line = bisect_left(self.offsets, self.tokens[start].start())
                while self.at() and bisect_left(self.offsets, self.tokens[self.index].start()) == line:
                    self.take()
                continue
            if not IDENTIFIER.fullmatch(key) or self.at() != "=":
                continue
            self.take()
            merge = self.at() == "+"
            if merge:
                self.take()
            line = 1 + bisect_left(self.offsets, self.tokens[start].start())
            try:
                value = self.value()
            except TemplateSyntax as error:
                if key in wanted:
                    errors.append(f"{self.path.name}, line {line}, {key}: {error}")
                # Recover only at clear top-level boundaries.
                while self.at() and not self.at().startswith("@"):
                    self.take()
                continue
            if key in wanted:
                fields[key] = Field(value, source(self.path, line), merge)
        return fields, errors


class Templates:
    def __init__(self, files):
        self.files = files
        self.cache = {}
        self.used_as_base = set()

    def effective(self, root, ident, ancestry=()):
        if not ident or ident in ancestry or len(ancestry) >= 24:
            return {}, [f"Template inheritance cycle or depth limit at {ident}"] if ident else []
        path = root.parent / PurePosixPath(ident).with_suffix(".tpf")
        if path in self.cache:
            fields, errors = self.cache[path]
            return dict(fields), list(errors)
        if not self.files.exists(path):
            return {}, [f"Missing base/source template {ident}"] if ancestry and ident.startswith(DRAFT) else []
        text = self.files.text(path)
        base = BASE.search(text)
        base_id = object_id(base[1]) if base else None
        if base_id and root == SERVER_OBJECTS:
            self.used_as_base.add(base_id)
        fields, errors = self.effective(root, base_id, (*ancestry, ident))
        values, problems = TemplateParser(text, path).parse(SCHEMATIC_FIELDS)
        for name, value in values.items():
            fields[name] = _merge_field(fields.get(name), value)
        errors.extend(problems)
        self.cache[path] = dict(fields), list(errors)
        return fields, errors


def _field(fields, name, default=None):
    entry = fields.get(name)
    return entry.value if entry else default


def _evidence(fields, name):
    entry = fields.get(name)
    return entry.source if entry else None


def _strings(strings, value):
    if isinstance(value, StringID):
        return strings.lookup(f"@{value.table}:{value.key}") if value.table and value.key else {}
    if isinstance(value, str) and value:
        return strings.lookup(value)
    return {}


def _name(strings, value, fallback):
    label = _strings(strings, value)
    return label.get("text") or (display_id(value.key) if isinstance(value, StringID) and value.key else fallback), label


def _list(value):
    return value if isinstance(value, list) else []


def _entry_evidence(field):
    return field.entry_sources or (field.source,) * len(_list(field.value))


def _merge_field(base, value):
    if value.merge and base and isinstance(base.value, list) and isinstance(value.value, list):
        entries = _entry_evidence(base) + _entry_evidence(value)
        return Field(base.value + value.value, value.source, entry_sources=entries)
    return value


def _sourced_list(fields, name):
    field = fields.get(name)
    return zip(_list(field.value), _entry_evidence(field)) if field else ()


def _resource_id(value):
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _ingredient_kind(value):
    return str(value or "").strip().lower()


def _ingredients(strings, data, proof):
    for row in _list(data):
        if not isinstance(row, dict):
            continue
        ident = row.get("ingredient")
        name, label = _name(strings, row.get("name"), display_id(str(ident or "")))
        yield {
            "ingredient": ident, "name": name, "name_label": label, "count": row.get("count"),
            "item_id": object_id(ident),
            "source": proof,
        }


def _slots(strings, fields, shared):
    hardpoints = {value.get("name"): value.get("hardpoint")
                  for value in _list(_field(shared, "slots")) if isinstance(value, dict)
                  and isinstance(value.get("name"), (StringID, str))}
    for index, (row, proof) in enumerate(_sourced_list(fields, "slots"), 1):
        if not isinstance(row, dict):
            continue
        name, label = _name(strings, row.get("name"), f"Slot {index}")
        options = []
        for option in _list(row.get("options")):
            if not isinstance(option, dict):
                continue
            kind = option.get("ingredientType")
            ingredients = list(_ingredients(strings, option.get("ingredients"), proof))
            if _ingredient_kind(kind) in RESOURCE_TYPES:
                for ingredient in ingredients:
                    ingredient["resource_id"] = _resource_id(ingredient["ingredient"])
            options.append({"ingredient_type": kind, "complexity": option.get("complexity"),
                            "skill_command": option.get("skillCommand"), "ingredients": ingredients})
        raw_name = row.get("name")
        yield {"name": name, "name_label": label, "optional": row.get("optional"),
               "options": options, "complexity": row.get("complexity"), "source": proof,
               "optional_skill_command": row.get("optionalSkillCommand") or row.get("skillCommand"),
               "appearance": row.get("appearance"),
               "hardpoint": hardpoints.get(raw_name) if isinstance(raw_name, (StringID, str)) else None}


def _attributes(strings, fields):
    for param in ("attributes", "experimentalAttributes"):
        for entry, proof in _sourced_list(fields, param):
            if isinstance(entry, StringID):
                experiment, label = _name(strings, entry, "")
                yield {"name": experiment, "experiment": experiment, "experiment_label": label,
                       "value": None, "min": None, "max": None, "source": proof, "status": "script_controlled"}
            elif isinstance(entry, dict):
                name, label = _name(strings, entry.get("name"), "")
                experiment, experiment_label = _name(strings, entry.get("experiment"), "")
                value = entry.get("value")
                minimum, maximum = value if isinstance(value, list) and len(value) == 2 else (value, value)
                key = entry.get("name").key if isinstance(entry.get("name"), StringID) else entry.get("name")
                yield {"name": name, "name_label": label, "key": key, "experiment": experiment,
                       "experiment_label": experiment_label, "value": value, "min": minimum, "max": maximum,
                       "source": proof, "status": "client_compiled_range" if proof and proof.get("asset_root")
                       else "authored_range"}


def _blank(ident):
    return {
        "id": ident, "name": display_id(ident), "name_label": {}, "description": {},
        "category": ident[len(DRAFT):].split("/")[0], "status": "reference_only", "sources": [], "issues": [],
        "outputs": [], "items_per_container": None, "slots": [], "complexity": None,
        "craft_time": {}, "xp": [], "grants": [], "groups": [], "revocations": [],
        "station": None, "experimentation": [], "manufacture_scripts": [], "skill_commands": [],
        "limited_uses": [], "table_recipes": [], "crafting_missions": [],
    }


def _add_source(record, evidence):
    if evidence and evidence not in record["sources"]:
        record["sources"].append(evidence)


def _set_station(record, raw_category, proof):
    if not raw_category or raw_category in ("none", "CT_none"):
        return
    category = str(raw_category).removeprefix("CT_")
    record["crafting_type"] = category
    record["station"] = {"category": category, "required": None, "source": proof}


def _hydrate_template(ident, record, strings, reader, fields, shared, has_server):
    crafted = object_id(_field(fields, "craftedObjectTemplate"))
    crafted_shared = object_id(_field(shared, "craftedSharedTemplate"))
    output = crafted or crafted_shared
    if output:
        record["outputs"].append({"item_id": output, "quantity": 1 if crafted else None,
                                  "quantity_kind": "prototype_objects" if crafted else None,
                                  "status": "authored" if crafted else "client_shared_reference",
                                  "prototype_objects": 1 if crafted else None,
                                  "source": _evidence(fields, "craftedObjectTemplate") or _evidence(shared, "craftedSharedTemplate")})
        if crafted:
            record["prototype_creation_source"] = OUTPUT_CREATION
    labels = shared or fields
    name = _field(labels, "objectName")
    description = _field(labels, "detailedDescription")
    if output and (not _strings(strings, name).get("text") or not _strings(strings, description).get("text")):
        path = PurePosixPath(output)
        shared_output = path if path.name.startswith("shared_") else path.with_name("shared_" + path.name)
        inherited, _ = reader.effective(SHARED_OBJECTS, shared_output.as_posix())
        if not _strings(strings, name).get("text"):
            name = _field(inherited, "objectName") or name
        if not _strings(strings, description).get("text"):
            description = _field(inherited, "detailedDescription") or description
    record["name"], record["name_label"] = _name(strings, name, record["name"])
    record["description"] = _strings(strings, description)
    record["items_per_container"] = _field(fields, "itemsPerContainer")
    record["crate_item_id"] = object_id(_field(fields, "crateObjectTemplate"))
    record["complexity"] = _field(fields, "complexity")
    record["destroys_ingredients"] = _field(fields, "destroyIngredients")
    record["skill_commands"] = _list(_field(fields, "skillCommands"))
    record["manufacture_scripts"] = _list(_field(fields, "manufactureScripts"))
    record["slots"] = list(_slots(strings, fields, shared))
    if not has_server:
        for index, (slot, proof) in enumerate(_sourced_list(shared, "slots"), 1):
            if isinstance(slot, dict):
                name, label = _name(strings, slot.get("name"), f"Slot {index}")
                record["slots"].append({"name": name, "name_label": label, "optional": None, "options": [],
                                        "hardpoint": slot.get("hardpoint"), "source": proof})
    record["experimentation"] = list(_attributes(strings, fields)) + list(_attributes(strings, shared))
    profiled_xp = next((attribute for attribute in record["experimentation"]
                        if str(attribute.get("key")).lower() == "xp" and isinstance(attribute["min"], (int, float))), None)
    for entry, proof in _sourced_list(fields, "xpPoints"):
        if isinstance(entry, dict):
            base = {"type": entry.get("type"), "level": entry.get("level"), "amount": entry.get("value"),
                    "source": proof}
            if profiled_xp:
                award = dict(base)
                award.update(_profiled_xp(profiled_xp))
                award["template_value"] = base["amount"]
                award["template_source"] = base["source"]
                base = award
            else:
                base["status"] = "template_value"
            record["xp"].append(base)
    if profiled_xp and not record["xp"]:
        record["xp"].append({"type": None, **_profiled_xp(profiled_xp)})
    for target, parameter in (("prototype", "prototypeTime"), ("manufacture", "manufactureTime")):
        seconds = _field(fields, parameter)
        if isinstance(seconds, (int, float)):
            time = {"seconds_per_complexity": seconds, "source": _evidence(fields, parameter),
                    "formula_source": TIME_RULES[target]}
            if isinstance(record["complexity"], (int, float)):
                time["at_authored_complexity_seconds"] = max(1, seconds * record["complexity"])
            record["craft_time"][target] = time
    _set_station(record, _field(fields, "category"), _evidence(fields, "category"))
    for key in ("craftedObjectTemplate", "slots", "category", "xpPoints", "complexity", "manufactureTime", "prototypeTime",
                "crateObjectTemplate", "destroyIngredients", "itemsPerContainer", "skillCommands", "manufactureScripts"):
        _add_source(record, _evidence(fields, key))
    for key in ("craftedSharedTemplate", "slots", "attributes", "objectName", "detailedDescription"):
        _add_source(record, _evidence(shared, key))
    if not has_server:
        record["status"] = "client_reference"
        record["issues"].append("Only the shared schematic definition is available.")
    elif not crafted or not record["slots"] or not all(slot["options"] for slot in record["slots"]):
        record["status"] = "base_template" if "/base/" in ident or "/base_" in ident else "partial_source"
        if not crafted:
            record["issues"].append("Crafted server output is unresolved.")
        if not record["slots"] or not all(slot["options"] for slot in record["slots"]):
            record["issues"].append("Server ingredient details are incomplete.")
    else:
        record["status"] = "authored_recipe"


def _profiled_xp(attribute):
    minimum, maximum = attribute["min"], attribute["max"]
    return {"amount": minimum if minimum == maximum else None, "min": minimum, "max": maximum,
            "source": attribute["source"], "status": "schematic_attribute",
            "award_rule_source": source(SCRIPTS / "systems/crafting/crafting_base.script", 783)}


def _cells(files, root, relative):
    path = root / relative
    return files.tab(path) if files.exists(path) else []


def _split_codes(value):
    return [item.strip() for item in str(value or "").strip('"').split(",") if item.strip()]


def _load_grants(files, strings, schematics):
    memberships = defaultdict(list)
    group_table = SHARED / "crafting/schematic_group.tab"
    for entry in _cells(files, SHARED, "crafting/schematic_group.tab"):
        ident = canonical_schematic(entry.get("SchematicName"))
        group = entry.get("GroupId")
        if not ident or not group:
            continue
        proof = source(group_table, entry.line)
        record = schematics.setdefault(ident, _blank(ident))
        memberships[group].append((record, proof))
        if group not in record["groups"]:
            record["groups"].append(group)
        _add_source(record, proof)

    skill_table = SHARED / "skill/skills.tab"
    for entry in _cells(files, SHARED, "skill/skills.tab"):
        skill = entry.get("NAME")
        if not skill:
            continue
        for column, target in (("SCHEMATICS_GRANTED", "grants"), ("SCHEMATICS_REVOKED", "revocations")):
            for group in _split_codes(entry.get(column)):
                for record, membership in memberships[group]:
                    grant = {
                        "skill_id": skill, "group": group, "source": source(skill_table, entry.line),
                        "group_source": membership, "label": strings.lookup(f"@skl_n:{skill}"),
                    }
                    if grant not in record[target]:
                        record[target].append(grant)


def _load_use_and_mission_tables(files, schematics):
    table = SERVER / "crafting/limited_use_schematics.tab"
    for entry in _cells(files, SERVER, "crafting/limited_use_schematics.tab"):
        ident = canonical_schematic(entry.get("schematic_name"))
        if not ident:
            continue
        proof = source(table, entry.line)
        record = schematics.setdefault(ident, _blank(ident))
        raw_item = entry.get("source_object")
        record["limited_uses"].append(
            {"uses": number(entry.get("schematic_uses")), "item_id": object_id(raw_item),
             "raw_item": raw_item, "source": proof}
        )
        _add_source(record, proof)
    table = SERVER / "quest/crafting/crafting_quests.tab"
    for entry in _cells(files, SERVER, "quest/crafting/crafting_quests.tab"):
        ident = canonical_schematic(entry.get("Schematic"))
        if not ident:
            continue
        proof = source(table, entry.line)
        record = schematics.setdefault(ident, _blank(ident))
        record["crafting_missions"].append(
            {"profession": entry.get("Profession"), "level": entry.get("Level"),
             "item_id": object_id(entry.get("Template")), "source": proof}
        )
        _add_source(record, proof)
    table = SERVER / "missions/crafting/crafting.tab"
    for entry in _cells(files, SERVER, "missions/crafting/crafting.tab"):
        ident = canonical_schematic(entry.get("strSchematic"))
        if not ident:
            continue
        proof = source(table, entry.line)
        record = schematics.setdefault(ident, _blank(ident))
        record["crafting_missions"].append({
            "name": entry.get("strName"),
            "item_id": object_id(entry.get("strDetailTemplate")),
            "component_ids": [item for index in range(1, 5)
                              if (item := object_id(entry.get(f"strComponent{index}")))],
            "source": proof,
        })
        _add_source(record, proof)


def _table_slot(entry, strings, proof, index):
    raw_name = entry.get("slot_name")
    name, label = _name(strings, raw_name if raw_name and ":" in raw_name else None,
                        display_id(raw_name) if raw_name else f"Slot {index}")
    kind = entry.get("slot_type")
    raw_ingredient = entry.get("slot_ingredient_name")
    resource = _resource_id(raw_ingredient) if _ingredient_kind(kind) in RESOURCE_TYPES else None
    ingredient = {
        "ingredient": raw_ingredient, "name": display_id(raw_ingredient or ""), "name_label": {},
        "count": number(entry.get("slot_ingredient_count")), "item_id": object_id(raw_ingredient),
        "source": proof,
    }
    if resource:
        ingredient["resource_id"] = resource
    raw_optional = str(entry.get("slot_optional") or "").lower()
    return {
        "name": name, "name_label": label,
        "slot_code": raw_name,
        "optional": {"1": True, "0": False, "true": True, "false": False}.get(raw_optional),
        "appearance": entry.get("slot_appearance"), "source": proof, "complexity": None,
        "optional_skill_command": None, "hardpoint": None,
        "options": [{"ingredient_type": kind, "complexity": None, "skill_command": None,
                     "ingredients": [ingredient] if raw_ingredient else []}],
    }


def _extract_recipe_table(files, strings, category):
    relative = f"crafting/{category}_schematics.tab"
    table = SERVER / relative
    recipe = None
    for entry in _cells(files, SERVER, relative):
        role = str(entry.get("entry_type") or "").strip().lower()
        proof = source(table, entry.line)
        if role == "slot":
            if recipe is not None:
                recipe["slots"].append(_table_slot(entry, strings, proof, len(recipe["slots"]) + 1))
            continue
        if recipe is not None:
            yield recipe
        recipe = None
        if not role:
            continue
        raw_name = entry.get("schematic_name")
        explicit = canonical_schematic(entry.get("crc_schematic_name")) or canonical_schematic(raw_name)
        if explicit is None and category == "armor" and raw_name:
            explicit = canonical_schematic(f"{DRAFT}armor/{raw_name}")
        if explicit is None:
            continue
        output = object_id(entry.get("crafted_object_template"))
        xp_type, xp_value = entry.get("xp_type"), number(entry.get("xp"))
        bounds = ("hit_points", "min_damage", "max_damage", "accuracy", "elemental_value",
                  "attack_speed", "wound_chance", "special_attack_cost")
        attributes = []
        for key in bounds:
            minimum, maximum = number(entry.get(f"{key}_low")), number(entry.get(f"{key}_high"))
            if minimum is not None or maximum is not None:
                attributes.append({"name": display_id(key), "key": key, "min": minimum, "max": maximum,
                                   "source": proof, "status": "explicit_table_range"})
        recipe = {
            "id": explicit, "kind": role, "name": raw_name, "source": proof,
            "expected_slots": number(entry.get("slots")), "slots": [],
            "outputs": [{"item_id": output, "quantity": None, "prototype_objects": None,
                         "status": "explicit_table", "source": proof}] if output else [],
            "items_per_container": number(entry.get("items_per_container")),
            "complexity": number(entry.get("complexity")),
            "category": entry.get("crafting_type") or ("clothing" if role == "clothing" else category),
            "subtype": entry.get("category"),
            "description_code": entry.get("schematic_description"),
            "volume": number(entry.get("volume")),
            "attributes": attributes,
            "xp": [{"type": xp_type, "amount": xp_value, "level": None, "source": proof}]
                  if xp_type or xp_value is not None else [],
            "manufacture_scripts": [entry.get("crafting_script")] if entry.get("crafting_script") else [],
        }
    if recipe is not None:
        yield recipe


def _load_recipe_tables(files, strings, schematics):
    for category in TABLE_RECIPE:
        for recipe in _extract_recipe_table(files, strings, category):
            record = schematics.setdefault(recipe["id"], _blank(recipe["id"]))
            record["table_recipes"].append(recipe)
            _add_source(record, recipe["source"])
            if record["status"] == "authored_recipe":
                continue
            if record["status"] in ("reference_only", "client_reference"):
                record["status"] = "table_recipe"
            for field in ("outputs", "slots", "xp", "manufacture_scripts"):
                shared_output = field == "outputs" and all(o["status"] == "client_shared_reference" for o in record[field])
                client_slots = field == "slots" and all(not slot["options"] for slot in record[field])
                if recipe[field] and (not record[field] or shared_output or client_slots):
                    record[field] = recipe[field]
            for field in ("complexity", "items_per_container"):
                if record[field] is None:
                    record[field] = recipe[field]
            if not record["station"]:
                _set_station(record, recipe["category"], recipe["source"])
            if not record["experimentation"]:
                record["experimentation"] = recipe["attributes"]
            if recipe["expected_slots"] is not None and recipe["expected_slots"] != len(recipe["slots"]):
                record["issues"].append("The crafting table slot count differs from its ingredient rows.")


def _iff_blocks(data, start, end):
    while start + 8 <= end:
        tag, size = struct.unpack_from(">4sI", data, start)
        body = start + 8
        next_block = body + size
        if next_block > end:
            raise ValueError("Schematic IFF block exceeds its container")
        yield tag, body, next_block
        start = next_block
    if start != end:
        raise ValueError("Schematic IFF has a truncated chunk header")


def _client_string(payload):
    if payload[:1] == b"\1":
        return payload[1:].split(b"\0", 1)[0].decode("ascii", "replace")
    return None


def _client_string_id(payload):
    if payload[:2] == b"\1\1":
        table, _, remainder = payload[2:].partition(b"\0")
        name = _client_string(remainder)
        if name is not None:
            return StringID(table.decode("ascii", "replace"), name)
    return None


def _client_integer(payload):
    if len(payload) == 6 and payload[0] == 1:
        return struct.unpack_from("<i", payload, 2)[0]
    if len(payload) == 10 and payload[0] == 3:
        return list(struct.unpack_from("<ii", payload, 2))
    return None


def _client_fields(data):
    values = {}

    def walk(start, end, lineage, definition=None):
        for tag, body, finish in _iff_blocks(data, start, end):
            if tag == b"FORM" and finish - body >= 4 and len(lineage) < 8:
                kind = data[body:body + 4]
                collection = {b"SISS": "slots", b"DSSA": "attributes"}.get(kind)
                if collection:
                    item = {}
                    walk(body + 4, finish, (*lineage, kind), item)
                    if item:
                        values.setdefault(collection, []).append(item)
                else:
                    walk(body + 4, finish, (*lineage, kind))
            elif tag == b"XXXX" and lineage == (b"SDSC", b"DERV"):
                if parent := object_id(data[body:finish].split(b"\0", 1)[0].decode("ascii", "replace")):
                    values["_base"] = parent
            elif tag == b"XXXX" and b"DERV" not in lineage:
                key, _, payload = data[body:finish].partition(b"\0")
                if definition is not None:
                    decode = {b"name": _client_string_id, b"experiment": _client_string_id,
                              b"hardpoint": _client_string, b"value": _client_integer}.get(key)
                    if decode and (value := decode(payload)) is not None:
                        definition[key.decode()] = value
                elif key == b"craftedSharedTemplate" and (value := _client_string(payload)) is not None:
                    values.setdefault("craftedSharedTemplate", value)
                elif key in (b"objectName", b"detailedDescription") and (value := _client_string_id(payload)):
                    values.setdefault(key.decode(), value)
                elif key in (b"slots", b"attributes") and len(payload) >= 5 and payload[0] in (0, 1):
                    name = key.decode()
                    values.setdefault(name, [])
                    values.setdefault("_append", {})[name] = bool(payload[0])

    if data[:4] != b"FORM" or data[8:12] != b"SDSC":
        raise ValueError("Client asset is not a shared draft schematic IFF")
    walk(0, len(data), ())
    return values


def _client_candidates(root, client_inventory):
    if client_inventory is None:
        return (path.relative_to(root).as_posix() for path in (root / DRAFT).rglob("*.iff"))
    return (str(value).replace("\\", "/") for value in client_inventory)


class ClientTemplates:
    """Read and merge the supplied shared IFF definitions."""

    def __init__(self, root):
        self.root = root
        self.cache = {}

    def effective(self, original, ancestry=()):
        if not original.startswith(DRAFT) or original in ancestry or len(ancestry) >= 24:
            return None
        if original in self.cache:
            return self.cache[original]
        try:
            path = (self.root / original).resolve()
            if not path.is_relative_to(self.root) or path.stat().st_size > 2_000_000:
                return None
            values = _client_fields(path.read_bytes())
        except (OSError, ValueError, struct.error):
            self.cache[original] = None
            return None
        parent = values.pop("_base", None)
        append = values.pop("_append", {})
        base = self.effective(parent, (*ancestry, original)) if parent else None
        fields = dict(base or {})
        evidence = {"path": original, "asset_root": "clientdata"}
        for key, value in values.items():
            fields[key] = _merge_field(fields.get(key), Field(value, evidence, append.get(key, False)))
        self.cache[original] = fields
        return fields


def _load_client_schematics(root, client_inventory, reader, strings, schematics):
    if not root:
        return
    root = resolve_asset_path(root)
    templates = ClientTemplates(root)
    evidence_rank = {"reference_only", "client_reference", "table_recipe"}
    for raw in sorted(_client_candidates(root, client_inventory)):
        ident = canonical_schematic(raw)
        if not ident:
            continue
        record = schematics.get(ident)
        if record and record["status"] not in evidence_rank:
            continue
        original = object_id(raw)
        fields = templates.effective(original)
        if fields is None:
            continue
        if record is None:
            record = schematics.setdefault(ident, _blank(ident))
        evidence = {"path": original, "asset_root": "clientdata"}
        record["client_template"] = original
        _add_source(record, evidence)
        if record["status"] == "reference_only":
            _hydrate_template(ident, record, strings, reader, {}, fields, False)
        else:
            presentation = _blank(ident)
            _hydrate_template(ident, presentation, strings, reader, {}, fields, False)
            for key in ("outputs", "slots", "xp", "experimentation", "description"):
                if not record[key]:
                    record[key] = presentation[key]
            if not record["name_label"] and presentation["name_label"]:
                record["name"], record["name_label"] = presentation["name"], presentation["name_label"]


def _annotate_stations(files, records):
    station_root = SERVER_OBJECTS / "tangible/crafting/station"
    compatibility = defaultdict(list)
    for path in files.paths(station_root, "*.tpf"):
        kind = path.stem.rsplit("_", 1)[-1]
        if kind not in ("station", "tool"):
            continue
        text = files.text(path)
        match = STATION_TYPES.search(text)
        if match:
            proof = source(path, text.count("\n", 0, match.start()) + 1)
            ident = path.relative_to(SERVER_OBJECTS.parent).with_suffix(".iff").as_posix()
            item = {"item_id": ident, "source": proof, "kind": kind}
            if kind == "station":
                item["access"] = "public" if path.stem.startswith("public_") else "private"
            for category in re.findall(r"CT_([A-Za-z]+)", match[1]):
                compatibility[category].append(item)

    tool_script = SCRIPTS / "systems/crafting/base_tool.script"
    raw_script = files.text(tool_script) if files.exists(tool_script) else ""
    rule = COMPLEXITY_LIMITS.search(raw_script)
    for record in records:
        station = record["station"]
        if not station:
            continue
        matches = compatibility.get(station["category"], ())
        station["compatible_stations"] = [item for item in matches if item["kind"] == "station"]
        station["compatible_tools"] = [item for item in matches if item["kind"] == "tool"]
        if not rule or not isinstance(record["complexity"], (int, float)) or record["status"] != "authored_recipe":
            continue
        levels = [int(value) for value in rule.groups()]
        level = next((index for index, maximum in enumerate(levels) if record["complexity"] <= maximum), len(levels))
        station["minimum_crafting_level"] = level
        station["required"] = level >= 2
        station["minimum_access"] = ("public" if level == 2 else "private_or_powered_droid") if level >= 2 else None
        station["when_schematic_filtering_enabled"] = True
        station["complexity_rule_source"] = source(tool_script, raw_script.count("\n", 0, rule.start()) + 1)
        station["filter_source"] = {"path": NATIVE_OBJECTS + "TangibleObject.cpp", "line": 2938}


class RecipeScripts:
    """Find authored manufacture scripts that assign a produced item's stack count."""

    def __init__(self, files):
        self.files = files
        self.cache = {}
        table = SERVER / "food/food_data.tab"
        self.food_yields = {}
        for row in _cells(files, SERVER, "food/food_data.tab"):
            raw = row.get("TEMPLATE")
            amount = number(row.get("QUANTITY"))
            if raw and amount is not None and (ident := object_id("object/tangible/" + raw)):
                self.food_yields[ident] = {"base_units": amount, "base_source": source(table, row.line)}

    def counts(self, name, ancestry=()):
        if name in self.cache:
            return self.cache[name]
        if name in ancestry or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", str(name)):
            return []
        path = SCRIPTS / (name.replace(".", "/") + ".script")
        if not self.files.exists(path):
            self.cache[name] = []
            return []
        text = self.files.text(path)
        text = re.sub(r"//[^\n]*|/\*[\s\S]*?\*/", lambda match: "\n" * match[0].count("\n"), text)
        attributes = {match.end(): match["attribute"] for match in ATTRIBUTE_COUNT.finditer(text)}
        counts = [
            {"expression": match["count"].strip(), "attribute": attributes.get(match.end()),
             "source": source(path, text.count("\n", 0, match.start()) + 1)}
            for match in SCRIPT_COUNT.finditer(text)
        ]
        if parent := SCRIPT_PARENT.search(text):
            counts.extend(self.counts(parent[1], (*ancestry, name)))
        self.cache[name] = counts
        return counts

    def annotate(self, record):
        if not record["outputs"] or record["outputs"][0]["status"] != "authored":
            return
        output = record["outputs"][0]
        evidence = []
        attribute_keys = {attribute.get("key") for attribute in record["experimentation"]}
        for script in record["manufacture_scripts"]:
            for count in self.counts(script):
                if count["attribute"] and count["attribute"] not in attribute_keys:
                    continue
                if count not in evidence:
                    evidence.append(count)
        if not evidence:
            return
        observed = {"status": "script_controlled", "evidence": evidence}
        if output["item_id"] in self.food_yields and any(
            count["source"]["path"].endswith("/crafting_base_food_new.script") and count["expression"] == "quantity"
            for count in evidence
        ):
            observed.update(self.food_yields[output["item_id"]])
        if len(evidence) == 1:
            count = evidence[0]
            attributes = [attribute for attribute in record["experimentation"]
                          if count["attribute"] and attribute.get("key") == count["attribute"]]
            if len(attributes) == 1:
                attribute = attributes[0]
                if isinstance(attribute["min"], (int, float)) and isinstance(attribute["max"], (int, float)):
                    observed.update({"status": "authored_attribute_range", "min": attribute["min"], "max": attribute["max"],
                                     "attribute_source": attribute["source"]})
            elif re.fullmatch(r"\d+", count["expression"]):
                observed["literal_count"] = int(count["expression"])
        output["units_per_object"] = observed


def load_schematics(files, strings, clientdata, client_inventory=None):
    """Read effective authored recipes, explicit crafting tables, and client schematic evidence."""
    schematics = {}
    reader = Templates(files)
    inventory = defaultdict(dict)
    for root in (SERVER_OBJECTS, SHARED_OBJECTS):
        for path in files.paths(root / "draft_schematic", "*.tpf", recursive=True):
            template = path.relative_to(root.parent).with_suffix(".iff").as_posix()
            if (ident := canonical_schematic(template)):
                inventory[ident][root] = template
    for ident, paths in sorted(inventory.items()):
        record = schematics[ident] = _blank(ident)
        server = paths.get(SERVER_OBJECTS)
        shared = paths.get(SHARED_OBJECTS)
        fields, errors = reader.effective(SERVER_OBJECTS, server) if server else ({}, [])
        shared = object_id(_field(fields, "sharedTemplate")) or shared
        client_fields, client_errors = reader.effective(SHARED_OBJECTS, shared) if shared else ({}, [])
        if server:
            _add_source(record, source(SERVER_OBJECTS.parent / PurePosixPath(server).with_suffix(".tpf")))
        if shared and client_fields:
            _add_source(record, source(SHARED_OBJECTS.parent / PurePosixPath(shared).with_suffix(".tpf")))
        _hydrate_template(ident, record, strings, reader, fields, client_fields, bool(server))
        record["issues"].extend(errors + client_errors)
    _load_grants(files, strings, schematics)
    _load_use_and_mission_tables(files, schematics)
    _load_recipe_tables(files, strings, schematics)
    _load_client_schematics(clientdata, client_inventory, reader, strings, schematics)
    for ident in reader.used_as_base:
        record = schematics.get(ident)
        if record and record["status"] == "partial_source":
            record["status"] = "base_template"
    _annotate_stations(files, schematics.values())
    scripts = RecipeScripts(files)
    for record in schematics.values():
        scripts.annotate(record)
        if record["status"] == "reference_only":
            record["issues"].append("A crafting table references this schematic. Its template is unresolved.")
    return schematics
