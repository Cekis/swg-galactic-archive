"""Index the native resource tree, distribution and client resource icons."""

from collections import defaultdict
import re
import struct

from catalog_sources import SERVER, SHARED, display_id, number, read_iff_table, source


RESOURCE_TREE = SHARED / "resource/resource_tree.txt"
DISTRIBUTION = SERVER / "resource/resource_distribution.txt"
CLIENT_TREE = "datatables/resource/resource_tree.iff"
ICON_ID = re.compile(r"[a-z0-9_]+")
POPULATION_FIELDS = ("Minimum # types", "Maximum # types", "Minimum # Pools", "Maximum # Pools")


def rows_in_tree(rows):
    """Follow resource_tree.iff's indented CLASS 1–8 hierarchy."""
    parents = [None] * 8
    for cells, evidence in rows:
        ident = str(cells.get("ENUM", "")).strip().lower()
        index = number(cells.get("INDEX"))
        if not ident or index is None or index <= 0:
            continue
        for depth in range(8):
            if name := cells.get(f"CLASS {depth + 1}"):
                break
        else:
            continue
        parent = parents[depth - 1] if depth > 0 else None
        parents[depth:] = [ident, *([None] * (7 - depth))]
        yield ident, index, name, parent, cells, evidence


def client_resource_icons(clientdata, models):
    icons = {}
    for path in sorted((clientdata / "appearance").glob("ui_res_*.spr")):
        ident = path.stem.removeprefix("ui_res_")
        if ICON_ID.fullmatch(ident) and (art := models.sprite_texture(f"appearance/{path.name}")):
            icons[ident] = art
    return icons


def icon_class(ident, parents, icons):
    """Match ResourceIconManager's direct, ancestor and unknown-class lookups."""
    visited = set()
    while ident and ident not in visited:
        if ident in icons:
            return ident
        visited.add(ident)
        ident = parents.get(ident) if ident in parents else "inorganic"
    return None


def load_resources(files, strings, clientdata, models):
    authored = list(rows_in_tree((row.cells, row.source) for row in files.tab(RESOURCE_TREE)))
    try:
        _, _, client_rows = read_iff_table(clientdata / CLIENT_TREE)
    except (OSError, ValueError, struct.error) as error:
        models.report_issue("Resources", f"Could not read client resource tree {clientdata / CLIENT_TREE}. {type(error).__name__}.")
        client_rows = []
    client = list(rows_in_tree((cells, {"path": "clientdata/" + CLIENT_TREE, "row": index})
                               for index, cells in enumerate(client_rows, 1)))
    client_by_id = {entry[0]: entry[-1] for entry in client}
    records = {}
    by_index = {}
    icons = client_resource_icons(clientdata, models)
    icon_parents = {ident: parent for ident, _, _, parent, _, _ in (client or authored)}
    strings.prefetch(("resource/resource_names", "obj_attr_n"))
    for ident, index, label, parent, cells, evidence in (*authored, *client):
        if ident in records:
            continue
        inherited = records.get(parent, {})
        group = {"organic": "Organic", "inorganic": "Inorganic", "space_mining": "Space mining"}
        category = group.get(ident, inherited.get("category", "Resource class"))
        if ident.startswith("space_"):
            category = "Space mining"
        name = strings.lookup(f"@resource/resource_names:{ident}")
        preferred_icon = icon_class(ident, icon_parents, icons)
        crate = cells.get("Resource Container Type")
        records[ident] = {
            "id": ident, "name": name["text"] or label or display_id(ident), "name_label": name,
            "index": index, "category": category, "parent": parent, "sources": [evidence],
            "class_name": label, "status": "reference_asset" if evidence["path"].startswith("clientdata/") else "authored",
            "container": crate or inherited.get("container"),
            "random_name_class": cells.get("Random Name Class") or inherited.get("random_name_class"),
            "permanent": cells.get("Permanent") in (1, "1", "true"),
            "recycled": cells.get("Recycled") in (1, "1", "true"),
            "population": {key: value for key in POPULATION_FIELDS
                           if (value := number(cells.get(key))) is not None},
            "attributes": [{"id": key, "name": strings.name("obj_attr_n", key) or display_id(key.removeprefix("res_")),
                            "min": number(cells.get(f"Att {slot} min")),
                            "max": number(cells.get(f"Att {slot} max"))}
                           for slot in range(1, 12) if (key := cells.get(f"Attribute {slot}"))],
            "icon": {"class": preferred_icon, **icons[preferred_icon]} if preferred_icon else None,
            "distribution": [], "station_prices": [],
        }
        if ident in client_by_id and evidence != client_by_id[ident]:
            records[ident]["sources"].append(client_by_id[ident])
        if records[ident]["status"] == "authored":
            by_index[index] = records[ident]

    for row in files.tab(DISTRIBUTION):
        record = by_index.get(number(row.get("Resource Index#")))
        if not record:
            continue
        for scene in row.get("Planet").strip('"').lower().split(","):
            if not (scene := scene.strip()):
                continue
            record["distribution"].append({
                "scene": scene, "pool_min": number(row.get("Pool Size Min")),
                "pool_max": number(row.get("Pool Size Max")), "source": row.source,
            })

    for path in files.paths(SHARED / "space_mining/station_price_list", "*_price_list.tab"):
        scene = "space_" + path.stem.removesuffix("_price_list")
        for row in files.tab(path):
            price = number(row.get("price_per_unit"))
            if (record := records.get(row.get("resource"))) and price is not None:
                record["station_prices"].append({"scene": scene, "price": price, "source": row.source})

    child_counts = defaultdict(int)
    for record in records.values():
        child_counts[record["parent"]] += 1
    for ident, record in records.items():
        record["child_count"] = child_counts[ident]
    return records, icons
