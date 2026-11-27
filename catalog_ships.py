"""Index player ship chassis, loadouts and acquisition paths from SWG data.

The page ID is the server template's `shipType`, used as the key of both
shiptype.tab and ship_chassis.tab. Player templates sharing one shipType
appear as variants. Client-only entries use their compiled template stem
and carry a separate availability marker.
"""

from collections import defaultdict
from pathlib import Path, PurePosixPath
import re
import struct

from catalog_client_assets import resolve_asset_path
from catalog_models import ModelError, ModelLibrary, chunks, descendant, normalize_ref, value
from catalog_sources import (
    SERVER, SERVER_OBJECTS, SHARED, SHARED_OBJECTS, SCRIPTS, display_id, number, read_iff_table, source,
)


CHASSIS_TABLE = SHARED / "space/ship_chassis.tab"
CHASSIS_CLIENT = "datatables/space/ship_chassis.iff"
SPAWN_TABLE = SERVER / "ship/shiptype.tab"
MODIFIER_TABLE = SERVER / "ship/chassis_modifiers.tab"
BROKER_TABLE = SERVER / "space_crafting/chassis_npc.tab"
CRAFTING_SCRIPT = SCRIPTS / "library/space_crafting.scriptlib"
STARTER_SCRIPT = SCRIPTS / "library/space_quest.scriptlib"
SHIP_OBJECTS = "object/ship/player/"
PLAYER_DRAFTS = SERVER_OBJECTS / "draft_schematic/space"
CRAFTED_HULLS = SERVER_OBJECTS / "tangible/ship/components/chassis"
DEED_FOLDER = SERVER_OBJECTS / "tangible/ship/crafted/chassis"
STATIC_ITEMS = SERVER / "item/master_item"
BASE_FIELD = re.compile(r"^\s*@base\s+(object/[^\s]+\.iff)", re.M | re.I)
STRING_FIELDS = re.compile(r'^\s*(objectName|detailedDescription|lookAtText)\s*=\s*"([^"]*)"\s*"([^"]*)"', re.M)
QUOTED_VALUE = r'"([^"]*)"'
QUOTED_VALUE_B = re.compile(rb"\x01([^\0]*)\0")
NUMBER_VALUE = r"[+-]?\d+(?:\.\d+)?"
CRAFTED_STAT = re.compile(
    rf'\[\s*name\s*=\s*"crafting"\s*"(hp|massMax)"[^\]]*?\bvalue\s*=\s*'
    rf"({NUMBER_VALUE})(?:\s*\.\.\s*({NUMBER_VALUE}))?"
)
DEED_VALUE = re.compile(rf'"ship_chassis\.([\w.]+)"\s*=\s*(?:"([^"]*)"|({NUMBER_VALUE}))')
DEED_CALL = re.compile(
    r'\bcreateChassisFromDeed\(\s*\w+\s*,\s*self\s*,\s*([^,]+),\s*([^,]+),\s*([^,]+),\s*("[^"]+"|\w+)\s*\)'
)
DEED_STATIC_VALUE = re.compile(r"(?:string|float|int|boolean):ship_chassis\.([\w.]+)=([^,\"]+)")
STARTER_PAIR = re.compile(
    r'pcd\s*=\s*"(object/intangible/ship/[^"]+)"\s*;\s*ship\s*=\s*"(object/ship/player/[^"]+)"'
)
DEED_CREATION = re.compile(r'createObject\("object/tangible/ship/crafted/chassis/"\s*\+\s*type\s*\+\s*"_deed\.iff"')
DEVICE_CREATION = re.compile(r'createObject\("object/intangible/ship/"\s*\+\s*type\s*\+\s*"_pcd\.iff"')
ROLES = {
    "bomber": "Bomber", "heavyfighter": "Heavy fighter", "interceptor": "Interceptor",
    "lightfighter": "Light fighter", "mediumfighter": "Medium fighter", "pob": "Player-on-board ship",
}
MODIFIERS = {
    "Level": "chassis_tier", "fltMaxSpeedModifier": "maximum_speed_modifier",
    "slideDamp": "slide_dampener", "engine_accel": "engine_acceleration",
    "engine_decel": "engine_deceleration", "engine_yaw_accel": "yaw_acceleration_degrees",
    "engine_pitch_accel": "pitch_acceleration_degrees", "engine_roll_accel": "roll_acceleration_degrees",
    "speed_rotation_factor_min": "minimum_speed_rotation_factor",
    "speed_rotation_factor_optimal": "optimal_speed_rotation_factor",
    "speed_rotation_factor_max": "maximum_speed_rotation_factor",
}
SPAWN_STATS = {
    "HitPoints": "default_spawn_hit_points", "massMax": "default_spawn_mass_limit",
    "slideDamp": "default_spawn_slide_dampener", "engine_speed": "default_spawn_engine_speed",
    "engine_yaw": "default_spawn_yaw_rate", "engine_pitch": "default_spawn_pitch_rate",
    "engine_roll": "default_spawn_roll_rate", "lots_consumed": "default_spawn_lots_consumed",
}
TEMPLATE_PATHS = ("appearanceFilename", "portalLayoutFilename", "cockpitFilename", "interiorLayoutFileName")
DEED_STATS = {
    "hp": "hit_points", "currentHp": "current_hit_points", "mass": "mass_limit",
    "badgeRestricted": "required_badge", "groundQuestRestricted": "required_ground_quest",
    "spaceQuestRestricted": "required_space_quest", "spaceQuestRestrictedType": "required_space_quest_type",
    "tcg_ship": "tcg_ship", "multifactional": "multifactional",
}


