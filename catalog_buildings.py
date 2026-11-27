"""Index placed buildings and client geometry for the game map."""

from collections import Counter, defaultdict
from functools import lru_cache
from itertools import product
import math
from pathlib import PurePosixPath
import re
import struct

from catalog_locations import CELL_TEMPLATE, IDENTITY, _compose, _display_coordinates, _rotate
from catalog_models import ModelError, ModelLibrary, chunks, descendant, field, normalize_ref, rows, unpack, value, version
from catalog_sources import SERVER_OBJECTS, SHARED_OBJECTS, display_id, source


BUILDING_TEMPLATES = ("object/building/", "object/static/structure/")
ASSET_ERRORS = (OSError, ValueError, IndexError, struct.error)
SHARED_TEMPLATE = re.compile(r'^\s*sharedTemplate\s*=\s*"([^"]+)"', re.M)
BASE_TEMPLATE = re.compile(r'^\s*@base[ \t]+(object/[a-zA-Z0-9_/-]+\.iff)', re.M)
BUILD_RADIUS = re.compile(r'^[ \t]*noBuildRadius[ \t]*=[ \t]*([+-]?\d+(?:\.\d+)?)', re.M)
ARCHITECTURAL = re.compile(
    r"(?:^|_)(?:building|house|housing|hut|shack|shed|tent|warehouse|bungalow|dome|shelter|garage|tower"
    r"|hangar|barracks|facade|bunker|cantina|palace|clinic|hotel|shop|library|capitol|hall|factory|lodge)(?:_|$)"
)


def is_building(template):
    return template.startswith(BUILDING_TEMPLATES[0]) or (
        template.startswith(BUILDING_TEMPLATES[1]) and ARCHITECTURAL.search(PurePosixPath(template).stem)
    )


def shared_name(template):
    path = PurePosixPath(template)
    return str(path.with_name(path.name.removeprefix("shared_")))

def template_source(directory, template):
    return directory.parent / PurePosixPath(template).with_suffix(".tpf")


def box_vertices(box):
    xmax, ymax, zmax, xmin, ymin, zmin = unpack("6f", field(version(box, "0000", "0001"), "BOX "))
    axes = ((xmin, xmax), (ymin, ymax), (zmin, zmax))
    if all(math.isfinite(n) for axis in axes for n in axis) and xmax > xmin and zmax > zmin:
        return list(product(*axes))
    return []


def indexed_geometry(mesh):
    mesh = version(mesh, "0000")
    return validated_mesh(rows("3f", field(mesh, "VERT")), rows("3i", field(mesh, "INDX")))


def collision_geometry(node, depth=0):
    if depth > 8:
        return []
    if node.tag == "CMSH":
        return [indexed_geometry(version(node, "0000").require("IDTL"))]
    if node.tag == "EXBX":
        return [(box_vertices(node), None)]
    if node.tag == "XCYL":
        data = next(chunks(version(node, "0000").body))
        x, y, z, radius, height = unpack("5f", data.body)
        if math.isfinite(radius) and radius > 0 and all(math.isfinite(n) for n in (x, y, z, height)):
            return [([(x + radius * math.cos(angle), y + h, z + radius * math.sin(angle))
                      for h in (0, height) for angle in (n * math.tau / 20 for n in range(20))], None)]
    if node.tag in ("CMPT", "CPST"):
        children = list(chunks(version(node, "0000").body))
        parts = [collision_geometry(child, depth + 1) for child in children]
        return [part for group in parts for part in group] if all(parts) else []
    return []


def bounds_of(points):
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def hull(points):
    ordered = sorted(set(points))

    def side(sequence):
        result = []
        for x, z in sequence:
            while len(result) >= 2:
                (ax, az), (bx, bz) = result[-2:]
                if (bx - ax) * (z - az) - (bz - az) * (x - ax) > 0:
                    break
                result.pop()
            result.append((x, z))
        return result[:-1]

    return side(ordered) + side(reversed(ordered))


def project(vertices, transform):
    (x, _, z), rotation = transform
    return [(round(x + px, 3), round(z + pz, 3)) for px, _, pz in (_rotate(rotation, point) for point in vertices)]


