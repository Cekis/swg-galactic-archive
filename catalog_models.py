"""Read SWG display assets from the supplied, extracted client files.

The binary layouts follow the client-tools Iff, SkeletalAppearanceTemplate,
SkeletalMeshGeneratorTemplate, BasicSkeletonTemplate, and
CompressedKeyframeAnimationTemplate readers. No client runtime is needed.
"""

from __future__ import annotations

from base64 import b64encode
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
import math
from pathlib import Path, PurePosixPath
import re
import struct


ASSET_NAME = re.compile(r"[a-z0-9_/. -]+")
ANIMATION_REFERENCE = re.compile(rb"appearance/animation/[a-z0-9_./-]+\.ans", re.I)
MAX_ASSET_BYTES = 12 * 1024 * 1024
MAX_VERTICES = 32_000
# Static primitives have independent 16-bit index buffers. Their aggregate
# can exceed the per-primitive or skeletal budget.
MAX_STATIC_VERTICES = 60_000
MAX_SKELETON_SEGMENTS = 24
MAX_TRIANGLES = 45_000
MAX_TEXTURES = 12
MAX_ANIMATION_KEYS = 65_000
PASS_BLEND_OFFSETS = {
    "0000": 6, "0001": 10, "0002": 8, "0003": 8, "0004": 8, "0005": 7,
    "0006": 7, "0007": 7, "0008": 7, "0009": 7, "0010": 8,
}


class ModelError(ValueError):
    """A file is present but no usable geometry or motion could be read."""


@dataclass(frozen=True)
class Chunk:
    tag: str
    body: memoryview
    form: bool = False

    def find(self, tag):
        return next((part for part in chunks(self.body) if part.tag == tag), None)

    def require(self, tag):
        part = self.find(tag)
        if part is None:
            raise ModelError(f"Missing {tag} in {self.tag}")
        return part


def chunks(body, limit=5000):
    offset = 0
    count = 0
    while offset < len(body):
        if count >= limit or offset + 8 > len(body):
            raise ModelError("Invalid IFF chunk list")
        tag, length = struct.unpack_from(">4sI", body, offset)
        start = offset + 8
        end = start + length
        if end > len(body):
            raise ModelError("Truncated IFF chunk")
        form = tag == b"FORM"
        if form:
            if length < 4:
                raise ModelError("Incomplete IFF form")
            tag = bytes(body[start:start + 4])
            start += 4
        yield Chunk(tag.decode("ascii"), body[start:end], form)
        offset = end
        count += 1


def descendant(node, tag, depth=0):
    if node.tag == tag:
        return node
    if depth < 10 and node.form:
        for part in chunks(node.body):
            if found := descendant(part, tag, depth + 1):
                return found
    return None


def particle_mesh_refs(node, depth=0):
    """Read the appearance path authored by ParticleDescriptionMesh."""
    if node.tag == "PTMH":
        if ref := normalize_ref(value(field(node, "0000"))):
            yield ref
    elif node.form and depth < 10:
        for part in chunks(node.body):
            if part.form:
                yield from particle_mesh_refs(part, depth + 1)


def version(node, *supported):
    result = next(chunks(node.body), None)
    if result is None or not result.form or result.tag not in supported:
        raise ModelError(f"Unknown {node.tag} version")
    return result


def field(node, name):
    part = node.find(name)
    return part.body if part else memoryview(b"")


def unpack(fmt, data, offset=0):
    return struct.unpack_from("<" + fmt, data, offset)


def rows(fmt, data, maximum=MAX_VERTICES):
    size = struct.calcsize("<" + fmt)
    if len(data) % size or len(data) // size > maximum:
        raise ModelError("Unsupported array size")
    return list(struct.iter_unpack("<" + fmt, data))


def value(data):
    return bytes(data).split(b"\0", 1)[0].decode("ascii")


def names(data):
    return bytes(data).removesuffix(b"\0").decode("ascii").lower().split("\0") if data else []


def finite(values):
    if not all(math.isfinite(number) for number in values):
        raise ModelError("Asset contains non-finite values")
    return values


def compact(values):
    return [round(number, 6) for number in finite(values)]


def quat(data):
    w, x, y, z = finite(data)
    return [x, y, z, w]


def normalize_ref(ref):
    if not isinstance(ref, str) or len(ref) > 220:
        return None
    ref = ref.strip().lower().replace("\\", "/")
    if not ASSET_NAME.fullmatch(ref) or any(item in ("", ".", "..") for item in ref.split("/")):
        return None
    return ref


def mob_template(ref):
    ref = normalize_ref(ref)
    if ref and ref.endswith(".iff") and not ref.startswith(("object/", "appearance/")):
        ref = "object/mobile/" + ref
    if ref and ref.startswith("object/") and ref.endswith(".iff"):
        return ref
    return None


def object_fields(root):
    """Read explicitly set parameters and the shared-template inheritance path."""
    result = {}
    if not root.form:
        return result
    for part in chunks(root.body):
        if part.form and part.tag == "DERV":
            inherited = part.find("XXXX")
            if inherited:
                result["base"] = value(inherited.body).lower()
        elif part.form and part.tag.isdecimal():
            for parameter in chunks(part.body):
                if parameter.tag != "XXXX":
                    continue
                key, sep, rest = bytes(parameter.body).partition(b"\0")
                if sep and rest[:1] == b"\x01" and key in (
                    b"appearanceFilename", b"portalLayoutFilename", b"clientDataFile",
                ):
                    result[key.decode()] = value(rest[1:]).lower()
        elif part.form and part.tag in ("SHOT", "STOT", "SCOT", "SWOT", "SITN", "SBLD"):
            for key, entry in object_fields(part).items():
                result.setdefault(key, entry)
    return result