def _field(text, name, pattern=QUOTED_VALUE):
    return re.search(rf"^[ \t]*{name}\s*=\s*{pattern}", text, re.M)


def _quoted(text, name):
    match = _field(text, name)
    return match[1].strip() if match else ""


def _list(text, name):
    match = _field(text, name, r'\+?\s*\[([^\]]*)\]')
    return re.findall(r'"([^"]+)"', match[1]) if match else []


def _iff(path, root):
    return "object/" + path.relative_to(root).with_suffix(".iff").as_posix()


def _template_path(root, ref):
    ref = normalize_ref(ref)
    if ref and ref.startswith("object/") and ref.endswith(".iff"):
        return root / PurePosixPath(ref.removeprefix("object/")).with_suffix(".tpf")
    return None


def _template_chain(files, root, ref):
    visited = set()
    while (path := _template_path(root, ref)) and path not in visited and len(visited) < 12 and files.exists(path):
        visited.add(path)
        text = files.text(path)
        yield path, text
        match = BASE_FIELD.search(text)
        ref = match[1] if match else None


def _asset_source(ref):
    return {"path": f"clientdata/{ref}"}


def _localized(strings, table, key):
    return strings.lookup(f"@{table}:{key}" if table and key else key)


def _label(ident, name_ref):
    # This content-tools capital ship has a stale Decimator name reference.
    if ident in {"player_corellian_corvette", "player_star_destroyer"} and name_ref.get("key") == "player_decimator":
        return display_id(ident.removeprefix("player_"))
    return name_ref.get("text") or display_id(ident.removeprefix("player_"))


def _authored_metadata(files, strings, ref):
    metadata = {"sources": []}
    for path, text in _template_chain(files, SHARED_OBJECTS, ref):
        fields = {key: value.lower() for key in TEMPLATE_PATHS if (value := _quoted(text, key))}
        for name, table, key in STRING_FIELDS.findall(text):
            # Explicit blank descriptions override any description of the base.
            fields[name] = _localized(strings, table, key)
        for name in ("hasWings", "playerControlled"):
            if match := _field(text, name, r"(true|false)\b"):
                fields[name] = match[1] == "true"
        for name, value in fields.items():
            metadata.setdefault(name, value)
        if "certifications" not in metadata and _field(text, "certificationsRequired", r"\["):
            metadata["certifications"] = _list(text, "certificationsRequired")
        if fields or path.stem == PurePosixPath(ref).stem:
            metadata["sources"].append(source(path))
    metadata.setdefault("certifications", [])
    return metadata


def _compiled_parameters(form):
    if form and (body := next((part for part in chunks(form.body) if part.form and part.tag.isdecimal()), None)):
        for parameter in chunks(body.body):
            if parameter.tag == "XXXX":
                key, separator, data = bytes(parameter.body).partition(b"\0")
                if separator:
                    yield key.decode("ascii"), data


