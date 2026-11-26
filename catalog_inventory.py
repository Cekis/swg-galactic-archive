"""Publish item, ship, schematic and resource records with reversible links."""

from collections import Counter

from catalog_entities import client_object_inventory, item_ref
from catalog_item_scripts import ItemScripts, describe_storage
from catalog_resources import load_resources
from catalog_schematics import load_schematics
from catalog_ships import load_ships
from catalog_sources import display_id


def crosslink(writer, left, right, detail=None):
    kind, ident, role = left
    other_kind, other_id, reverse = right
    writer.link(kind, ident, role, other_kind, other_id, detail)
    writer.link(other_kind, other_id, reverse, kind, ident, detail)


def link_recipe_items(writer, items, ident, recipe, resources):
    products = set()
    for output in recipe["outputs"]:
        item = items.resolve_item(output.get("item_id"))
        if not item:
            continue
        record = items.ensure(item)
        output["catalog_id"] = item
        role = "recipe_references"
        if output.get("status") in ("authored", "explicit_table"):
            role = "crafted_by"
            products.add(item)
            record["status"] = "authored"
        proof = output.get("source")
        if proof and proof not in record["sources"]:
            record["sources"].append(proof)
        crosslink(writer, ("schematics", ident, "products"), ("items", item, role), output)
    for slot in recipe["slots"]:
        for option in slot["options"]:
            for ingredient in option["ingredients"]:
                detail = {"slot": slot["name"], "count": ingredient.get("count"),
                          "source": ingredient.get("source")}
                if (resource := ingredient.get("resource_id")) in resources:
                    crosslink(writer, ("schematics", ident, "resources"),
                              ("resources", resource, "schematics"), detail)
                    ingredient["resource_link"] = resource
                if item := items.resolve_item(ingredient.get("item_id")):
                    items.ensure(item)
                    crosslink(writer, ("schematics", ident, "ingredients"),
                              ("items", item, "used_in_recipes"), detail)
                    ingredient["catalog_id"] = item
    return products


def publish_schematics(writer, items, schematics, resources):
    craftable = set()
    for ident, recipe in sorted(schematics.items()):
        schematic_item = items.ensure(ident)
        if not schematic_item["sources"]:
            schematic_item["sources"].extend(recipe["sources"])
        if recipe.get("name"):
            schematic_item["name"] = recipe["name"]
        crosslink(writer, ("schematics", ident, "schematic_item"), ("items", ident, "schematic"))
        products = set()
        for definition in (recipe, *recipe["table_recipes"]):
            products.update(link_recipe_items(writer, items, ident, definition, resources))
        craftable.update(products)
        for entry in recipe["limited_uses"]:
            if item := items.resolve_item(entry.get("item_id") or entry.get("raw_item")):
                items.ensure(item)
                entry["catalog_id"] = item
                crosslink(writer, ("schematics", ident, "learned_from"),
                          ("items", item, "teaches_schematics"), entry)
        if station := recipe["station"]:
            for field in ("compatible_tools", "compatible_stations"):
                for entry in station.get(field, []):
                    if item := items.resolve_item(entry.get("item_id")):
                        items.ensure(item)
                        entry["catalog_id"] = item
                        writer.link("schematics", ident, "crafting_tools", "items", item, entry)
        names = [items.records[output["catalog_id"]]["name"] for output in recipe["outputs"]
                 if "catalog_id" in output]
        writer.entity("schematics", ident, recipe["name"], recipe, category=display_id(recipe["category"]),
                      status=recipe["status"], search=names + [grant["skill_id"] for grant in recipe["grants"]],
                      card={"slots": len(recipe["slots"]), "products": len(products),
                            "station": (recipe.get("station") or {}).get("category")})
    return craftable


def ship_profiles(ships):
    for ident, ship in sorted(ships.items()):
        yield ident, ship
        for variant in ship.get("variants", []):
            variant_id = variant["id"]
            if variant_id != ident and variant_id not in ships:
                sources = variant["sources"] + [entry for entry in ship["sources"] if entry not in variant["sources"]]
                yield variant_id, {**ship, **variant, "variants": [], "chassis_id": ident, "sources": sources}


