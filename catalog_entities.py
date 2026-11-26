"""Prepare NPC, equipment, item, and location profiles for the local index."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path, PurePosixPath
import re

from catalog_models import ModelError, chunks, descendant, value
from catalog_sources import (
    PLANETS, SERVER, SERVER_OBJECTS, SHARED, SHARED_OBJECTS, SCRIPTS, display_id, number, source,
)


ITEM_ROOTS = {
    "tangible": "Item template", "intangible": "Intangible", "weapon": "Weapon",
    "draft_schematic": "Schematic", "manufacture_schematic": "Manufacturing schematic",
    "resource_container": "Resource container", "construction_contract": "Construction contract",
    "building": "Structure", "factory": "Factory", "installation": "Installation",
    "token": "Token", "waypoint": "Waypoint", "mission": "Mission entry", "player_quest": "Chronicle quest item",
}
STATIC_ITEM_DIRS = ("static/item", "static/structure", "static/worldbuilding/furniture", "static/worldbuilding/terminal")
ITEM_ID = re.compile(r"(?:object/(?:%s)/[a-zA-Z0-9_./-]+\.iff"
                     r"|(?:item|weapon|armor|costume|schematic|storyteller)_[a-zA-Z0-9_]+)" %
                     "|".join((*ITEM_ROOTS, *STATIC_ITEM_DIRS)))
SCRIPT_ID = re.compile(r"(?<![\w.])(?:\w+\.)+\w+(?![\w.])")
OBJECT_LABEL = re.compile(r'^[ \t]*(objectName|detailedDescription)[ \t]*=[ \t]*"([^"]*)"[ \t]*"([^"]*)"', re.MULTILINE)
SHARED_TEMPLATE = re.compile(r'^[ \t]*sharedTemplate[ \t]*=[ \t]*"([^"]+)"', re.MULTILINE)
OBJECT_BASE = re.compile(r"^@base[ \t]+(\S+)", re.MULTILINE)
OBJECT_SCALAR = re.compile(r"^[ \t]*(\w+)[ \t]*=[ \t]*([^\r\n]+)", re.MULTILINE)
DEPLOYED_TEMPLATE = re.compile(r'"player_structure\.deed\.template"\s*=\s*"([^"]+)"')
ITEM_ATTRIBUTES = {
    "appearanceFilename", "gameObjectType", "maxHitPoints", "hitPoints", "currentDecayPoints", "volume",
    "containerType", "containerVolumeLimit", "maxResources", "complexity", "count", "charges",
    "invulnerable", "race", "species", "armorRating", "armorEffectiveness", "requiredLevel",
    "weaponType", "attackType", "damageType", "elementalType", "elementalValue", "minDamageAmount",
    "maxDamageAmount", "attackSpeed", "attackCost", "accuracy", "audibleRange", "minRange", "maxRange",
    "damageRadius", "woundChance", "slotType", "level", "missionType",
}
NPC_STATS = ("BaseLevel", "difficultyClass", "Damagelevelmodifier", "StatLevelModifier",
             "ToHitLevelModifier", "ArmorLevelModifier", "armorKinetic", "armorEnergy", "armorBlast",
             "armorHeat", "armorCold", "armorElectric", "armorAcid", "armorStun", "attackSpeed",
             "movement_speed", "minCash", "maxCash", "minScale", "maxScale", "aggressive", "assist",
             "invulnerable", "socialGroup", "pvpFaction", "lootTable", "lootList")


def item_ref(value):
    value = (value or "").strip().replace("\\", "/").lower()
    return value if ITEM_ID.fullmatch(value) and all(part not in ("", ".", "..") for part in value.split("/")) else None


def client_object_inventory(clientdata):
    object_root = clientdata / "object"
    inventory = {}
    for group in (*ITEM_ROOTS, *STATIC_ITEM_DIRS, "ship"):
        for directory, _, filenames in os.walk(object_root / group):
            for filename in filenames:
                if filename.lower().endswith(".iff"):
                    path = Path(directory) / filename
                    inventory[path.relative_to(clientdata).as_posix().lower()] = path
    return inventory


def client_template_fields(path):
    """Decode the single-valued StringId parameters of the compiled SHOT form."""
    try:
        with path.open("rb") as stream:
            data = stream.read(512 * 1024 + 1)
        if len(data) > 512 * 1024:
            return {}
        root = next(chunks(memoryview(data)), None)
        template = descendant(root, "SHOT") if root else None
        if not template or not template.form:
            return {}
        settings = {}
        for version in chunks(template.body):
            if not version.form or not version.tag.isdecimal():
                continue
            for parameter in chunks(version.body):
                if parameter.tag != "XXXX":
                    continue
                key, _, raw = bytes(parameter.body).partition(b"\0")
                if key in (b"objectName", b"detailedDescription") and raw.startswith(b"\x01\x01"):
                    table, _, index = raw[2:].partition(b"\0")
                    label = value(index[1:]) if index.startswith(b"\x01") else ""
                    if table and label:
                        settings[key.decode("ascii")] = f"@{table.decode('ascii')}:{label}"
                elif key == b"appearanceFilename" and raw.startswith(b"\x01"):
                    if appearance := value(raw[1:]):
                        settings["appearance"] = appearance
        return settings
    except (OSError, ModelError, ValueError):
        return {}


def item_category(ident):
    _, group, subtype, *_ = ident.split("/")
    types = {
        "armor": "Armor", "clothing": "Clothing", "deed": "Deed", "ship": "Ship component",
        "ship_control_device": "Ship control device", "food": "Food and drink", "furniture": "Furniture",
        "loot": "Loot item", "droid": "Droid item", "dice": "Dice", "medicine": "Medicine",
        "loot_npc": "Loot item", "crafting": "Crafting component", "component": "Crafting component",
        "wearables": "Wearable", "terminal": "Terminal", "vehicle": "Vehicle item",
        "painting": "Decoration", "container": "Container",
    }
    if group == "tangible" and subtype == "ship" and ident.endswith("_deed.iff"):
        return "Ship deed"
    if group == "tangible":
        return types.get(subtype, ITEM_ROOTS[group])
    if group == "intangible" and subtype == "ship":
        return "Ship control device"
    if group == "static":
        return "Scenery item"
    return ITEM_ROOTS[group]


def authored_scalar(raw):
    raw = raw.split("//", 1)[0].strip()
    if raw in ("true", "false"):
        return raw == "true"
    converted = number(raw)
    return converted if converted is not None else raw.strip('"')


def supported_item_template(template):
    if "/base/" in template:
        return False
    if template.startswith("object/building/"):
        return template.startswith("object/building/player/")
    return True


def script_ids(text):
    return sorted(set(SCRIPT_ID.findall(text or "")))


def attributes(row, columns=None):
    columns = columns or row.cells.keys()
    return {name: (parsed if (parsed := number(row.get(name))) is not None else row.get(name))
            for name in columns if row.get(name) != ""}


def npc_label(strings, ident):
    for table in ("mob/creature_names", "npc_spawner_n", "npc_name", "space/space_mobile_type"):
        if label := strings.name(table, ident):
            return label
    return None


def summarize_npc(ident, strings, category, **fields):
    name = fields.pop("name", None) or npc_label(strings, ident) or display_id(ident)
    return {"id": ident, "name": name, "category": category, "template": None,
            "level": None, "notes": None, "stats": {}, "balance": {}, "equipment": [],
            "scripts": [], "sources": [], "locations": [], **fields}


def weapon_choices(files):
    choices = {}
    for row in files.tab(SERVER / "ai/ai_weapons.tab"):
        ident = row.get("weapon_id")
        refs = sorted({ref for key, value in row.cells.items() if key != "weapon_id" and (ref := item_ref(value))})
        if ident and refs:
            choices[ident] = (refs, row.source)
    for path in files.paths(SERVER / "npc_customization/weapon", "*.tab"):
        refs = set()
        for row in files.tab(path):
            for value in row.cells.values():
                if value.startswith(("melee/", "ranged/", "creature/")):
                    value = "object/weapon/" + value
                if ref := item_ref(value):
                    refs.add(ref)
        if refs:
            choices.setdefault(path.stem, (sorted(refs), source(path)))
    return choices


def equipment(row, choices):
    output = []
    for slot in ("primary", "secondary"):
        declared = row.get(slot + "_weapon")
        if not declared or declared == "none":
            continue
        arm = {"slot": slot, "key": declared, "attack_speed": number(row.get(slot + "_weapon_speed")),
               "specials": row.get(slot + "_weapon_specials") or None, "items": [], "source": row.source}
        weapon = item_ref(declared)
        if weapon:
            arm.update(status="fixed", items=[weapon])
        elif declared == "unarmed":
            arm["status"] = "unarmed"
        elif declared in choices:
            refs, proof = choices[declared]
            arm.update(status="fixed" if len(refs) == 1 else "pool", items=refs, pool_source=proof)
        else:
            arm["status"] = "unresolved_pool"
        output.append(arm)
    return output


def load_npcs(files, strings, spatial):
    npcs = {}
    choices = weapon_choices(files)
    balance = {int(row.get("Level")): row for row in files.tab(SERVER / "mob/stat_balance.tab") if row.get("Level").isdigit()}
    mobs = SERVER / "mob/creatures.tab"
    for row in files.tab(mobs):
        ident = row.get("creatureName")
        if not ident or ident in npcs:
            continue
        level = number(row.get("BaseLevel"))
        difficulty = row.get("difficultyClass", "NORMAL") or "NORMAL"
        row_balance = balance.get(level)
        chosen = {}
        if row_balance:
            prefix = {"ELITE": "Elite_", "BOSS": "Boss_"}.get(difficulty.upper(), "")
            base_stats = ("HP", "Action", "damagePerSecond", "ToHit", "Def", "Armor", "XP",
                          "Regen", "CombatRegen")
            chosen = {"values": {name: value for name in base_stats
                                 if (value := number(row_balance.get(prefix + name))) is not None},
                      "source": row_balance.source}
        role = row.get("niche").replace("_", " ").title()
        npcs[ident] = summarize_npc(
            ident, strings, "Creature" if role not in ("", "Npc", "Pc", "Droid", "Vehicle") else (role or "NPC").upper(),
            template=row.get("template") or None, level=level, difficulty=difficulty,
            level_mode="authored_base", notes=row.get("notes") or None, where_hint=row.get("where") or None,
            stats=attributes(row, NPC_STATS), balance=chosen, equipment=equipment(row, choices),
            scripts=script_ids(row.get("scripts")), sources=[row.source],
        )

    for row in files.tab(SERVER / "space_content/npc_spawners.tab"):
        ident, template = row.get("sidName"), row.get("strTemplate")
        if not ident or not template.startswith("object/"):
            continue
        if ident not in npcs:
            npcs[ident] = summarize_npc(ident, strings, "Space contact", template=template,
                                        name=row.get("oldStrName_now_in_npc_spawner_n") or None)
        npc = npcs[ident]
        npc["sources"].append(row.source)
        npc["scripts"].extend(row.get(f"strScript{slot}") for slot in range(1, 5) if row.get(f"strScript{slot}"))
        if npc.get("template") and npc["template"] != template:
            npc.setdefault("variant_templates", []).append(template)
        else:
            npc["template"] = template

    for row in files.tab(SERVER / "space_mobile/space_mobile.tab"):
        key = row.get("strIndex")
        if not key:
            continue
        ident = "space/" + key
        npcs[ident] = summarize_npc(
            ident, strings, "Space mobile", name=npc_label(strings, key), template=row.get("strAppearance") or None,
            notes=row.get("strComments") or None,
            stats=attributes(row, ("strShip", "strPilot", "shipClass", "intXP", "intMinCredits", "intMaxCredits",
                                   "space_faction", "isAggro", "strCapitalShipType", "strAppearance")),
            scripts=script_ids(row.get("scriptlist")) + script_ids(row.get("strConversation")),
            sources=[row.source],
        )
    for ident, locations in spatial.by_mob.items():
        if ident not in npcs:
            if not locations:
                continue
            npcs[ident] = summarize_npc(ident, strings, "Placement", template=locations[0].get("template"))
        npcs[ident]["locations"].extend(locations)
    return npcs


class Items:
    def __init__(self, files, strings):
        self.files = files
        self.strings = strings
        self.records = {}
        self.template_fields = {}
        self.asset_labels = {}
        self.assets = {}
        self.deployments = []
        self.wrappers = defaultdict(set)

    def ensure(self, ident):
        if ident not in self.records:
            template = ident if ident.startswith("object/") else None
            category = item_category(template) if template else "Item"
            self.records[ident] = {"id": ident, "name": display_id(ident), "category": category,
                                   "template": template, "description": {}, "sources": [], "attributes": {},
                                   "acquisitions": [], "level": None, "status": "referenced"}
        return self.records[ident]

    def parse_templates(self):
        for root, is_shared in ((SERVER_OBJECTS, False), (SHARED_OBJECTS, True)):
            for directory in (*ITEM_ROOTS, *STATIC_ITEM_DIRS):
                for path in self.files.paths(root / directory, "*.tpf", recursive=True):
                    template = "object/" + path.relative_to(root).with_suffix(".iff").as_posix().lower()
                    data = self.files.text(path)
                    labels = {key: f"@{table}:{ident}" for key, table, ident in OBJECT_LABEL.findall(data)
                              if table and ident}
                    match = SHARED_TEMPLATE.search(data)
                    base = OBJECT_BASE.search(data)
                    native = {key: authored_scalar(raw) for key, raw in OBJECT_SCALAR.findall(data)
                              if key in ITEM_ATTRIBUTES}
                    self.template_fields[template] = {"labels": labels, "source": source(path),
                                                      "shared": match[1].lower() if match else None,
                                                      "base": base[1].lower() if base else None, "attributes": native}
                    if not is_shared and supported_item_template(template):
                        self.ensure(template)["status"] = "authored"
                    if not is_shared and match and "/base/" not in template:
                        self.wrappers[match[1].lower()].add(template)
                    if is_shared or not template.startswith("object/tangible/deed/"):
                        continue
                    for target in DEPLOYED_TEMPLATE.findall(data):
                        if deployed := item_ref(target):
                            self.ensure(deployed)
                            self.deployments.append((template, deployed, source(path)))

    def from_client(self, inventory):
        self.assets = inventory
        for asset in sorted(inventory):
            file = PurePosixPath(asset)
            template = item_ref(str(file.with_name(file.name.removeprefix("shared_"))))
            if not template or asset in self.wrappers or not supported_item_template(template):
                continue
            item = self.ensure(template)
            if item["status"] != "authored":
                item["status"] = "reference_asset"

    def resolve_item(self, raw):
        ident = item_ref(raw)
        if not ident or ident in self.records or not ident.startswith("object/"):
            return ident
        file = PurePosixPath(ident)
        if not file.name.startswith("shared_"):
            return ident
        canonical = file.with_name(file.name.removeprefix("shared_")).as_posix()
        wrappers = self.wrappers.get(ident, set())
        if canonical in wrappers or (not wrappers and canonical in self.records):
            return canonical
        return next(iter(wrappers)) if len(wrappers) == 1 else ident

    def asset_for(self, template):
        field = self.template_fields.get(template, {})
        if shared := field.get("shared"):
            if shared in self.assets:
                return shared
        file = PurePosixPath(template)
        conventional = file if file.name.startswith("shared_") else file.with_name("shared_" + file.name)
        return next((asset for asset in (conventional.as_posix(), template) if asset in self.assets), None)

    def load_asset_labels(self):
        needed = set()
        for item in self.records.values():
            template = item["template"]
            if template and (asset := self.asset_for(template)):
                fields = self.template_fields.get(asset, {})
                if not {"objectName", "detailedDescription"}.issubset(fields.get("labels", {})):
                    needed.add(asset)
        ordered = sorted(needed)
        with ThreadPoolExecutor(max_workers=12) as pool:
            self.asset_labels = dict(zip(ordered, pool.map(client_template_fields,
                                                           (self.assets[asset] for asset in ordered))))
        self.strings.prefetch(
            ref[1:].split(":", 1)[0] for labels in
            [*(record["labels"] for record in self.template_fields.values()), *self.asset_labels.values()]
            for ref in labels.values() if ref.startswith("@")
        )

    def from_master(self):
        directory = SERVER / "item/master_item"
        for filename in ("master_item.tab", "storyteller_item.tab"):
            if not self.files.exists(directory / filename):
                continue
            for row in self.files.tab(directory / filename):
                ident = row.get("name")
                if not ident:
                    continue
                item = self.ensure(ident)
                item["sources"].append(row.source)
                configured = row.get("string_name")
                name = self.strings.name("static_item_n", ident) or configured or item["name"]
                item.update(name=name, category=(row.get("type") or "item").title(),
                            template=item_ref(row.get("template_name")) or row.get("template_name") or None,
                            level=number(row.get("required_level")), status="authored",
                            description=self.strings.lookup(f"@static_item_d:{ident}"),
                            attributes=attributes(row, ("tier", "value", "required_skill", "charges",
                                                       "can_reverse_engineer", "version", "unique")))
                if not item["description"]["text"] and row.get("string_detail"):
                    item["description"] = {"text": row.get("string_detail"), "source": row.source}
        for filename in ("armor_stats.tab", "weapon_stats.tab", "item_stats.tab"):
            for row in self.files.tab(directory / filename):
                ident = row.get("name")
                if not ident:
                    continue
                item = self.ensure(ident)
                item["attributes"].update(attributes(row, (key for key in row.cells if key != "name")))
                item["sources"].append(row.source)
                item["status"] = "authored"
        for ident, title in self.strings.table("static_item_n")[0].items():
            item = self.ensure(ident)
            item["name"] = title or item["name"]
            if item["status"] == "referenced":
                asset = self.strings.table("static_item_n")[1]
                if asset:
                    item.update(status="reference_asset")
                    item["sources"].append({"path": asset})
            if not item["description"].get("text"):
                item["description"] = self.strings.lookup(f"@static_item_d:{ident}")

    def from_acquisitions(self):
        item_tables = SERVER / "item"
        vendor_tables = [item_tables / "token_vendor.tab",
                         *self.files.paths(item_tables / "vendor", "*.tab", recursive=True)]
        loot_tables = [SERVER / "space_loot/loot_items.tab",
                       *self.files.paths(SERVER / "loot/loot_items", "*.tab", recursive=True)]
        for kind, tables in (("vendor", vendor_tables), ("loot", loot_tables)):
            for path in tables:
                if not self.files.exists(path):
                    continue
                for row in self.files.tab(path):
                    fields = row.cells.items()
                    if kind == "vendor" and "item" in row.cells:
                        fields = [("item", row.get("item"))]
                    for column, value in fields:
                        known_id = value.strip('"').lower()
                        if ident := item_ref(value) or (known_id if kind == "loot" and known_id in self.records else None):
                            acquisition = {"kind": kind, "table": path.stem, "column": column, "source": row.source}
                            if kind == "vendor":
                                acquisition["terms"] = attributes(row, (
                                    key for key in row.cells if key in ("cash", "class", "resourceAmount", "any_token")
                                    or re.fullmatch(r"token\d+", key)
                                ))
                            item = self.ensure(ident)
                            item["acquisitions"].append(acquisition)
                            item["status"] = "authored"

    def from_ship_components(self):
        component_ids = {}
        table = SHARED / "space/ship_components.tab"
        if self.files.exists(table):
            for row in self.files.tab(table):
                if ident := item_ref(row.get("object_template")):
                    component_ids[row.get("name")] = (ident, row.source)
                    item = self.ensure(ident)
                    item["attributes"].update(attributes(row, ("component_type", "compatibility")))
                    item["sources"].append(row.source)
                    item["status"] = "authored"
        for path in self.files.paths(SERVER / "ship/components", "*.tab"):
            for row in self.files.tab(path):
                if ident := item_ref(row.get("strType")):
                    item = self.ensure(ident)
                    item["attributes"].update(attributes(row, (key for key in row.cells if key not in ("strType", "strComments"))))
                    item["sources"].append(row.source)
                    item["status"] = "authored"
        return component_ids

    def native_attributes(self, template):
        chain = []
        seen = set()
        while template and template not in seen and template in self.template_fields:
            seen.add(template)
            field = self.template_fields[template]
            chain.append(field)
            template = field["base"]
        return {key: {"value": value, "source": field["source"]}
                for field in reversed(chain) for key, value in field["attributes"].items()}

    def finish(self):
        self.load_asset_labels()
        for ident, item in self.records.items():
            template = item["template"]
            if not template:
                continue
            field = self.template_fields.get(template, {})
            file = PurePosixPath(template)
            shared = field.get("shared") or (template if file.name.startswith("shared_")
                                             else str(file.with_name("shared_" + file.name)))
            shared_field = self.template_fields.get(shared, {})
            asset = self.asset_for(template)
            compiled = self.asset_labels.get(asset, {})
            labels = {**compiled, **shared_field.get("labels", {}), **field.get("labels", {})}
            item["base_template"] = field.get("base")
            item["native_attributes"] = {**self.native_attributes(shared), **self.native_attributes(template)}
            item["client_template"] = asset
            item["appearance"] = compiled.get("appearance") or item["native_attributes"].get("appearanceFilename", {}).get("value")
            if item["level"] is None:
                required = item["native_attributes"].get("requiredLevel") or {}
                item["level"] = required.get("value") if isinstance(required.get("value"), int) else None
            if (ref := labels.get("objectName")) and ident == template:
                label = self.strings.lookup(ref)
                item["name"] = label["text"] or item["name"]
                item["name_code"] = label
            if not item["description"].get("text") and (ref := labels.get("detailedDescription")):
                item["description"] = self.strings.lookup(ref)
            item["sources"].extend(record["source"] for record in (field, shared_field) if record)
            if field or shared_field or asset:
                if asset:
                    item["sources"].append({"path": "clientdata/" + asset})
                if item["status"] == "referenced":
                    item["status"] = "authored" if field and ident == template and not file.name.startswith("shared_") else "reference_asset"


def scene_title(scene, strings):
    planet_names = {"kashyyyk_main": "Kashyyyk · Kachirho", "space_tatooine": "Tatooine Space",
                    "space_corellia": "Corellian Space", "space_naboo": "Naboo Space",
                    "space_ord_mantell": "Ord Mantell Space", "space_npe_falcon": "Millennium Falcon"}
    return (planet_names.get(scene) or PLANETS.get(scene) or strings.name("planet_n", scene)
            or strings.name("zone_n", scene) or display_id(scene))


def coords(row, columns):
    values = [number(row.get(name)) for name in columns]
    return values if all(value is not None for value in values) else None


def load_places(files, strings, spatial):
    places = {}
    world_info = {row.get("planet"): row for row in files.tab(SERVER / "region/planets.tab")}

    def world(scene):
        if scene not in places:
            row = world_info.get(scene)
            places[scene] = {"id": scene, "name": scene_title(scene, strings), "scene": scene,
                             "category": "Space zone" if scene.startswith("space_") else "World",
                             "map": spatial.maps.get(scene), "bounds": coords(row, ("minX", "minZ", "maxX", "maxZ"))
                             if row else None, "sources": [row.source] if row else []}
        return places[scene]

    for scene in sorted(set(world_info) | {entry["scene"] for entry in spatial.maps.values()} | set(PLANETS)):
        if scene:
            world(scene)
    for path in files.paths(SERVER / "region", "region_*.tab"):
        scene = path.stem.removeprefix("region_")
        for row in files.tab(path):
            label = row.get("regionName")
            if not label:
                continue
            world(scene)
            key = f"{scene}/region/{row.line}"
            located = strings.lookup(label) if label.startswith("@") else {"text": display_id(label)}
            circle = row.get("geometry") in ("circle", "1")
            planar = ("minX_or_centerX", "minZ_or_centerZ", "maxX_or_radius")
            location = coords(row, planar + (() if circle else ("maxZ",)))
            position = None
            if location:
                x1, z1, third = location[:3]
                position = [x1 if circle else (x1 + third) / 2, None, z1 if circle else (z1 + location[3]) / 2]
            places[key] = {"id": key, "name": located["text"] or display_id(label), "scene": scene,
                           "category": "Region", "name_label": located, "map": spatial.maps.get(scene),
                           "marker": {"coordinates": position, "status": "region_center"},
                           "boundary": {"shape": "circle" if circle else "rectangle", "values": location},
                           "attributes": attributes(row, ("pvpType", "geographicalType", "minDifficultyType",
                                                          "maxDifficultyType", "buildable", "municipal", "visible")),
                           "sources": [row.source]}
    for key, row in spatial.places.items():
        scene = row["scene"]
        world(scene)
        label = strings.lookup(row["name_key"]) if row.get("name_key") else {}
        name = label.get("text") or row.get("name") or display_id(key)
        category = {"shuttleport": "Transport", "starport": "Transport",
                    "buildout_area": "Buildout area"}.get(row["kind"], "Landmark")
        places[key] = {"id": key, "name": name, "scene": scene,
                       "map": spatial.maps.get(row.get("map_id") or scene),
                       "marker": {"coordinates": row.get("coordinates"), "status": row.get("status")},
                       "category": category, "name_label": label, "bounds": row.get("bounds"),
                       "template": row.get("template"), "object_id": row.get("object_id"),
                       "transport_type": row["kind"] if category == "Transport" else None,
                       "required_event": row.get("required_event"),
                       "sources": [row["source"]]}
    return places, world
