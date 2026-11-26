"""Inventory the native files and rendered results used by archive routes."""

from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from functools import cache
from hashlib import sha256
import json
from pathlib import Path

from catalog_db import packed
from catalog_locations import MapLibrary
from catalog_model_previews import model_refs, render_model_profile
from catalog_models import ModelLibrary, normalize_ref
from catalog_sources import GAME_MODEL_PATHS


def digest(value):
    return sha256(packed(value).encode("utf-8")).hexdigest()


class PayloadDigest(str):
    """A digest computed before discarding a reusable playback payload."""


def shrink_result(result):
    """Hash the large payloads while retaining the fields used during lookup."""
    big = ("meshes", "textures", "rig", "animations", "sprite_image")
    return {key: digest(value) if key in big and not isinstance(value, PayloadDigest) else value
            for key, value in result.items()}


class AssetTrace(ModelLibrary):
    def __init__(self, clientdata, tools, game, overlay):
        super().__init__(clientdata, tools, (game / path for path in GAME_MODEL_PATHS), asset_overlay=overlay)
        self.used = set()
        self.texture_results = {}
        self.animation_results = {}

    @cache
    def _path(self, ref, prefix):
        path = super()._path(ref, prefix)
        if path.is_file():
            self.used.add((normalize_ref(ref), path))
        return path

    @cache
    def _material(self, shader):
        return super()._material(shader)

    @cache
    def texture_png(self, ref, use_alpha):
        png = super().texture_png(ref, use_alpha)
        # Retain the PNG's identity and success state without holding every
        # encoded image in memory while traversing the model catalog.
        token = sha256(png).digest() if png else None
        self.texture_results[f"{ref}|{'rgba' if use_alpha else 'rgb'}"] = token.hex() if token else None
        return token

    def _browse_animations(self, lat, rig):
        key = lat, digest(rig)
        if key not in self.animation_results:
            clips = super()._browse_animations(lat, rig)
            self.animation_results[key] = PayloadDigest(digest(clips)) if clips else []
        return self.animation_results[key]

    @cache
    def preview_result(self, appearance, cdf):
        return shrink_result(super()._preview({"appearance": appearance}, cdf))

    def _preview(self, result, cdf):
        return {**result, **self.preview_result(result["appearance"], cdf)}

    @cache
    def decoded_template(self, template):
        return super().get(template)

    def get(self, template, profile=None):
        return self.decoded_template(template)


def audit_routes(catalog, library, *, kind_filter=None, images=True):
    """Trace every default model route, NPC variant, map and resource icon."""
    model_results, map_results, resource_results = {}, {}, {}
    statuses, route_counts = Counter(), Counter()
    inventory = {}
    next_progress = 5_000
    with catalog.connect() as db:
        for kind, ident, raw in db.execute("""
            SELECT kind,id,data FROM entity
            WHERE (kind IN ('items','npcs','ships') OR (kind='locations' AND category='Building'))
                AND (? IS NULL OR kind=?)
            ORDER BY kind,id
        """, (kind_filter, kind_filter)):
            profile = json.loads(raw)
            variants = ["", *(profile.get("model_choices") or [])] if kind == "npcs" else [""]
            for variant in dict.fromkeys(variants):
                identity = packed([kind, ident, variant])
                if kind in ("items", "locations"):
                    key = kind, model_refs(kind, profile), profile.get("form"), bool(profile.get("layout_mismatch"))
                    if key not in inventory:
                        result = render_model_profile(library, kind, ident, profile)
                        inventory[key] = result["status"], digest(result)
                    status, checksum = inventory[key]
                else:
                    result = render_model_profile(library, kind, ident, profile, variant)
                    status, checksum = result["status"], digest(result)
                statuses[status] += 1
                model_results[identity] = checksum
                route_counts[kind] += 1
            if (count := len(model_results)) >= next_progress:
                print(f"{kind_filter or 'Archive'}: {count:,} model routes checked", flush=True)
                next_progress += 5_000

    if images:
        metadata = catalog.meta()
        maps = MapLibrary(library.root)
        for ident, record in sorted(metadata["maps"].items()):
            for thumbnail in (False, True):
                image = maps.map_image(record, thumbnail=thumbnail)
                map_results[f"{ident}|{'thumbnail' if thumbnail else 'full'}"] = sha256(image[1]).hexdigest() if image else None
            if ref := record.get("asset"):
                path = (library.root / ref).resolve()
                if path.is_relative_to(library.root) and path.is_file():
                    library.used.add((ref, path))

        for name, icon in sorted(metadata["resource_icons"].items()):
            token = library.texture_png(icon["texture"], True)
            resource_results[name] = token.hex() if token else None

    report = {
        "models": model_results, "maps": map_results, "resource_icons": resource_results,
        "textures": library.texture_results, "model_status": dict(statuses), "model_routes": dict(route_counts),
    }
    print(f"{kind_filter or 'Archive'}: {len(model_results):,} model routes checked", flush=True)
    return report


def _audit_job(database, client, tools, game, overlay, kind):
    from catalog_db import Catalog

    library = AssetTrace(client, tools, game, overlay)
    report = audit_routes(Catalog(database), library, kind_filter=kind, images=kind == "ships")
    return report, library.used


def audit_in_parallel(database, client, tools, game, overlay):
    sections = ("items", "npcs", "locations", "ships")
    report = {"models": {}, "maps": {}, "resource_icons": {}, "textures": {},
              "model_status": Counter(), "model_routes": Counter()}
    used = set()
    with ProcessPoolExecutor(max_workers=len(sections)) as pool:
        jobs = [pool.submit(_audit_job, database, client, tools, game, overlay, kind) for kind in sections]
        for job in jobs:
            part, files = job.result()
            used.update(files)
            for key, values in part.items():
                if key == "textures":
                    for ref in report[key].keys() & values.keys():
                        if report[key][ref] != values[ref]:
                            raise ValueError(f"Inconsistent decoding for {ref}")
                report[key].update(values)
    return report, used


def compare_audits(expected, actual):
    for category in ("models", "maps", "resource_icons", "textures"):
        left, right = expected[category], actual[category]
        if left != right:
            changed = [key for key in sorted(left.keys() | right.keys()) if left.get(key) != right.get(key)
                       or key not in left or key not in right]
            raise ValueError(f"{len(changed):,} {category} outputs changed: {changed[:8]}")


def asset_destinations(files, client, tools, game, overlay):
    """Translate resolved source files to their same-priority package locations."""
    roots = ((overlay, Path(".local/model-assets")), (client, Path("clientdata")),
             (tools, Path("client-tools")), (game, Path("game")))
    output = {}
    for ref, path in sorted(files):
        for root, dest in roots:
            if path.is_relative_to(root):
                relative = (dest / path.relative_to(root)).as_posix().lower()
                if not relative.endswith("/" + ref):
                    raise ValueError(f"Unexpected native asset spelling for {path}")
                if relative in output and output[relative] != (ref, path):
                    raise ValueError(f"Conflicting package asset {relative}")
                output[relative] = ref, path
                break
        else:
            raise ValueError(f"Unhandled model asset root for {path}")
    return dict(sorted(output.items()))