def _compiled_metadata(model, strings, ref):
    try:
        form = model._form(ref, "object/")
        if form.tag != "SSHP":
            return {}
        _, appearance = model._object_appearance(ref)
        result = {**appearance, "sources": [_asset_source(ref)], "certifications": []}
        ship = dict(_compiled_parameters(form))
        for key in ("playerControlled", "hasWings"):
            if ship.get(key, b"")[:1] == b"\x01":
                result[key] = ship[key][1:2] == b"\x01"
        for key in ("cockpitFilename", "interiorLayoutFileName"):
            if ship.get(key, b"")[:1] == b"\x01":
                result[key] = value(ship[key][1:]).lower()
        tangible = dict(_compiled_parameters(descendant(form, "STOT")))
        certs = tangible.get("certificationsRequired", b"")
        if len(certs) >= 5:
            result["certifications"] = [token.decode("ascii") for token in QUOTED_VALUE_B.findall(certs[5:]) if token]
        labels = dict(_compiled_parameters(descendant(form, "SHOT")))
        for key in ("objectName", "detailedDescription", "lookAtText"):
            data = labels.get(key, b"")
            if data.startswith(b"\x01"):
                table = QUOTED_VALUE_B.match(data, 1)
                entry = QUOTED_VALUE_B.match(data, table.end() if table else 2)
                if entry:
                    result[key] = _localized(strings, table[1].decode("ascii") if table else "",
                                             entry[1].decode("ascii"))
        return result
    except (OSError, UnicodeError, ModelError, struct.error):
        return {}


def _template_records(files, strings, model):
    records = defaultdict(list)
    shared_refs = set()
    for path in files.paths(SERVER_OBJECTS / "ship/player", "player_*.tpf"):
        text = files.text(path)
        ident = _quoted(text, "shipType")
        shared_ref = _quoted(text, "sharedTemplate")
        if not ident or not shared_ref:
            continue
        shared_refs.add(shared_ref)
        metadata = _authored_metadata(files, strings, shared_ref)
        if not any(metadata.get(key) for key in ("appearanceFilename", "portalLayoutFilename")):
            compiled = _compiled_metadata(model, strings, shared_ref)
            metadata["sources"].extend(compiled.get("sources", []))
            for key in TEMPLATE_PATHS:
                if compiled.get(key):
                    metadata.setdefault(key, compiled[key])
        records[ident].append(_record_template(strings, path.stem, _iff(path, SERVER_OBJECTS), shared_ref, metadata,
                                              [source(path), *metadata["sources"]]))

    directory = model.root / SHIP_OBJECTS
    for path in sorted(directory.glob("shared_player_*.iff")):
        ref = SHIP_OBJECTS + path.name.lower()
        if ref in shared_refs:
            continue
        metadata = _compiled_metadata(model, strings, ref)
        if not metadata.get("playerControlled"):
            continue
        ident = path.stem.lower().removeprefix("shared_")
        records[ident].append(_record_template(strings, ident, ref, ref, metadata, [_asset_source(ref)],
                                              "client-reference"))
    return records


def _record_template(strings, ident, template, shared_ref, metadata, sources, availability="server-authored"):
    name_ref = metadata.get("objectName", {})
    if not name_ref.get("text") and not name_ref.get("code"):
        name_ref = _localized(strings, "space_ship", ident)
    description = metadata.get("detailedDescription", {"text": None})
    model_asset = metadata.get("portalLayoutFilename") or metadata.get("appearanceFilename")
    return {
        "id": ident, "name": _label(ident, name_ref), "name_ref": name_ref, "template": template,
        "shared_template": shared_ref, "model_asset": model_asset, "description": description,
        "look_at_text": metadata.get("lookAtText", {"text": None}), "certifications": metadata["certifications"],
        "availability": availability, "sources": sources,
        "cockpit_asset": metadata.get("cockpitFilename"), "interior_asset": metadata.get("interiorLayoutFileName"),
        "portal_asset": metadata.get("portalLayoutFilename"),
        "has_wings": metadata.get("hasWings"), "player_controlled": metadata.get("playerControlled"),
    }