def skeleton(root, attachment, rig):
    if root.tag == "SLOD":
        root = version(root, "0000").require("SKTM")
    if root.tag != "SKTM":
        raise ModelError(f"Unsupported skeleton {root.tag}")
    form = version(root, "0001", "0002")
    labels = names(form.require("NAME").body)
    parents = rows("i", form.require("PRNT").body, 512)
    pre = rows("4f", form.require("RPRE").body, 512)
    post = rows("4f", form.require("RPST").body, 512)
    rest_rotation = rows("4f", form.require("BPRO").body, 512)
    rest_translation = rows("3f", form.require("BPTR").body, 512)
    count = unpack("i", form.require("INFO").body)[0]
    if count == 0 or count != len(labels) or any(len(values) != count for values in
                                               (parents, pre, post, rest_rotation, rest_translation)):
        raise ModelError("Inconsistent skeleton joint count")
    offset = len(rig)
    parent_segment = next((i for i, bone in enumerate(rig) if bone["name"] == attachment), -1)
    if attachment and parent_segment < 0:
        raise ModelError(f"Unknown skeleton attachment {attachment}")
    segment = []
    for i, label in enumerate(labels):
        parent = parents[i][0]
        if parent < -1 or parent >= i:
            raise ModelError("Invalid skeleton hierarchy")
        segment.append({
            "name": label, "parent": parent_segment if parent < 0 else offset + parent,
            "pre": quat(pre[i]), "post": quat(post[i]), "bind_rotation": quat(rest_rotation[i]),
            "bind_translation": compact(rest_translation[i]),
        })
    rig.extend(segment)


def portal_exterior(root):
    """Read the exterior appearance of portal cell zero, the parent object."""
    layout = version(root, "0001", "0002", "0003", "0004")
    cell = version(layout.require("CELS").require("CELL"), "0004", "0005")
    data = bytes(cell.require("DATA").body)
    # Both cell formats start with a 32-bit portal count and can-see-parent byte.
    _, separator, appearance = data[5:].partition(b"\0")
    if not separator or not (ref := normalize_ref(value(appearance))):
        raise ModelError("The client portal layout has no exterior appearance.")
    return ref


def expand_quaternion(packed, formats):
    def component(bits, code, sign):
        for shift, prefix in enumerate((254, 252, 248, 240, 224, 192, 128)):
            count = 1 << shift
            if prefix <= code < prefix + count:
                separation = 2 / (count + 1)
                base = -1 + (code - prefix + 1) * separation
                magnitude = (bits & (sign - 1)) * separation / (sign - 1)
                return base - magnitude if bits & sign else base + magnitude
        raise ModelError("Unsupported quaternion encoding")

    x, y, z = (component(bits, code, sign) for bits, code, sign in
               zip((packed >> 21, packed >> 10, packed), formats, (1024, 1024, 512)))
    w = math.sqrt(max(0.0, 1 - x * x - y * y - z * z))
    length = math.sqrt(x * x + y * y + z * z + w * w)
    return compact((x / length, y / length, z / length, w / length))


def animation(root, asset, label, logical, rig):
    if root.tag != "CKAT":
        raise ModelError(f"Unsupported animation {root.tag}")
    form = version(root, "0001")
    fps, frames, count, n_ar, n_sr, n_at, n_st = unpack("f6h", form.require("INFO").body)
    if not 1 <= fps <= 120 or not 1 <= frames <= 1500 or not 1 <= count <= 512:
        raise ModelError("Unsupported animation length or frame rate")
    animated_rotations = []
    static_rotations = []
    animated_translations = []
    static_translations = []
    if rotations := form.find("AROT"):
        for channel in chunks(rotations.body):
            length, x, y, z = unpack("h3B", channel.body)
            keys = rows("hI", channel.body[5:], frames + 1)
            if channel.tag != "QCHN" or len(keys) != length or not keys:
                raise ModelError("Invalid animated rotation channel")
            animated_rotations.append([[time, *expand_quaternion(packed, (x, y, z))]
                                       for time, packed in keys])
    for x, y, z, packed in rows("3BI", field(form, "SROT"), 512):
        static_rotations.append([[0, *expand_quaternion(packed, (x, y, z))]])
    if translations := form.find("ATRN"):
        for channel in chunks(translations.body):
            length = unpack("h", channel.body)[0]
            keys = rows("hf", channel.body[2:], frames + 1)
            if channel.tag != "CHNL" or len(keys) != length or not keys:
                raise ModelError("Invalid animated translation channel")
            animated_translations.append([compact(key) for key in keys])
    for (translation,) in rows("f", field(form, "STRN"), 1536):
        static_translations.append([[0, *compact((translation,))]])
    if tuple(map(len, (animated_rotations, static_rotations, animated_translations,
                      static_translations))) != (n_ar, n_sr, n_at, n_st):
        raise ModelError("Inconsistent animation channel counts")
    animated = {}
    rig_names = {bone["name"]: i for i, bone in enumerate(rig)}
    entries = list(chunks(form.require("XFRM").body))
    if len(entries) != count:
        raise ModelError("Inconsistent animation transform count")
    for entry in entries:
        joint, _, channels = bytes(entry.body).partition(b"\0")
        index = rig_names.get(joint.decode("ascii").lower())
        if index is None:
            continue
        rot, rotation, mask, x, y, z = unpack("BhB3h", channels)
        if min(rotation, x, y, z) < 0:
            raise ModelError("Invalid animation channel index")
        r = (animated_rotations if rot != 0 else static_rotations)[rotation]
        translation = [(animated_translations if mask & bit else static_translations)[channel]
                       for bit, channel in zip((8, 16, 32), (x, y, z))]
        animated[index] = [r, *translation]
    if not animated or not any(len(channel) > 1 for track in animated.values() for channel in track):
        raise ModelError("No moving joints match the model's rig")
    return {"name": label, "logical": logical, "asset": asset, "fps": fps, "frame_count": frames,
            "tracks": animated}