def projected_faces(points, faces):
    for a, b, c in faces:
        triangle = [points[a], points[b], points[c]]
        (ax, az), (bx, bz), (cx, cz) = triangle
        winding = (bx - ax) * (cz - az) - (bz - az) * (cx - ax)
        if winding:
            yield triangle if winding > 0 else triangle[::-1]


def validated_mesh(vertices, faces):
    if any(not math.isfinite(n) for point in vertices for n in point):
        raise ModelError("Non-finite building geometry")
    if not vertices or not faces or any(i < 0 or i >= len(vertices) for face in faces for i in face):
        raise ModelError("Invalid building triangle index")
    return vertices, faces


def portal_layout(root):
    if root.tag != "PRTO":
        raise ModelError("Expected a portal layout")
    layout = version(root, "0000", "0001", "0002", "0003", "0004")
    result = []
    for index, node in enumerate(node for node in chunks(layout.require("CELS").body) if node.tag == "CELL"):
        cell = version(node, "0004", "0005")
        data = bytes(cell.require("DATA").body)
        portals = unpack("I", data)[0]
        name, appearance, extra = data[5:].split(b"\0", 2)
        floor = value(extra[1:]) if extra[:1] == b"\x01" else None
        result.append({"index": index, "name": name.decode("ascii"),
                       "appearance": appearance.decode("ascii").lower(), "floor": floor, "portals": portals})
    checksum = layout.find("CRC ")
    return result, unpack("I", checksum.body)[0] if checksum else None


def floor_mesh(root):
    if root.tag != "FLOR":
        raise ModelError("Expected a floor mesh")
    floor = version(root, "0003", "0004", "0005", "0006")
    verts, tris = floor.require("VERT").body, floor.require("TRIS").body
    vertices = rows("3f", verts[4:], 120_000)
    count = unpack("I", tris)[0]
    if len(vertices) != unpack("I", verts)[0] or count < 1 or count > 120_000:
        raise ModelError("Unexpected floor mesh size")
    stride, remainder = divmod(len(tris) - 4, count)
    if stride < 12 or remainder:
        raise ModelError("Unexpected floor triangle data")
    return validated_mesh(vertices, [unpack("3i", tris, 4 + i * stride) for i in range(count)])


class BuildingAssets(ModelLibrary):
    def __init__(self, spatial):
        super().__init__(spatial.clientdata, report_issue=spatial.report_issue)
        self.spatial = spatial
        self.files = spatial.files

    @lru_cache(maxsize=2048)
    def building(self, template):
        server = template_source(SERVER_OBJECTS, template)
        hint = SHARED_TEMPLATE.search(self.files.text(server)) if self.files.exists(server) else None
        try:
            shared, fields = self._object_appearance(hint[1] if hint and normalize_ref(hint[1]) else template)
        except ASSET_ERRORS:
            shared, fields = None, {}
        portal = fields.get("portalLayoutFilename")
        if not portal and (resolved := self.spatial._portal_rooms(template)):
            portal = resolved[0]
        appearance = fields.get("appearanceFilename")
        cells, checksum = [], None
        if portal:
            try:
                cells, checksum = portal_layout(self._form(portal))
            except ASSET_ERRORS:
                pass
            if cells and not appearance:
                appearance = cells[0]["appearance"]
        return {"shared": shared, "portal": portal, "cells": cells, "checksum": checksum,
                "appearance": appearance}

    @lru_cache(maxsize=800)
    def footprint(self, appearance, bounds_only=False):
        if not appearance:
            return None
        try:
            asset = appearance
            for _ in range(8):
                root = self._form(asset)
                if root.tag != "APT ":
                    break
                asset = value(version(root, "0000").require("NAME").body)
            else:
                return None
            if not bounds_only:
                try:
                    if geometry := self._outline_geometry(root, asset):
                        return geometry
                except ASSET_ERRORS:
                    pass
            box = descendant(root, "EXBX")
            if box and (vertices := box_vertices(box)):
                return "Client appearance bounds", asset, [(vertices, None)]
        except ASSET_ERRORS:
            pass
        return None

    @staticmethod
    def _outline_geometry(root, asset):
        radar = descendant(root, "RADR")
        if radar and unpack("i", field(radar, "INFO"))[0] and (mesh := radar.find("IDTL")):
            return "Client radar shape", asset, [indexed_geometry(mesh)]
        # Some supplied APPR records retain collision geometry alone.
        collision = descendant(root, "CMPT") or descendant(root, "CMSH")
        if collision and (parts := collision_geometry(collision)):
            if sum(len(faces) if faces else len(vertices) for vertices, faces in parts) <= 1800:
                return "Client collision geometry", asset, parts
        return None

    @lru_cache(maxsize=384)
    def cell_floor(self, asset):
        try:
            return floor_mesh(self._form(asset)) if asset else None
        except ASSET_ERRORS:
            return None