def _table(files, path, key):
    if not files.exists(path):
        return {}
    header, types = files.text(path).split("\n", 2)[:2]
    defaults = {}
    for column, spec in zip(header.rstrip("\r").split("\t"), types.rstrip("\r").split("\t")):
        if match := re.fullmatch(r"[fib]\[([^]]+)\]", spec.strip()):
            if (parsed := number(match[1])) is not None:
                defaults[column] = parsed
    result = {}
    for row in files.tab(path):
        if ident := row.get(key):
            result[ident] = defaults | {name: cell for name, cell in row.cells.items() if cell}, row.source
    return result


def _client_chassis(clientdata):
    try:
        _, _, rows = read_iff_table(resolve_asset_path(clientdata) / CHASSIS_CLIENT)
        return {row["name"]: (row, {**_asset_source(CHASSIS_CLIENT), "row": index})
                for index, row in enumerate(rows, 1) if str(row.get("name", "")).startswith("player_")}
    except (OSError, AssertionError, ValueError, KeyError, struct.error):
        return {}


def _matching_fields(data, fields):
    return {label: parsed for key, label in fields.items() if (parsed := number(data.get(key))) is not None}


def _slots(chassis, spawn, strings):
    result = []
    for key, cell in chassis.items():
        if f"{key}_hitweight" not in chassis or not cell:
            continue
        name_ref = _localized(strings, "ship_slot_n", key)
        compatibility = re.split(r"[,\s]+", str(cell).strip())
        default = spawn.get(key) or None
        default_stats = {
            field.removeprefix(key + "_"): parsed for field, raw in spawn.items()
            if field.startswith(key + "_") and (parsed := number(raw)) is not None
        } if default else {}
        result.append({
            "id": key, "name": name_ref.get("text") or display_id(key), "name_ref": name_ref,
            "compatibility": compatibility,
            "compatibility_names": [
                _localized(strings, "ship_slot_compatabilities_n", compat).get("text") or compat
                for compat in compatibility
            ],
            "hit_weight": number(chassis.get(f"{key}_hitweight")),
            "targetable": number(chassis.get(f"{key}_targetable")) == 1,
            "default_component": default, "default_component_stats": default_stats,
        })
    return result


def _line(text, match):
    return text.count("\n", 0, match.start()) + 1


def _crafting_sources(files):
    if not files.exists(CRAFTING_SCRIPT):
        return None, None
    text = files.text(CRAFTING_SCRIPT)
    return tuple(source(CRAFTING_SCRIPT, _line(text, match)) if (match := pattern.search(text)) else None
                 for pattern in (DEED_CREATION, DEVICE_CREATION))


def _relations_for_crafting(files, links, names):
    blueprints = {}
    for path in files.paths(CRAFTED_HULLS, "*.tpf"):
        text = files.text(path)
        if match := re.search(r'"shiptype"\s*=\s*"([^"]+)"', text):
            token = _iff(path, SERVER_OBJECTS)
            if ident := names.get(match[1]):
                blueprints[token] = ident
                links[ident].append({"kind": "product", "item": token, "source": source(path, _line(text, match))})
    for path in files.paths(PLAYER_DRAFTS, "*.tpf", True):
        text = files.text(path)
        match = _field(text, "craftedObjectTemplate")
        if not match or not (ident := blueprints.get(match[1])):
            continue
        link = {"kind": "schematic", "item": _iff(path, SERVER_OBJECTS), "product": match[1],
                "source": source(path, _line(text, match)), "attributes": {}}
        if shared := _template_path(SHARED_OBJECTS, _quoted(text, "sharedTemplate")):
            if files.exists(shared):
                ranges = {"hp": "hit_points", "massMax": "mass_limit"}
                for stat, minimum, maximum in CRAFTED_STAT.findall(files.text(shared)):
                    link["attributes"][ranges[stat] + "_min"] = number(minimum)
                    link["attributes"][ranges[stat] + "_max"] = number(maximum or minimum)
                if link["attributes"]:
                    link["attribute_source"] = source(shared)
        links[ident].append(link)


def _deed_attributes(values):
    attrs = {}
    for key, value in values.items():
        if key in DEED_STATS:
            if key in {"hp", "currentHp", "mass"}:
                value = number(value)
            elif key in {"tcg_ship", "multifactional"}:
                value = value == "true"
            attrs[DEED_STATS[key]] = value
    return attrs


