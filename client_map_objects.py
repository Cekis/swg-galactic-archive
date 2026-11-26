"""Read positional objects from the supplied client buildouts and snapshots."""

from collections import defaultdict
import math
from pathlib import PurePosixPath
import struct

from catalog_locations import CELL_TEMPLATE, _compose, _display_coordinates, _rotation
from catalog_models import ModelError, chunks, version
from catalog_sources import read_iff_table
from validate_buildout_templates import catalog as crc_catalog


BUILDOUT = PurePosixPath("datatables/buildout")
CRC_FILE = "misc/object_template_crc_string_table.iff"
CLIENT_ERRORS = (OSError, ValueError, KeyError, TypeError, AssertionError, struct.error)


def compile_area_rows(rows, area, area_index, names):
    """Mirror WorldSnapshot's versioned IDs and room-relative transforms."""
    known = {}
    building_id = -(area_index + 1) * 30000
    next_id = building_id + 2000
    current_building = current_cell = None
    explicit = bool(rows and "objid" in rows[0])
    for index, row in enumerate(rows, 1):
        crc = row["shared_template_crc"] & 0xffffffff
        template = names.get(crc)
        cell_index = row["cell_index"]
        portal = row.get("portal_layout_crc")
        if explicit:
            ident = row["objid"]
            parent = row["container"]
            if ident < 0:
                ident ^= (area_index + 1) << 48
            if parent < 0:
                parent ^= (area_index + 1) << 48
        elif portal or template and template.startswith(CELL_TEMPLATE):
            ident = building_id
            building_id += 1
            if portal:
                current_building, current_cell = ident, None
                parent = None
            else:
                current_cell = ident
                parent = current_building
        else:
            ident, parent = next_id, current_cell if cell_index else None
            next_id += 1

        local = tuple(row[field] for field in ("px", "py", "pz"))
        rotation = tuple(row[field] for field in ("qw", "qx", "qy", "qz"))
        if not all(math.isfinite(value) for value in (*local, *rotation)):
            continue
        transform = _rotation(rotation)
        if cell_index or parent:
            if not parent or parent not in known:
                continue
            world = _compose(known[parent], local, transform)
        else:
            world = ((float(area["x1"]) + local[0], local[1], float(area["z1"]) + local[2]), transform)
        known[ident] = world
        yield index, {
            "template": template, "template_crc": f"0x{crc:08x}" if template is None else None,
            "coordinates": _display_coordinates(world[0]), "object_id": str(ident), "area": area["area"],
            "required_event": area.get("eventRequired") or None,
            "cell_index": cell_index or None, "status": "transformed_cell" if parent else "authored_world",
            "world_transform": world, "local_coordinates": _display_coordinates(local),
            "placement_key": str(ident), "parent_key": str(parent) if parent else None,
            "portal_crc": portal & 0xffffffff if portal else None,
        }


def _buildouts(clientdata, issues):
    try:
        _, _, scenes = read_iff_table(clientdata / BUILDOUT / "buildout_scenes.iff")
    except CLIENT_ERRORS:
        issues["client_buildout_index_unavailable"] += 1
        return
    try:
        templates = crc_catalog(clientdata / CRC_FILE)
    except CLIENT_ERRORS:
        templates = {}
        issues["client_crc_index_unavailable"] += 1
    for scene_index, entry in enumerate(scenes):
        scene = entry.get("sceneName", "")
        if not scene or not scene.replace("_", "").isalnum():
            continue
        path = BUILDOUT / f"areas_{scene}.iff"
        if not (clientdata / path).is_file():
            continue
        try:
            areas = read_iff_table(clientdata / path)[2]
            for area_index, area in enumerate(areas, 100 * scene_index):
                token = area.get("area") or ""
                if not token or not token.replace("_", "").isalnum():
                    continue
                asset = BUILDOUT / scene / (token + ".iff")
                if not (clientdata / asset).is_file():
                    continue
                try:
                    columns, _, rows = read_iff_table(clientdata / asset)
                    if not {"shared_template_crc", "px", "py", "pz"}.issubset(columns):
                        continue
                    emitted = 0
                    for ordinal, point in compile_area_rows(rows, area, area_index, templates):
                        emitted += 1
                        yield {"scene": scene, "origin": "client_buildout", **point,
                               "source": {"path": "clientdata/" + asset.as_posix(), "row": ordinal}}
                    issues["client_buildout_coordinates_unresolved"] += len(rows) - emitted
                except CLIENT_ERRORS:
                    issues["unreadable_client_buildout_tables"] += 1
        except CLIENT_ERRORS:
            issues["unreadable_client_area_tables"] += 1


def snapshot_points(payload, skipped):
    """Parse WSNP/0001. Children store a transform relative to their parent node."""
    root = next(chunks(memoryview(payload)), None)
    if root is None or root.tag != "WSNP" or not root.form:
        raise ModelError("Expected a world snapshot")
    scene = version(root, "0001")
    raw_names = bytes(scene.require("OTNL").body)
    (count,) = struct.unpack_from("<I", raw_names)
    names = raw_names[4:].removesuffix(b"\0").decode("ascii").split("\0")
    if len(names) != count:
        raise ModelError("Invalid world snapshot template table")
    ordinal = 0

    def nodes(entries, parent=None, parent_id=None, depth=0):
        nonlocal ordinal
        if depth > 32:
            raise ModelError("The snapshot has an unsupported object hierarchy")
        for entry in entries:
            if entry.tag != "NODE" or not entry.form:
                continue
            data = version(entry, "0000")
            values = struct.unpack("<4i8fI", data.require("DATA").body)
            ident, _, name_index, cell_index = values[:4]
            rotation, position = values[4:8], values[8:11]
            ordinal += 1
            if not 0 <= name_index < len(names) or not all(math.isfinite(n) for n in (*rotation, *position)):
                continue
            transform = _rotation(rotation)
            world = _compose(parent, position, transform) if parent else (position, transform)
            if ident not in skipped:
                yield ordinal, {"template": names[name_index], "coordinates": _display_coordinates(world[0]),
                                "object_id": str(ident), "cell_index": cell_index or None,
                                "status": "transformed_cell" if parent else "authored_world",
                                "world_transform": world, "local_coordinates": _display_coordinates(position),
                                "placement_key": str(ident), "parent_key": str(parent_id) if parent_id is not None else None,
                                "portal_crc": values[-1] or None}
            yield from nodes(chunks(data.body), world, ident, depth + 1)

    yield from nodes(chunks(scene.require("NODS").body, limit=250_000))


def client_map_objects(clientdata, issues):
    if not clientdata or not clientdata.is_dir():
        return
    buildout_ids = defaultdict(set)
    for point in _buildouts(clientdata, issues):
        buildout_ids[point["scene"]].add(int(point["object_id"]))
        yield point
    for path in sorted((clientdata / "snapshot").glob("*.ws")):
        try:
            for index, point in snapshot_points(path.read_bytes(), buildout_ids[path.stem]):
                yield {"scene": path.stem, "origin": "client_snapshot", **point,
                       "source": {"path": "clientdata/snapshot/" + path.name, "row": index}}
        except CLIENT_ERRORS:
            issues["unreadable_client_snapshots"] += 1