def world_footprint(geometry, transform):
    if not geometry:
        return None
    _, _, parts = geometry
    paths = []
    for vertices, faces in parts:
        points = project(vertices, transform)
        if faces:
            # Vertical walls collapse in an overhead projection. Remove
            # identical top and bottom triangles before extracting outlines.
            polygons = {tuple(sorted(path)): path for path in projected_faces(points, faces)}
            paths.extend(polygons.values())
        elif points:
            paths.append(hull(points))
    return footprint_paths(paths)


def footprint_paths(paths):
    paths = [path for path in paths if len(path) >= 3]
    if not paths:
        return None
    bounds = bounds_of([point for path in paths for point in path])
    edges = Counter(tuple(sorted((path[index - 1], point))) for path in paths for index, point in enumerate(path))
    return {"shape": "polygon", "paths": paths, "bounds": bounds,
            "edges": [edge for edge, count in edges.items() if count == 1]}


def floor_footprint(rooms):
    paths = {}
    for room in rooms:
        if floor := room["floor"]:
            points = [tuple(point) for point in floor["points"]]
            for path in projected_faces(points, floor["faces"]):
                paths.setdefault(tuple(sorted(path)), path)
    return footprint_paths(paths.values())


class BuildingIndex:
    def __init__(self, spatial, items, npcs):
        self.assets = BuildingAssets(spatial) if spatial.clientdata else None
        self.spatial, self.items, self.npcs = spatial, items, npcs
        self.points = defaultdict(dict)
        self.restrictions = {}
        self.coverage = Counter()

    def record(self, point, origin):
        template = point.get("template") or ""
        key = point.get("placement_key")
        if (origin == "server_buildout" and template and point["coordinates"] and not point.get("parent_key") and
                (restriction := self._no_build(template))):
            self.restrictions[point["scene"], key] = (point, restriction)
        if key is None or not (is_building(template) or point.get("parent_key")):
            return
        records = self.points[point["scene"]]
        if key not in records:
            records[key] = {**point, "origin": origin}
        elif not records[key]["coordinates"] and point["coordinates"]:
            # Keep the server row and use a matching client's recovered transform.
            if records[key]["parent_key"] == point.get("parent_key") and records[key]["cell_index"] == point.get("cell_index"):
                records[key].update(coordinates=point["coordinates"], world_transform=point["world_transform"],
                                    parent_source=point["source"], status=point["status"])

    @lru_cache(maxsize=16384)
    def _no_build(self, template):
        files = self.spatial.files
        server = template_source(SERVER_OBJECTS, template)
        hint = SHARED_TEMPLATE.search(files.text(server)) if files.exists(server) else None
        ref = normalize_ref(hint[1]) if hint else None
        path = PurePosixPath(template)
        ref = ref or str(path.with_name("shared_" + path.name.removeprefix("shared_")))
        seen = set()
        while ref and ref not in seen and len(seen) < 16:
            seen.add(ref)
            path = template_source(SHARED_OBJECTS, ref)
            if not files.exists(path):
                return None
            text = files.text(path)
            if radius := BUILD_RADIUS.search(text):
                amount = float(radius[1])
                return (amount, source(path, 1 + text.count("\n", 0, radius.start()))) if amount > 0 else None
            base = BASE_TEMPLATE.search(text)
            ref = base[1] if base else None
        return None

    @lru_cache(maxsize=8192)
    def _object_label(self, template):
        stem = PurePosixPath(template).stem.removeprefix("shared_")
        if template.startswith("object/mobile/") and stem in self.npcs:
            return self.npcs[stem]["name"], ["npcs", stem]
        if template and (ident := self.items.resolve_item(template)) and (item := self.items.records.get(ident)):
            return item["name"], ["items", ident]
        return display_id(stem) if stem else "Unnamed object", None

    def _contents(self, children, parents, scripts, transform):
        stack = list(parents)
        direct = {point["placement_key"] for point in parents}
        visited = set()
        contents = []
        for point in stack:
            key = point["placement_key"]
            if key in visited:
                continue
            visited.add(key)
            template = point.get("template") or ""
            if template.startswith(CELL_TEMPLATE):
                continue
            stack.extend(children.get(key, ()))
            label, target = self._object_label(template)
            contents.append({"name": label, "target": target, "template": template or point.get("template_crc"),
                             "coordinates": point["coordinates"], "local_coordinates": point.get("local_coordinates"),
                             "coordinate_frame": "cell" if key in direct else "parent",
                             "source": point["source"], "origin": point["origin"],
                             **({"spawner": point["spawner"]} if point.get("spawner") else {}),
                             "required_event": point.get("required_event")})
        for point in scripts:
            ref, mob = point["spawn_ref"], point["mob_id"]
            label, target = self._object_label(ref if "/" in ref or ref in self.items.records else
                                               ("object/mobile/" + ref + ".iff"))
            if mob in self.npcs:
                label, target = self.npcs[mob]["name"], ["npcs", mob]
            coordinates = point["coordinates"]
            if coordinates is None and transform and point["local_coordinates"] is not None:
                coordinates = _display_coordinates(_compose(transform, point["local_coordinates"], IDENTITY)[0])
            contents.append({"name": point.get("name") or label, "target": target, "template": ref,
                             "coordinates": coordinates, "local_coordinates": point["local_coordinates"],
                             "source": point["source"], "origin": "building_spawn",
                             "spawn_origin": point.get("spawn_origin"), "required_event": point.get("required_event")})
        return sorted(contents, key=lambda row: row["name"].casefold())

    def _rooms(self, building, authored, layout, children, spawns):
        placed = {room["cell_index"]: room for room in authored if room.get("cell_index")}
        declared = {room["index"]: room for room in layout if room["index"]}
        scripts = defaultdict(list)
        for point in spawns:
            scripts[point["room"].casefold()].append(point)
        room_names = Counter(room["name"].casefold() for room in declared.values())
        rooms = []
        for index in sorted(placed.keys() | declared.keys()):
            room, record = declared.get(index, {}), placed.get(index, {})
            name = room.get("name") or "Cell " + str(index)
            # Portal-defined rooms without a placement row use the building's transform.
            transform = record.get("world_transform") or building["world_transform"]
            floor = None
            if room and (mesh := self.assets.cell_floor(room["floor"])):
                vertices, faces = mesh
                floor = {"points": project(vertices, transform), "faces": faces}
            parents = children.get(record.get("placement_key"), ())
            matches = scripts.pop(name.casefold(), ()) if room_names[name.casefold()] == 1 else ()
            inventory = self._contents(children, parents, matches, transform)
            rooms.append({"index": index, "name": name, "portals": room.get("portals"),
                          "floor": floor, "floor_asset": room.get("floor"), "appearance": room.get("appearance"),
                          "object_id": record.get("runtime_object_id") or record.get("object_id"),
                          "origin": _display_coordinates(transform[0]), "source": record.get("source"), "contents": inventory})
        unresolved = {rows_[0]["room"]: self._contents(children, (), rows_, None) for rows_ in scripts.values()}
        return rooms, unresolved

    def publish(self, writer):
        for (scene, _), (point, (radius, proof)) in self.restrictions.items():
            x, _, z = point["coordinates"]
            label, _ = self._object_label(point["template"])
            writer.object_area(scene, {
                "layer": "no_build", "name": label, "no_build": True, "context": "Object placement restriction",
                "description": f"The placed {label} has an authored no-build radius of {radius:g} meters.",
                "coordinates": point["coordinates"], "template": point["template"], "source": point["source"],
                "geometry_kind": "Authored no-build radius", "geometry_source": proof,
                "extent": {"shape": "circle", "values": [x, z, radius], "bounds": [x - radius, z - radius, x + radius, z + radius]},
            })
        self.coverage["object_no_build_zones"] = len(self.restrictions)
        all_spawns = defaultdict(list)
        for point in self.spatial.building_spawn_points:
            all_spawns[point["scene"], point["building_key"]].append(point)
        for scene, records in self.points.items():
            children = defaultdict(list)
            for point in records.values():
                if point.get("parent_key"):
                    children[point["parent_key"]].append(point)
            for key, point in records.items():
                template = point.get("template") or ""
                if is_building(template) and point["coordinates"] and point["world_transform"]:
                    self._publish_building(writer, scene, key, point, children, all_spawns.get((scene, key), ()))
        return dict(self.coverage)

    def _publish_building(self, writer, scene, key, point, children, spawns):
        template = point["template"]
        authored = [child for child in children[key] if (child.get("template") or "").startswith(CELL_TEMPLATE)]
        profile = self.assets.building(template) if self.assets else {}
        layout, crc = profile.get("cells", []), point.get("portal_crc")
        mismatch = bool(crc is not None and profile.get("checksum") is not None and crc & 0xffffffff != profile["checksum"])
        enterable = bool(authored or len(layout) > 1 or spawns)
        if not enterable and (profile.get("portal") and not layout or not ARCHITECTURAL.search(PurePosixPath(template).stem)):
            return
        appearance = profile.get("appearance")
        geometry = self.assets.footprint(appearance) if self.assets else None
        extent = world_footprint(geometry, point["world_transform"])
        if not extent and geometry and geometry[0] != "Client appearance bounds":
            geometry = self.assets.footprint(appearance, bounds_only=True)
            extent = world_footprint(geometry, point["world_transform"])
        cells, unresolved = self._rooms(point, authored, [] if mismatch else layout, children, spawns) if enterable else ([], {})
        if not extent and enterable and layout and not mismatch:
            exterior = layout[0]["floor"]
            if exterior and (floor := self.assets.cell_floor(exterior)):
                geometry = "Client exterior floor", exterior, [floor]
                extent = world_footprint(geometry, point["world_transform"])
        if not extent and enterable:
            extent = floor_footprint(cells)
            if extent:
                geometry = "Mapped interior footprint", profile["portal"], None
        template_name, _ = self._object_label(template)
        name = point.get("label") or template_name
        ident = f"{scene}/building/{key}"
        summary = {
            "name": name, "coordinates": point["coordinates"], "enterable": enterable, "source": point["source"],
            "template": template, "object_id": point.get("runtime_object_id") or point.get("object_id"),
            "required_event": point.get("required_event"),
            "context": "Building with interior cells" if enterable else "Scenery building",
        }
        if geometry and extent:
            label, asset, _ = geometry
            summary.update(extent=extent, geometry_kind=label, geometry_source={"path": "clientdata/" + asset})
        if enterable:
            summary.update(target=["locations", ident], cell_count=len(cells),
                           description=f"{name} has {len(cells)} indexed interior cells.",
                           layer="building_enterable")
            sources = [point["source"]]
            if point.get("parent_source"):
                sources.append(point["parent_source"])
            sources.extend({"path": "clientdata/" + profile[ref]} for ref in ("shared", "portal") if profile.get(ref))
            data = {**summary, "id": ident, "scene": scene, "category": "Building", "cells": cells,
                    "unresolved_cells": unresolved, "layout_asset": profile.get("portal"), "layout_mismatch": mismatch,
                    "description": {"text": summary["description"], "sources": sources}, "sources": sources,
                    "marker": {"coordinates": point["coordinates"]}, "bounds": extent["bounds"] if extent else None}
            writer.entity("locations", ident, name, data, category="Building", scene=scene,
                          search=[template], card={"has_map": True, "cells": len(cells)})
            writer.link("locations", scene, "buildings", "locations", ident)
            writer.link("locations", ident, "world", "locations", scene)
            self.coverage["buildings"] += 1
            self.coverage["cells"] += len(cells)
            self.coverage["cell_contents"] += sum(len(cell["contents"]) for cell in cells)
            if mismatch:
                self.coverage["mismatched_portal_layouts"] += 1
        else:
            summary["layer"] = "building_filler"
        if extent:
            writer.object_area(scene, summary)
            self.coverage[summary["layer"] + "_outlines"] += 1
        else:
            self.coverage["unavailable_footprints"] += 1