def _deed_script(files, script):
    path = SCRIPTS / (script.replace(".", "/") + ".script")
    if not files.exists(path):
        return None
    text = files.text(path)
    constants = dict(re.findall(r'\bconst\s+\w+\s+(\w+)\s*=\s*("[^"]+"|[-+.\d]+f?)\s*;', text))
    for match in DEED_CALL.finditer(text):
        hp, current_hp, mass, ship = (constants.get(raw.strip(), raw.strip()) for raw in match.groups())
        if ship.startswith('"') and all(number(x.rstrip("fF")) is not None for x in (hp, current_hp, mass)):
            return ship.strip('"'), {
                "hp": hp.rstrip("fF"), "currentHp": current_hp.rstrip("fF"), "mass": mass.rstrip("fF"),
            }, source(path, _line(text, match))
    return None


def _relations_for_deeds(files, links, names, conversion_source):
    paths = [*files.paths(DEED_FOLDER, "*_deed.tpf"),
             *files.paths(SERVER_OBJECTS / "tangible/space", "*_deed.tpf", True)]
    for path in paths:
        text = files.text(path)
        fields = {key: literal or numeric for key, literal, numeric in DEED_VALUE.findall(text)}
        attribute_source = source(path)
        deed_type = fields.get("type")
        if not deed_type:
            for script in _list(text, "scripts"):
                if scripted := _deed_script(files, script):
                    deed_type, fields, attribute_source = scripted
                    break
        conventional_name = path.stem.removesuffix("_deed")
        if ident := names.get(deed_type or conventional_name):
            if deed_type or (path.parent == DEED_FOLDER and conversion_source):
                link = {"kind": "deed", "item": _iff(path, SERVER_OBJECTS), "source": source(path)}
                if attrs := _deed_attributes(fields):
                    link.update(attributes=attrs, attribute_source=attribute_source)
                elif conversion_source:
                    link["link_source"] = conversion_source
                links[ident].append(link)

    master = _table(files, STATIC_ITEMS / "master_item.tab", "name")
    stats = _table(files, STATIC_ITEMS / "item_stats.tab", "name")
    for item, (raw, stat_source) in stats.items():
        fields = dict(DEED_STATIC_VALUE.findall(str(raw.get("objvars", ""))))
        if fields.get("type") not in names or item not in master:
            continue
        row, item_source = master[item]
        if target := row.get("template_name"):
            links[names[fields["type"]]].append({
                "kind": "deed", "item": target, "static_item": item, "attributes": _deed_attributes(fields),
                "source": item_source, "attribute_source": stat_source,
            })


def _relations_for_control_devices(files, links, names, creation_source):
    for path in files.paths(SERVER_OBJECTS / "intangible/ship", "*_pcd.tpf"):
        if ident := names.get(path.stem.removesuffix("_pcd")):
            link = {"kind": "control_device", "item": _iff(path, SERVER_OBJECTS), "source": source(path)}
            if creation_source:
                link["link_source"] = creation_source
            links[ident].append(link)
    if files.exists(STARTER_SCRIPT):
        text = files.text(STARTER_SCRIPT)
        for match in STARTER_PAIR.finditer(text):
            device, ship = match.groups()
            short_name = PurePosixPath(ship).stem.removeprefix("player_")
            if ident := names.get(short_name):
                links[ident].append({
                    "kind": "control_device", "item": device, "source": source(STARTER_SCRIPT, _line(text, match)),
                    "role": "starter_ship",
                })


def _relationship_ids(links, kind):
    return sorted({x["item"] for x in links if x["kind"] == kind})


def _combined_sources(sources):
    seen = set()
    result = []
    for entry in sources:
        signature = entry["path"], entry.get("line"), entry.get("row")
        if signature not in seen:
            seen.add(signature)
            result.append(entry)
    return result