def zone_names(labels, entries):
    return frozenset(labels[index] for index in entries if 0 <= index < len(labels))


def mesh_part(root, asset, custom, joint_indices):
    if root.tag != "SKMG":
        raise ModelError(f"Unsupported mesh {root.tag}")
    form = version(root, "0002", "0003", "0004")
    settings = unpack("9i4h", form.require("INFO").body)
    labels = names(field(form, "OZN "))
    combinations = []
    raw = field(form, "OZC ")
    index = 0
    while index < len(raw):
        count = unpack("H", raw, index)[0]
        index += 2
        combinations.append(zone_names(labels, unpack(f"{count}H", raw, index)))
        index += count * 2
    full = zone_names(labels, (item[0] for item in rows("H", field(form, "FOZC"))[1:]))
    hides = zone_names(labels, (item[0] for item in rows("H", field(form, "ZTO "))))
    positions = [list(finite(item)) for item in rows("3f", form.require("POSN").body)]
    normals = [list(finite(item)) for item in rows("3f", field(form, "NORM"))]
    if blend := form.find("BLTS"):
        for target in chunks(blend.body):
            if target.tag != "BLT ":
                continue
            info = target.require("INFO").body
            factor = custom.get(value(info[8:]).lower(), 0) / 255
            if not factor:
                continue
            for tag, destination in (("POSN", positions), ("NORM", normals)):
                for vertex, dx, dy, dz in rows("I3f", field(target, tag)):
                    for axis, change in enumerate((dx, dy, dz)):
                        destination[vertex][axis] += change * factor
    local_joints = [joint_indices.get(name) for name in names(field(form, "XFNM"))]
    counts = rows("I", form.require("TWHD").body)
    weights = rows("If", form.require("TWDT").body, MAX_VERTICES * 8)
    if len(counts) != len(positions) or sum(count[0] for count in counts) != len(weights):
        raise ModelError("Inconsistent vertex weights")
    cursor = 0
    influences = []
    missing_joints = set()
    for (count,) in counts:
        affected = []
        for joint, weight in weights[cursor:cursor + count]:
            mapped = local_joints[joint]
            if weight > 0 and mapped is not None:
                affected.append((mapped, weight))
            elif weight > 0 and joint not in missing_joints:
                missing_joints.add(joint)
        cursor += count
        affected = sorted(affected, key=lambda pair: pair[1], reverse=True)[:4]
        total = sum(weight for _, weight in affected)
        influences.append([(joint, weight / total) for joint, weight in affected])
    return {"asset": asset, "layer": settings[-1], "hides": hides, "combinations": combinations,
            "full": full, "positions": positions, "normals": normals, "influences": influences,
            "missing_joints": len(missing_joints),
            "groups": [item for item in chunks(form.body) if item.tag == "PSDT"]}


def mesh_groups(mesh, occluded):
    if mesh["full"] and mesh["full"] <= occluded:
        return []
    combinations = mesh["combinations"]
    output = []
    for group in mesh["groups"]:
        position_indices = [entry[0] for entry in rows("I", group.require("PIDX").body[4:])]
        normal_indices = [entry[0] for entry in rows("I", field(group, "NIDX"))]
        texcoord = []
        if tex := group.find("TCSF"):
            dimensions = unpack("II", group.require("TXCI").body)[1]
            if 2 <= dimensions <= 4:
                texcoord = rows(f"{dimensions}f", field(tex, "TCSD"))
        triangles = []
        for primitive in chunks(group.require("PRIM").body):
            if primitive.tag == "ITL ":
                triangles.extend(rows("3I", primitive.body[4:], MAX_TRIANGLES))
            elif primitive.tag == "OITL":
                for zone, a, b, c in rows("h3I", primitive.body[4:], MAX_TRIANGLES):
                    if zone < 0 or zone >= len(combinations) or not combinations[zone] <= occluded:
                        triangles.append((a, b, c))
        if not triangles:
            continue
        buffers = {name: [] for name in ("positions", "normals", "uvs", "joints", "weights", "indices")}
        remapped = {}
        for triangle in triangles:
            for point in triangle:
                if point not in remapped:
                    remapped[point] = len(remapped)
                    index = position_indices[point]
                    buffers["positions"].extend(compact(mesh["positions"][index]))
                    normal = mesh["normals"][normal_indices[point]] if normal_indices else (0, 1, 0)
                    buffers["normals"].extend(compact(normal))
                    buffers["uvs"].extend(compact(texcoord[point][:2] if texcoord else (0, 0)))
                    influence = mesh["influences"][index] + [(0, 0)] * 4
                    buffers["joints"].extend(joint for joint, _ in influence[:4])
                    buffers["weights"].extend(compact([weight for _, weight in influence[:4]]))
                buffers["indices"].append(remapped[point])
        output.append({"asset": mesh["asset"], "shader": value(group.require("NAME").body), **buffers})
    return output