def publish_ships(writer, items, ships, schematics, components):
    counts = Counter()
    for ident, ship in ship_profiles(ships):
        for field, role in (("deeds", "ship_deeds"), ("products", "ship_products"),
                            ("control_devices", "ship_devices")):
            for raw in ship.get(field, []):
                if item := items.resolve_item(raw):
                    items.ensure(item)
                    crosslink(writer, ("ships", ident, field), ("items", item, role))
        for raw in ship.get("schematics", []):
            if raw in schematics:
                crosslink(writer, ("ships", ident, "schematics"), ("schematics", raw, "ships"))
        for entry in ship.get("relationships", []):
            if entry["kind"] != "schematic" and (item := items.resolve_item(entry["item"])):
                entry["catalog_id"] = item
            if item := items.resolve_item(entry.get("static_item")):
                items.ensure(item)
                crosslink(writer, ("ships", ident, "deeds"), ("items", item, "ship_deeds"), entry)
        for slot in ship.get("slots", []):
            raw = slot.get("default_component")
            template, proof = components.get(raw, (raw, None))
            if item := items.resolve_item(template):
                items.ensure(item)
                slot["catalog_id"] = item
                slot["catalog_source"] = proof
                crosslink(writer, ("ships", ident, "components"), ("items", item, "ship_components"),
                          {"slot": slot["id"], "source": proof})
        if chassis := ship.get("chassis_id"):
            crosslink(writer, ("ships", ident, "chassis"), ("ships", chassis, "variants"))
        status = ship.get("availability") or ship.get("status") or "authored"
        counts[status] += 1
        writer.entity("ships", ident, ship["name"], ship, category=ship["category"], status=status,
                      level=ship["attributes"].get("chassis_tier"),
                      search=[ship.get("template"), ship.get("chassis_id")],
                      card={"slots": len(ship.get("slots", [])), "template": ship.get("template"),
                            "model_asset": ship.get("model_asset")})
    return counts


def publish_resources(writer, items, resources, world):
    for ident, resource in sorted(resources.items()):
        if parent := resource["parent"]:
            crosslink(writer, ("resources", ident, "parent"), ("resources", parent, "children"))
        if container := items.resolve_item(resource["container"]):
            items.ensure(container)
            crosslink(writer, ("resources", ident, "containers"), ("items", container, "holds_resources"))
        scenes = set()
        for field, role in (("distribution", "resources"), ("station_prices", "station_resources")):
            for occurrence in resource[field]:
                scene = occurrence["scene"]
                world(scene)
                scenes.add(scene)
                crosslink(writer, ("resources", ident, field), ("locations", scene, role), occurrence)
        writer.entity("resources", ident, resource["name"], resource, category=resource["category"],
                      status=resource["status"], search=[resource["class_name"], resource["index"]], scenes=scenes,
                      card={"icon": (resource["icon"] or {}).get("class"), "children": resource["child_count"],
                            "parent": resources.get(resource["parent"], {}).get("name"),
                            "worlds": len(scenes), "index": resource["index"]})


def publish_items(writer, items, craftable):
    for ident, record in list(items.records.items()):
        if (template := item_ref(record["template"])) and template != ident:
            items.ensure(template)
            crosslink(writer, ("items", ident, "base_item"), ("items", template, "variants"))
    for deed, deployed, proof in items.deployments:
        items.ensure(deed)
        crosslink(writer, ("items", deed, "deploys"), ("items", deployed, "deeds"), proof)
    items.finish()
    for ident, item in sorted(items.records.items()):
        acquisitions = Counter(entry["kind"] for entry in item["acquisitions"] if not entry.get("development"))
        form, storage = describe_storage(item)
        item.update(form=form, script_destinations=dict(storage))
        writer.entity("items", ident, item["name"], item, category=item["category"], level=item["level"],
                      status=item["status"], search=[item["template"], item["client_template"]] if item["template"] else [],
                      card={"template": item["template"], "has_source": bool(item["sources"]),
                            "crafted": ident in craftable, "acquisition": acquisitions, "form": form, "storage": storage})


def publish_inventory(writer, files, strings, items, world, clientdata, models):
    inventory = client_object_inventory(clientdata)
    items.parse_templates()
    items.from_master()
    items.from_client(inventory)
    items.from_acquisitions()
    components = items.from_ship_components()
    print("Indexing resources and original client icons", flush=True)
    resources, icons = load_resources(files, strings, clientdata, models)
    print("Indexing schematic recipes and player ship chassis", flush=True)
    schematics = load_schematics(files, strings, clientdata, inventory)
    ships = load_ships(files, strings, clientdata, model=models)
    craftable = publish_schematics(writer, items, schematics, resources)
    ship_counts = publish_ships(writer, items, ships, schematics, components)
    publish_resources(writer, items, resources, world)
    print("Tracing item creation and storage in game scripts", flush=True)
    script_sources = ItemScripts(files, items).index()
    publish_items(writer, items, craftable)
    return {"resource_icons": icons, "inventory": {
        "items_by_source": dict(Counter(item["status"] for item in items.records.values())),
        "schematics_by_source": dict(Counter(recipe["status"] for recipe in schematics.values())),
        "ships_by_source": dict(ship_counts), "authored_craftable_outputs": len(craftable),
        "resources_with_icons": sum(bool(resource["icon"]) for resource in resources.values()),
        "script_acquisitions": script_sources,
    }}