def load_ships(files, strings, clientdata, *, model=None) -> dict[str, dict]:
    """Return stable ship records with authored data and labeled client references."""
    model = model or ModelLibrary(clientdata)
    templates = _template_records(files, strings, model)
    spawn_rows = _table(files, SPAWN_TABLE, "name")
    modifiers = _table(files, MODIFIER_TABLE, "strChassis")
    authored_chassis = _table(files, CHASSIS_TABLE, "name")
    client_chassis = _client_chassis(clientdata)
    broker_rows = _table(files, BROKER_TABLE, "shipType")
    runtime_names = {template["id"].removeprefix("player_"): ident for ident, group in templates.items()
                     for template in group if template["availability"] == "server-authored"}
    links = defaultdict(list)
    conversion_source, creation_source = _crafting_sources(files)
    _relations_for_crafting(files, links, runtime_names)
    _relations_for_deeds(files, links, runtime_names, conversion_source)
    _relations_for_control_devices(files, links, runtime_names, creation_source)

    # Configured test rows without a player template are reference entries.
    configured = {ident for ident in authored_chassis.keys() & spawn_rows.keys() if ident.startswith("player_")}
    ships = {}
    for ident in sorted(templates.keys() | configured):
        group = sorted(templates.get(ident, []), key=lambda record: (record["id"] != ident, record["id"]))
        if group:
            primary, *variants = group
        else:
            metadata = {"certifications": []}
            primary = _record_template(strings, ident, None, None, metadata, [], "server-reference")
            variants = []
        chassis, chassis_source = authored_chassis.get(ident, client_chassis.get(ident, ({}, None)))
        spawn, spawn_source = spawn_rows.get(ident, ({}, None))
        tuning, tuning_source = modifiers.get(ident, ({}, None))
        broker, broker_source = broker_rows.get(ident.removeprefix("player_"), ({}, None))
        relationships = links[ident]
        controls = _relationship_ids(relationships, "control_device")
        attrs = _matching_fields(tuning, MODIFIERS) | _matching_fields(spawn, SPAWN_STATS)
        if role := tuning.get("strCategory"):
            attrs["role"] = role
        if broker:
            attrs.update(broker_conversion_credits=number(broker.get("price")), broker_skill=broker.get("skill"))
        for key in ("wing_open_speed_factor", "flyby_sound", "hit_sound_group"):
            if (entry := chassis.get(key)) not in (None, ""):
                attrs[key] = number(entry) if key == "wing_open_speed_factor" else entry
        if primary.get("player_controlled") is not None:
            attrs["player_controlled"] = primary["player_controlled"]
        if primary.get("has_wings") is not None:
            attrs["has_wings"] = primary["has_wings"]
        if primary["availability"] == "server-authored" and not controls:
            primary["availability"] = "server-reference"
            for variant in variants:
                if variant["availability"] == "server-authored":
                    variant["availability"] = "server-reference"
        category = ROLES.get(str(tuning.get("strCategory", "")).lower())
        if not category:
            category = "Capital ship" if ident in {"player_corvette", "player_star_destroyer"} else "Ship"
        if not primary["name_ref"].get("text") and broker.get("name"):
            primary["name"] = broker["name"]
        sources = [
            *primary["sources"], *(entry for entry in (chassis_source, spawn_source, tuning_source, broker_source) if entry),
        ]
        for relation in relationships:
            sources.extend(relation[key] for key in ("source", "attribute_source", "link_source") if key in relation)
        for record in [primary, *variants]:
            if (asset := record["model_asset"]) and normalize_ref(asset) and asset.startswith("appearance/"):
                try:
                    if model._path(asset, "appearance/").is_file():
                        record["sources"].append(_asset_source(asset))
                    else:
                        model.report_issue("Models", f"Ship {ident} references missing client appearance {asset}.")
                except (OSError, ModelError) as error:
                    model.report_issue("Models", f"Could not locate ship {ident} appearance {asset}. {error}")
            for localized in (record["name_ref"], record["description"], record["look_at_text"]):
                if asset := localized.get("asset"):
                    record["sources"].append({"path": asset})
        sources.extend(primary["sources"])
        ships[ident] = {
            **primary, "id": ident, "category": category, "attributes": attrs, "variants": variants,
            "slots": _slots(chassis, spawn, strings), "relationships": relationships, "sources": _combined_sources(sources),
            "control_devices": controls, "deeds": _relationship_ids(relationships, "deed"),
            "schematics": _relationship_ids(relationships, "schematic"),
            "products": _relationship_ids(relationships, "product"),
        }
    return ships