def static_vertices(buffer, server_copy=False):
    versioned = version(buffer, "0002", "0003")
    flags, count = unpack("Ii", versioned.require("INFO").body)
    if not 1 <= count <= MAX_VERTICES or not flags & 1 or flags & 2:
        raise ModelError("Unsupported static vertex layout")
    tex_count = (flags >> 8) & 15
    if tex_count > 8:
        raise ModelError("Too many static texture coordinate sets")
    sizes = [1 + ((flags >> (12 + 2 * i)) & 3) for i in range(tex_count)]
    offset = 3 + (3 if flags & 4 else 0)
    uv_offset = offset + int(bool(flags & 8)) + int(bool(flags & 16))
    if versioned.tag == "0003" and flags & 32:
        uv_offset += 1
    stride = uv_offset + sum(sizes)
    data = versioned.require("DATA").body
    # Version two strips its legacy DOT3 fields from the GPU format after
    # loading. Their four floats are still present at the end of each vertex.
    if len(data) == (stride + 4) * count * 4:
        stride += 4
    if server_copy and versioned.tag == "0003" and sizes == [2, 4] and len(data) == (stride - 4) * count * 4:
        stride -= 4
    if server_copy and versioned.tag == "0003" and sizes in ([2], [2, 4]) and not flags & 8:
        # Some .ssa exports retain a packed diffuse color and omit the
        # tangent set without updating the original VTXA format flags.
        if len(data) == (uv_offset + 3) * count * 4:
            uv_offset += 1
            stride = uv_offset + 2
    if len(data) != stride * count * 4:
        raise ModelError("Inconsistent static vertex buffer size")
    positions, normals, uvs = [], [], []
    for row in rows(f"{stride}f", data):
        positions.extend(compact(row[:3]))
        normals.extend(compact(row[3:6]) if flags & 4 else (0, 1, 0))
        if sizes and sizes[0] >= 2:
            # Some exported meshes leave unused UV coordinates undefined.
            # Keep their vertices, but give those texture channels finite values.
            uvs.extend(round(value, 6) if math.isfinite(value) else 0.0 for value in row[uv_offset:uv_offset + 2])
        else:
            uvs.extend((0, 0))
    return count, {"positions": positions, "normals": normals, "uvs": uvs,
                   "joints": [0] * (count * 4), "weights": [0] * (count * 4)}


