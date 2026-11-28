"""Resolve the client terrain environment families used for outdoor audio."""

from dataclasses import dataclass
from math import sqrt
import struct

from catalog_models import chunks, descendant, unpack, value, version


def _header(part):
    data = version(part.require("IHDR"), "0000", "0001").require("DATA").body
    return bool(unpack("i", data)[0]), value(data[4:])


def _feather(kind, amount):
    if kind == 1:
        return amount ** 2
    if kind == 2:
        return sqrt(amount)
    if kind == 3:
        return (3 - 2 * amount) * amount ** 2
    return amount


def _nearest_distance(x, z, points):
    closest = float("inf")
    for (ax, az), (bx, bz) in zip(points, (*points[1:], points[0])):
        dx, dz = bx - ax, bz - az
        length = dx * dx + dz * dz
        along = max(0, min(1, ((x - ax) * dx + (z - az) * dz) / length)) if length else 0
        closest = min(closest, (ax + along * dx - x) ** 2 + (az + along * dz - z) ** 2)
    return sqrt(closest)


@dataclass(frozen=True, slots=True)
class Boundary:
    kind: str
    points: tuple
    feather: int
    feather_distance: float

    @property
    def center(self):
        if self.kind == "circle":
            return self.points[:2]
        if self.kind == "rectangle":
            left, bottom, right, top = self.points
            return (left + right) / 2, (bottom + top) / 2
        x, z = zip(*self.points)
        return sum(x) / len(x), sum(z) / len(z)

    def coverage(self, x, z):
        distance = self.feather_distance
        if self.kind == "circle":
            cx, cz, radius = self.points
            delta_squared = (cx - x) ** 2 + (cz - z) ** 2
            radius_squared = radius ** 2
            if delta_squared > radius_squared:
                return 0
            inner_squared = (radius * (1 - distance)) ** 2
            amount = 1 if delta_squared <= inner_squared else (radius_squared - delta_squared) / (radius_squared - inner_squared)
        elif self.kind == "rectangle":
            left, bottom, right, top = self.points
            nearest_edge = min(x - left, right - x, z - bottom, top - z)
            if nearest_edge < 0:
                return 0
            feather_width = 0.5 * min(right - left, top - bottom) * distance
            amount = min(1, nearest_edge / feather_width) if feather_width > 0 else 1
        else:
            points = self.points
            inside = sum(
                (az <= z < bz or bz <= z < az) and x < ax + (bx - ax) * (z - az) / (bz - az)
                for (ax, az), (bx, bz) in zip(points, (*points[1:], points[0]))
            ) % 2
            if not inside:
                return 0
            amount = min(1, _nearest_distance(x, z, points) / distance) if distance > 0 else 1
        return _feather(self.feather, amount)


def _boundary(tag, part):
    data = part.require("DATA").body
    if tag == "BCIR" and part.tag in ("0001", "0002"):
        x, z, radius, feather, distance = unpack("3fif", data)
        return Boundary("circle", (x, z, radius), feather, max(0, min(1, distance)))
    if tag == "BREC" and part.tag in ("0002", "0003", "0004"):
        x0, z0, x1, z1, feather, distance = unpack("4fif", data)
        return Boundary("rectangle", (min(x0, x1), min(z0, z1), max(x0, x1), max(z0, z1)),
                        feather, max(0, min(1, distance)))
    if tag == "BPOL" and part.tag == "0007":
        count, = unpack("i", data)
        if 3 <= count <= (len(data) - 12) // 8:
            points = tuple(struct.iter_unpack("<2f", data[4:4 + count * 8]))
            feather, distance = unpack("if", data, 4 + count * 8)
            return Boundary("polygon", points, feather, distance)
    return None


@dataclass(frozen=True, slots=True)
class Layer:
    bounds: tuple
    unknown_bounds: bool
    filters: bool
    invert_bounds: bool
    invert_filters: bool

    def coverage(self, x, z):
        lower = max((bound.coverage(x, z) for bound in self.bounds), default=0)
        upper = 1 if self.unknown_bounds or not self.bounds else lower
        if not self.bounds and not self.unknown_bounds:
            lower = 1
        if self.invert_bounds:
            lower, upper = 1 - upper, 1 - lower
        if not upper:
            return 0, 0
        inside = lower > 0
        if self.filters:
            lower = 0
        if self.invert_filters:
            lower, upper = (1 - upper if inside else 0), 1 - lower
        return lower, upper


@dataclass(frozen=True, slots=True)
class EnvironmentRule:
    family: str
    label: str
    weight: float
    layers: tuple


@dataclass(frozen=True, slots=True)
class TerrainAudio:
    rules: tuple

    @property
    def anchors(self):
        return tuple(dict.fromkeys(bound.center for rule in self.rules for layer in rule.layers for bound in layer.bounds))

    def at(self, x, z):
        possible = {}
        for rule in self.rules:
            lower = upper = 1
            for layer in rule.layers:
                minimum, maximum = layer.coverage(x, z)
                lower *= minimum
                upper *= maximum
                if not upper:
                    break
            if lower > 0 and lower >= rule.weight:
                possible = {rule.family: (rule.label, False)}
            elif upper > 0 and upper >= rule.weight and rule.family not in possible:
                possible = {family: (label, True) for family, (label, _) in possible.items()}
                possible[rule.family] = (rule.label, True)
        return possible


def _rules(layer, families, parents=()):
    part = version(layer, "0002", "0003", "0004")
    enabled, name = _header(part)
    if not enabled:
        return
    invert_bounds, invert_filters = unpack("2i", part.require("ADTA").body)
    bounds, children, affectors = [], [], []
    filters = unknown_bounds = False
    for child in chunks(part.body):
        if child.tag == "LAYR":
            children.append(child)
        elif child.form and child.tag.startswith(("B", "F", "AENV")):
            desc = next(chunks(child.body))
            if not _header(desc)[0]:
                continue
            if child.tag.startswith("B"):
                boundary = _boundary(child.tag, desc)
                if boundary is None:
                    unknown_bounds = True
                else:
                    bounds.append(boundary)
            elif child.tag.startswith("F"):
                filters = True
            else:
                family, override, weight = unpack("2if", desc.require("DATA").body)
                if family in families:
                    key, default_weight = families[family]
                    affectors.append((key, weight if override else default_weight))
    layers = (*parents, Layer(tuple(bounds), unknown_bounds, filters, bool(invert_bounds), bool(invert_filters)))
    for key, weight in affectors:
        yield EnvironmentRule(key, name, weight, layers)
    for child in children:
        yield from _rules(child, families, layers)


def read_terrain_audio(path):
    root = next(chunks(memoryview(path.read_bytes())))
    group = descendant(root, "EGRP")
    layers = descendant(root, "LYRS")
    if group is None or layers is None:
        return TerrainAudio(())
    families = {}
    for family in chunks(version(group, "0000", "0001", "0002").body):
        data = family.require("DATA").body
        name = value(data[4:])
        weight, = unpack("f", data, 8 + len(name))
        families[unpack("i", data)[0]] = name.casefold(), weight
    return TerrainAudio(tuple(rule for layer in chunks(layers.body) if layer.tag == "LAYR"
                              for rule in _rules(layer, families)))