def static_triangles(kind, indices):
    if kind in (3, 9):
        return indices[:len(indices) // 3 * 3]
    result = []
    for i in range(len(indices) - 2):
        if kind in (4, 10):
            tri = indices[i:i + 3]
            if i % 2:
                tri[0], tri[1] = tri[1], tri[0]
        else:
            tri = [indices[0], indices[i + 1], indices[i + 2]]
        if len(set(tri)) == 3:
            result.extend(tri)
    return result


def static_mesh(root, asset):
    root = version(root, "0002", "0003", "0004", "0005")
    shaders = version(root.require("SPS "), "0000", "0001")
    output = []
    for shader in chunks(shaders.body):
        if not shader.form or not (name := shader.find("NAME")):
            continue
        for primitive in chunks(shader.body):
            if not primitive.form or primitive.tag not in ("0000", "0001"):
                continue
            kind = unpack("i", primitive.require("INFO").body)[0]
            if kind not in (3, 4, 5, 9, 10, 11):
                continue
            count, buffers = static_vertices(primitive.require("VTXA"), asset.endswith(".ssa"))
            if kind >= 9:
                indices_data = primitive.require("INDX").body
                index_type = "H" if primitive.tag == "0001" else "I"
                indices = [index for (index,) in rows(index_type, indices_data[4:], MAX_TRIANGLES * 3)]
                if len(indices) != unpack("I", indices_data)[0] or any(index >= count for index in indices):
                    raise ModelError("Invalid static mesh indices")
            else:
                indices = list(range(count))
            result = static_triangles(kind, indices)
            if result:
                output.append({"asset": asset, "shader": value(name.body), "indices": result, **buffers})
    return output


def color(shader):
    name = PurePosixPath(shader).stem
    if "face" in name or name.endswith("_body") or "_head" in name:
        return [0.55, 0.36, 0.27]
    if "hair" in name:
        return [0.28, 0.21, 0.17]
    if "eye" in name:
        return [0.27, 0.39, 0.45]
    return [0.41, 0.48, 0.55]


def shader_alpha(static, effect):
    """Use the base pass used by ShaderImplementation::isOpaqueSolid."""
    material = {}
    reference_values = static.find("ARVS")
    references = {}
    if reference_values:
        for tag, threshold in rows("4sB", reference_values.require("0000").body, 256):
            references[tag] = threshold
    for implementation in chunks(effect.body):
        if implementation.tag != "IMPL":
            continue
        base_pass = descendant(implementation, "PASS")
        if not base_pass:
            continue
        layout = next(chunks(base_pass.body), None)
        blend_offset = PASS_BLEND_OFFSETS.get(layout.tag) if layout else None
        if blend_offset is None:
            continue
        data = field(layout, "DATA")
        flags = data[blend_offset:blend_offset + 10]
        if len(flags) != 10:
            continue
        if flags[0]:
            material["blend"] = list(flags[1:4])
            material["depth_write"] = bool(data[blend_offset - 2])
        if flags[4]:
            tag = bytes(flags[5:9])
            threshold = references.get(tag)
            if threshold is None and re.fullmatch(rb"A\d{3}", tag[::-1]):
                threshold = min(255, int(tag[::-1][1:]))
            material["alpha_test"] = {"comparison": flags[9], "reference": (threshold if threshold is not None else 128) / 255}
        return material
    return material


class ModelLibrary:
    """Resolve a catalog template to bounded JSON from read-only client assets."""

    def __init__(self, clientdata: Path, client_tools: Path | None = None, asset_roots=(), *,
                 asset_overlay: Path | None = None):
        self.root = Path(clientdata).resolve()
        self.client_tools = Path(client_tools).resolve() if client_tools else None
        candidates = [self.root]
        if asset_overlay is not None:
            candidates.insert(0, Path(asset_overlay).resolve())
        if self.client_tools:
            candidates.extend(self.client_tools / suffix for suffix in ("", "data", "data/sku.0/sys.client/built/game",
                                                                        "data/sku.0/sys.shared/built/game", "publish/clientdata"))
        candidates.extend(Path(root).resolve() for root in asset_roots)
        self.roots = tuple(dict.fromkeys(root for root in candidates if root == self.root or root.is_dir()))

    def _paths(self, ref, prefix):
        ref = normalize_ref(ref)
        if not ref or not ref.startswith(prefix):
            raise ModelError("Outside the expected client asset directory")
        for root in self.roots:
            path = (root / ref).resolve()
            if not path.is_relative_to(root):
                raise ModelError("Outside the supplied client asset root")
            yield path

    def _path(self, ref, prefix):
        paths = self._paths(ref, prefix)
        first = next(paths)
        if first.is_file():
            return first
        return next((path for path in paths if path.is_file()), first)

    def _blob(self, ref, prefix):
        path = self._path(ref, prefix)
        if path.stat().st_size > MAX_ASSET_BYTES:
            raise ModelError("Preview asset is larger than the local viewer limit")
        return path.read_bytes()

    def _form(self, ref, prefix="appearance/"):
        result = next(chunks(memoryview(self._blob(ref, prefix))), None)
        if result is None or not result.form:
            raise ModelError("No IFF form in the client asset")
        return result

    @lru_cache(maxsize=8192)
    def _object_appearance(self, ref):
        path = PurePosixPath(ref)
        shared = str(path.with_name("shared_" + path.name))
        # The normal mobile table names the server template. The exact shared
        # object template supplies the appearance and authored wearable file.
        candidates = (ref,) if path.name.startswith("shared_") else (shared, ref)
        selected = next((name for name in candidates if self._path(name, "object/").is_file()), None)
        if selected is None:
            raise FileNotFoundError
        location = selected
        visited = set()
        found = {}
        while location and location not in visited and len(visited) < 12:
            visited.add(location)
            attributes = object_fields(self._form(location, "object/"))
            for key in ("appearanceFilename", "portalLayoutFilename", "clientDataFile"):
                if attributes.get(key):
                    found.setdefault(key, attributes[key])
            if len(found) == 3:
                break
            location = attributes.get("base")
            if location and (not normalize_ref(location) or not self._path(location, "object/").is_file()):
                break
        return selected, found

    def _mesh_ref(self, ref):
        if ref.endswith(".mgn"):
            return ref
        form = self._form(ref)
        if form.tag == "MLOD":
            return value(version(form, "0000").require("NAME").body)
        raise ModelError(f"No highest-detail skeletal mesh in {ref}")

    def _wearables(self, cdf):
        if not cdf:
            return [], {}
        root = self._form(cdf, "clientdata/")
        form = version(root, "0000")
        outfits = []
        body_custom = {}
        for entry in chunks(form.body):
            if entry.tag == "WEAR" and entry.form:
                custom = {}
                meshes = []
                for item in chunks(entry.body):
                    if item.tag == "MESH":
                        meshes.append(value(item.body))
                    elif item.tag == "WCSI":
                        key = value(item.body)
                        custom[key.rsplit("/", 1)[-1].lower()] = unpack("i", item.body, len(key) + 1)[0]
                outfits.extend((mesh, custom) for mesh in meshes)
            elif entry.tag == "CSSI":
                key = value(entry.body)
                body_custom[key.rsplit("/", 1)[-1].lower()] = unpack("i", entry.body, len(key) + 1)[0]
        return outfits, body_custom

    def _material(self, shader):
        texture = None
        try:
            root = self._form(shader, "shader/")
            static = descendant(root, "SSHT")
            if not static:
                return None, {}
            static = version(static, "0000", "0001")
            if table := static.find("TXMS"):
                for entry in chunks(table.body):
                    if entry.tag != "TXM ":
                        continue
                    contents = version(entry, "0000", "0001", "0002")
                    if bytes(field(contents, "DATA")[:4]) in (b"NIAM", b"MAIN"):
                        texture = value(contents.require("NAME").body)
                        break
            if embedded := static.find("EFCT"):
                return texture, shader_alpha(static, version(embedded, "0000", "0001"))
            if reference := static.find("NAME"):
                effect = value(reference.body).replace("\\", "/").lower()
                material = shader_alpha(static, version(self._form(effect, "effect/"), "0000", "0001"))
                return texture, {"effect": effect, **material}
        except (OSError, ValueError, struct.error, StopIteration):
            pass
        return texture, {}

    def sprite_texture(self, ref):
        """Resolve the MAIN texture referenced by a client SpriteAppearance."""
        try:
            sprite = self._form(ref)
            if sprite.tag != "SPRT":
                return None
            chunk = sprite.require("0000").body
            shader = value(chunk[32:]).lower()
            if texture := self._material(shader)[0]:
                texture = normalize_ref(texture)
                if texture and self._path(texture, "texture/").is_file():
                    return {"sprite": ref, "shader": shader, "texture": texture}
        except (OSError, ValueError, struct.error):
            pass
        return None

    @lru_cache(maxsize=64)
    def _png_texture(self, ref, use_alpha):
        pixels = self.texture_png(ref, use_alpha)
        return "data:image/png;base64," + b64encode(pixels).decode("ascii") if pixels else None

    @lru_cache(maxsize=64)
    def texture_png(self, ref, use_alpha):
        try:
            from PIL import Image

            raw = self._blob(ref, "texture/")
            with Image.open(BytesIO(raw)) as image:
                if image.width * image.height > 16 * 1024 * 1024:
                    return None
                # DDS alpha can carry a specular or palette mask. Convert
                # opaque materials before filtering the RGB so that invisible
                # alpha samples do not remove their authored diffuse color.
                pixels = image.convert("RGBA" if use_alpha else "RGB")
                pixels.thumbnail((256, 256))
                file = BytesIO()
                pixels.save(file, format="PNG")
                return file.getvalue()
        except (OSError, ImportError, ModelError, ValueError):
            return None

    def _browse_animations(self, lat, rig):
        targets = (("Idle", ("loop_standing", "loop_stand", "loop_idle")),
                   ("Conversation", ("emt_conversation_1",)),
                   ("Wave", ("emt_wave2", "emt_wave1")),
                   ("Vocalize", ("emt_stand_vocalize_greet", "emt_stand_vocalize")))
        accepted = {logical for _, names in targets for logical in names}
        form = version(self._form(lat), "0000")
        references = {}
        for group in chunks(form.body):
            entry = group.find("INFO") if group.tag == "ANIM" and group.form else None
            if entry and (logical := value(entry.body)) in accepted:
                references[logical] = list(dict.fromkeys(ref.decode() for ref in
                                                        ANIMATION_REFERENCE.findall(group.body)))
        output = []
        remaining = MAX_ANIMATION_KEYS

        def add_clip(logical, label, ref):
            nonlocal remaining
            try:
                clip = animation(self._form(ref), ref, label, logical, rig)
                cost = sum(len(channel) for track in clip["tracks"].values() for channel in track)
                if cost <= remaining:
                    output.append(clip)
                    remaining -= cost
                    return True
            except (OSError, ValueError, struct.error, IndexError):
                pass
            return False

        for label, options in targets:
            logical = next((name for name in options if name in references), None)
            if logical is None:
                continue
            candidates = references[logical]
            if label == "Idle":
                candidates.sort(key=lambda ref: (0 if any(term in ref for term in
                                                         ("breathe_normally", "idl_stand")) else 1,
                                                  0 if "idl_" in ref else 1))
            for ref in candidates[:16]:
                if add_clip(logical, label, ref):
                    break
            if label == "Idle":
                moving = next((ref for ref in candidates if re.search(
                    r"_(?:walk|fly)(?:_(?:forward|front))?\.ans$", ref)), None)
                if moving:
                    add_clip(logical, "Locomotion", moving)
        return output

    def _appearance_form(self, ref, alternatives=None):
        seen = set()
        while len(seen) < 10:
            ref = normalize_ref(ref)
            if not ref:
                raise ModelError("Invalid client appearance reference.")
            if not ref.startswith("appearance/"):
                ref = "appearance/" + ref
            if ref in seen:
                break
            seen.add(ref)
            root = self._form(ref)
            if root.tag == "APT ":
                ref = value(version(root, "0000").require("NAME").body)
            elif root.tag == "DTLA":
                # Modern LODs embed an APPR with its own DATA before the LOD
                # table. The CHLD list is directly in the version form.
                detail_form = version(root, *(f"{n:04d}" for n in range(1, 9)))
                data = detail_form.find("DATA")
                children = {
                    unpack("i", child.body)[0]: value(child.body[4:])
                    for child in chunks(data.body) if child.tag == "CHLD"
                } if data and data.form else {}
                distances = [(near, far, ident) for ident, near, far in rows("iff", field(detail_form, "INFO"), 128)
                             if children.get(ident) and math.isfinite(near) and math.isfinite(far)]
                if not distances:
                    raise ModelError("No authored detail meshes in the client appearance.")
                ref, *other_levels = (children[ident] for _, _, ident in sorted(distances))
                if alternatives is not None:
                    alternatives[:0] = other_levels
            elif root.tag == "PEFT":
                meshes = list(dict.fromkeys(particle_mesh_refs(root)))
                if not meshes:
                    return root, ref
                ref, *other_meshes = meshes
                if alternatives is not None:
                    alternatives[:0] = other_meshes
            elif root.tag == "PRTO":
                ref = portal_exterior(root)
            elif root.tag == "LSAT":
                ref = value(field(version(root, "0000"), "BASE"))
            else:
                return root, ref
        raise ModelError("Circular client appearance references.")

    def get(self, template: str, profile: dict | None = None) -> dict:
        ref = normalize_ref(template)
        if ref and ref.startswith("appearance/") and PurePosixPath(ref).suffix in {
            ".sat", ".apt", ".lod", ".msh", ".pob", ".cmp", ".ssa", ".mgn", ".lmg", ".lsb", ".prt", ".spr",
        }:
            return self._direct_appearance(ref)
        ref = mob_template(ref)
        if ref is None:
            return {"status": "missing", "message": "This entry has no client object template."}
        return self._model(ref)

    def _mesh_fallback(self, ref, original=None, alternatives=None):
        # The extracted client retains geometry for a number of APPR-only
        # wrappers. Prefer the exact named mesh before trying a base appearance.
        stem = PurePosixPath(ref).stem
        same_name = re.sub(r"_hue(?=_|$)", "", stem).removeprefix("pv_")
        original_stem = PurePosixPath(original or ref).stem
        for relative in dict.fromkeys((f"appearance/{stem}.ssa", f"appearance/{original_stem}.ssa",
                         f"appearance/mesh/{stem}.lmg", f"appearance/mesh/{stem}.mgn", f"appearance/mesh/{stem}_l0.mgn",
                         f"appearance/mesh/{stem}.msh", f"appearance/lod/{stem}.lod", f"appearance/{stem}.apt",
                         f"appearance/{same_name}.sat", f"appearance/droid_{stem}.sat")):
            if relative == ref or not self._path(relative, "appearance/").is_file():
                continue
            other_levels = []
            try:
                root, asset = self._appearance_form(relative, other_levels)
                if root.tag == "MLOD":
                    asset = self._mesh_ref(asset)
                    root = self._form(asset)
                if root.tag in {"SKMG", "MESH", "SMAT", "CMPA"}:
                    return root, asset
            except (OSError, ValueError, struct.error, IndexError):
                pass
            finally:
                if alternatives is not None:
                    alternatives.extend(other_levels)
        raise ModelError("The available appearance contains bounds, with no matching compiled render mesh.")

    def _model_form(self, ref, alternatives=None):
        try:
            root, asset = self._appearance_form(ref, alternatives)
        except FileNotFoundError:
            return self._mesh_fallback(ref, alternatives=alternatives)
        if root.tag == "APPR":
            return self._mesh_fallback(asset, ref, alternatives)
        if root.tag == "MLOD":
            asset = self._mesh_ref(asset)
            root = self._form(asset)
        return root, asset

    @lru_cache(maxsize=8192)
    def available(self, ref):
        """Check that the supplied assets provide a renderable appearance chain."""
        try:
            asset = normalize_ref(ref)
            if not asset:
                return False
            if not asset.startswith("appearance/"):
                template = mob_template(asset)
                if not template:
                    return False
                _, info = self._object_appearance(template)
                asset = info.get("appearanceFilename") or info.get("portalLayoutFilename")
            return bool(asset and self._model_form(asset)[0].tag in {"MESH", "SMAT", "SKMG", "CMPA"})
        except (OSError, ValueError, struct.error, IndexError):
            return False

    @lru_cache(maxsize=12)
    def _model(self, ref):
        try:
            selected, fields = self._object_appearance(ref)
        except FileNotFoundError:
            return {"status": "missing", "template": ref, "message": "The matching client object template is unavailable."}
        except (OSError, ValueError, struct.error, IndexError):
            return {"status": "unsupported", "template": ref, "message": "The client object template could not be read."}
        appearance = fields.get("appearanceFilename") or fields.get("portalLayoutFilename")
        result = {"template": ref, "client_template": selected, "appearance": appearance}
        if not appearance:
            return {**result, "status": "missing", "message": "The compiled template has no appearance reference."}
        return self._preview(result, fields.get("clientDataFile"))

    @lru_cache(maxsize=8)
    def _direct_appearance(self, ref):
        return self._preview({"template": ref, "appearance": ref}, None)

    def _preview(self, result, cdf):
        appearance = result["appearance"]
        first_failure = None
        pending = [appearance]
        attempted = set()
        while pending and len(attempted) < 16:
            candidate = pending.pop(0)
            if candidate in attempted:
                continue
            attempted.add(candidate)
            alternatives = []
            detail = dict(result)
            if candidate != appearance:
                detail["model_selection"] = "Authored lower-detail mesh"
            try:
                root, asset = self._model_form(candidate, alternatives)
                preview = self._assemble(detail, cdf, root, asset)
                if preview["status"] == "available":
                    return preview
            except FileNotFoundError:
                preview = {**detail, "status": "missing", "message": "A referenced client appearance asset is unavailable."}
            except ModelError as error:
                preview = {**detail, "status": "unsupported", "message": str(error)}
            except (ValueError, struct.error, OSError, IndexError):
                preview = {**detail, "status": "unsupported", "message": "The client appearance could not be decoded."}
            if first_failure is None:
                first_failure = preview
            pending[:0] = alternatives
        return first_failure

    def _assemble(self, result, cdf, root, source_asset):
        if root.tag == "PEFT":
            raise ModelError("This appearance is a particle effect with no authored model mesh.")
        if root.tag == "SPRT":
            sprite = self.sprite_texture(source_asset)
            pixels = self.texture_png(sprite["texture"], True) if sprite else None
            if not pixels:
                raise ModelError("The in-game sprite texture is unavailable.")
            return {**result, "status": "available", "geometry_kind": "sprite", "sprite": sprite,
                    "sprite_image": "data:image/png;base64," + b64encode(pixels).decode("ascii")}
        if result["appearance"].endswith(".lsb"):
            result["model_selection"] = "Authored lightsaber hilt"
        elif result["appearance"].endswith(".prt"):
            result["model_selection"] = "Authored particle mesh. Particle effects are omitted from this preview."
        if source_asset != result["appearance"] and root.tag == "SKMG":
            result["recovered_geometry"] = source_asset
        if root.tag == "MESH":
            output = static_mesh(root, source_asset)
            return self._finalize(result, output, [], [], [], max_vertices=MAX_STATIC_VERTICES)
        if root.tag == "CMPA":
            return self._finalize(result, self._component_meshes(root, source_asset, ()), [], [], [],
                                  max_vertices=MAX_STATIC_VERTICES)
        if root.tag == "SKMG":
            return self._standalone_skeletal(result, root, source_asset)
        if root.tag != "SMAT":
            raise ModelError(f"Preview geometry is unavailable for {root.tag} appearances.")
        form = version(root, "0002", "0003")
        components = names(form.require("MSGN").body)
        skeleton_paths = names(form.require("SKTI").body)
        if not 1 <= len(components) <= 24 or len(skeleton_paths) % 2 or len(skeleton_paths) > 2 * MAX_SKELETON_SEGMENTS:
            raise ModelError("Unsupported appearance component count")
        rig = []
        issues = []
        for skt, attachment in zip(skeleton_paths[::2], skeleton_paths[1::2]):
            try:
                skeleton(self._form(skt), attachment, rig)
            except (OSError, ModelError, struct.error, IndexError):
                issues.append(f"Could not read skeleton segment {skt}")
        outfits = []
        custom = {}
        if cdf:
            try:
                outfits, custom = self._wearables(cdf)
                result["client_data"] = cdf
            except (OSError, ModelError, struct.error, IndexError):
                issues.append(f"Could not read authored wearables from {cdf}")
        if len(outfits) > 20:
            raise ModelError("Too many wearable components for the model preview")
        joint_indices = {bone["name"]: index for index, bone in enumerate(rig)}
        parts = []
        for component, modifications in [(base, custom) for base in components] + outfits:
            try:
                asset = self._mesh_ref(component)
                part = mesh_part(self._form(asset), asset, modifications, joint_indices)
                if part["missing_joints"]:
                    issues.append(f"{asset} refers to {part['missing_joints']} unavailable joints")
                parts.append(part)
            except (OSError, ModelError, struct.error, IndexError) as error:
                issues.append(f"Could not decode {component}: {error}" if isinstance(error, ModelError)
                              else f"Could not decode {component}")
        layers = defaultdict(list)
        for part in parts:
            layers[part["layer"]].append(part)
        occluded = frozenset()
        output = []
        total_vertices = total_triangles = 0
        for _, layer in sorted(layers.items(), reverse=True):
            layer_hides = frozenset()
            for part in layer:
                try:
                    groups = mesh_groups(part, occluded)
                except (ModelError, struct.error, IndexError):
                    issues.append(f"Could not read shader primitives for {part['asset']}")
                    continue
                for group in groups:
                    total_vertices += len(group["positions"]) // 3
                    total_triangles += len(group["indices"]) // 3
                if total_vertices > MAX_VERTICES or total_triangles > MAX_TRIANGLES:
                    raise ModelError("Too much geometry for the model preview")
                if groups:
                    output.extend(groups)
                    layer_hides |= part["hides"]
            occluded |= layer_hides
        lat = names(field(form, "LATX")[2:])
        clips = []
        if len(lat) >= 2 and rig:
            try:
                clips = self._browse_animations(lat[1], rig)
            except (OSError, ValueError, struct.error, IndexError):
                issues.append(f"Could not read the logical animation table {lat[1]}")
        return self._finalize(result, output, rig, clips, issues)

    def _standalone_skeletal(self, result, root, asset):
        stem = re.sub(r"_l\d+$", "", PurePosixPath(asset).stem)
        rig, clips = [], []
        for name in dict.fromkeys((stem, stem.removesuffix("_hue"), "pv_" + stem)):
            try:
                skeleton(self._form(f"appearance/skeleton/{name}.skt"), "", rig)
                break
            except (OSError, ValueError, struct.error, IndexError):
                rig.clear()
        if rig:
            for name in (stem, "pv_" + stem):
                reference = f"appearance/lat/{name}.lat"
                try:
                    if clips := self._browse_animations(reference, rig):
                        break
                except (OSError, ValueError, struct.error, IndexError):
                    continue
        part = mesh_part(root, asset, {}, {bone["name"]: i for i, bone in enumerate(rig)})
        issues = ["The client wrapper contains bounds only. The preview uses its matching compiled mesh."]
        if not rig:
            issues.append("The matching mesh has no recoverable authored animation rig.")
        return self._finalize(result, mesh_groups(part, frozenset()), rig, clips, issues)

    def _component_meshes(self, root, ref, ancestors):
        if ref in ancestors or len(ancestors) >= 8:
            raise ModelError("Circular component appearance references.")
        definition = version(root, "0002", "0003", "0004", "0005")
        output = []
        vertices = triangles = 0
        for part in chunks(definition.body):
            if part.tag != "PART":
                continue
            name = value(part.body)
            transform = finite(unpack("12f", part.body, len(name) + 1))
            child = name if name.startswith("appearance/") else "appearance/" + name
            try:
                form, asset = self._model_form(child)
                if form.tag == "CMPA":
                    groups = self._component_meshes(form, asset, (*ancestors, ref))
                elif form.tag == "MESH":
                    groups = static_mesh(form, asset)
                else:
                    continue
            except (OSError, ValueError, struct.error, IndexError):
                continue
            for group in groups:
                for key in ("positions", "normals"):
                    values = group[key]
                    translated = key == "positions"
                    group[key] = compact([sum(transform[j * 4 + k] * values[i + k] for k in range(3))
                                          + (transform[j * 4 + 3] if translated else 0)
                                          for i in range(0, len(values), 3) for j in range(3)])
                vertices += len(group["positions"]) // 3
                triangles += len(group["indices"]) // 3
                if vertices > MAX_STATIC_VERTICES or triangles > MAX_TRIANGLES:
                    raise ModelError("Too much component geometry for the model preview.")
                output.append(group)
        return output

    def _finalize(self, result, output, rig, clips, issues, *, max_vertices=MAX_VERTICES,
                  max_triangles=MAX_TRIANGLES):
        if not output:
            return {**result, "status": "unsupported", "message": "The appearance has no decoded renderable mesh.",
                    "issues": issues}
        vertices = sum(len(group["positions"]) // 3 for group in output)
        triangles = sum(len(group["indices"]) // 3 for group in output)
        if vertices > max_vertices or triangles > max_triangles:
            raise ModelError("Too much geometry for the model preview.")
        textures = {}
        material_by_shader = {}
        for group in output:
            shader = group["shader"]
            group["color"] = color(shader)
            if shader not in material_by_shader:
                texture, material = self._material(shader)
                uses_alpha = "blend" in material or "alpha_test" in material
                key = texture + "#alpha" if texture and uses_alpha else texture
                if key and key not in textures and len(textures) < MAX_TEXTURES:
                    if png := self._png_texture(texture, uses_alpha):
                        textures[key] = png
                material_by_shader[shader] = (texture, key if key in textures else None, material)
            group["texture"], group["texture_key"], group["material"] = material_by_shader[shader]
        return {**result, "status": "available", "meshes": output, "textures": textures, "rig": rig,
                "animations": clips, "vertices": vertices, "triangles": triangles, "issues": issues}
