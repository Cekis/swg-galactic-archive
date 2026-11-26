"""Resolve inventory models and label previews made from surviving game assets."""

import math

from catalog_buildings import ASSET_ERRORS, box_vertices, collision_geometry, floor_mesh, portal_layout
from catalog_models import compact, descendant, normalize_ref


# The hangar-ship props and compiled display assets depict named chassis.
# Keep the associations variant-specific, even when flight templates share a hull.
SHIP_DISPLAYS = {
    ship: f"appearance/{asset}_model.ssa"
    for asset, ships in {
        "a_wing": "awing",
        "arc170": "arc170",
        "blacksun_fighter_heavy": "blacksun_heavy_s01 blacksun_heavy_s02",
        "blacksun_fighter_heavy_02": "blacksun_heavy_s03 blacksun_heavy_s04",
        "blacksun_fighter_light": "blacksun_light_s01 blacksun_light_s02",
        "blacksun_fighter_light_02": "blacksun_light_s03 blacksun_light_s04",
        "blacksun_fighter_medium": "blacksun_medium_s01 blacksun_medium_s02",
        "blacksun_fighter_medium_02": "blacksun_medium_s03 blacksun_medium_s04",
        "blacksun_transport": "gunship_neutral",
        "bwing": "bwing",
        "corellian_corvette": "corellian_corvette corvette",
        "droid_fighter": "droid_fighter_s01",
        "grievous_starship": "grievous_starship",
        "hutt_fighter_heavy": "hutt_heavy_s01",
        "hutt_fighter_heavy_02": "hutt_heavy_s02",
        "hutt_fighter_heavy_s03": "hutt_turret_ship",
        "hutt_fighter_light": "hutt_light_s01 basic_hutt_light prototype_hutt_light",
        "hutt_fighter_light_02": "hutt_light_s02",
        "hutt_fighter_medium": "hutt_medium_s01",
        "hutt_fighter_medium_02": "hutt_medium_s02",
        "imperial_gunboat": "gunship_imperial",
        "jedifighter": "jedi_starfighter",
        "kse_firespray": "firespray",
        "lambda_shuttle": "lambda",
        "naboo_starfighter": "naboo_n1",
        "rebel_gunboat": "gunship_rebel",
        "soorosuub_space_yacht": "sorosuub_space_yacht",
        "star_destroyer": "star_destroyer",
        "tie_advanced": "tieadvanced tieadvanced_modified",
        "tie_aggressor": "tieaggressor",
        "tie_bomber": "tiebomber",
        "tie_fighter": "tiefighter basic_tiefighter prototype_tiefighter tie_light_duty",
        "tie_fighter_modified": "tiefighter_modified",
        "tie_interceptor": "tieinterceptor",
        "tie_interceptor_imperial_guard": "tieinterceptor_imperial_guard",
        "tie_oppressor": "tieoppressor",
        "v_wing": "vwing",
        "vrt49": "decimator",
        "xwing": "xwing advanced_xwing",
        "y8": "y8_mining_ship",
        "ykl37r": "ykl37r",
        "yt1300": "yt1300 yt1300_decorated_01",
        "yt2400": "yt2400",
        "ywing": "ywing ywing_longprobe",
        "z95": "z95 advanced_z95 basic_z95 prototype_z95",
    }.items()
    for ship in ships.split()
}

# collection/rewards.tab points to the correct template for this reward.
ITEM_TEMPLATE_ALIASES = {
    "object/tangible/collection/reward/potted_planet_reward_01.iff":
        "object/tangible/collection/reward/potted_plant_reward_01.iff",
}

MAX_FLOOR_TRIANGLES = 60_000
BOX_FACES = (
    (0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
    (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3),
)


def model_refs(kind, profile):
    refs = [profile.get(key) for key in ("client_template", "shared_template", "model_asset",
                                         "layout_asset", "template", "appearance")]
    if kind == "locations":
        refs[:0] = [path.removeprefix("clientdata/") for proof in profile.get("sources", [])
                    if (path := proof.get("path", "")).startswith("clientdata/object/")]
    refs = [ref for raw in refs if (ref := normalize_ref(raw)) and ref.startswith(("object/", "appearance/"))]
    if kind == "items":
        refs += [ITEM_TEMPLATE_ALIASES[ref] for ref in refs if ref in ITEM_TEMPLATE_ALIASES]
    return tuple(dict.fromkeys(refs))


def preview_mesh(asset, vertices, faces):
    normals = [[0., 0., 0.] for _ in vertices]
    for a, b, c in faces:
        origin = vertices[a]
        x, y, z = (end - start for start, end in zip(origin, vertices[b]))
        u, v, w = (end - start for start, end in zip(origin, vertices[c]))
        normal = (y * w - z * v, z * u - x * w, x * v - y * u)
        for index in (a, b, c):
            for axis, value in enumerate(normal):
                normals[index][axis] += value
    directions = []
    for normal in normals:
        length = math.hypot(*normal)
        directions.extend(compact([value / length for value in normal]) if length else (0, 1, 0))
    count = len(vertices)
    return {
        "asset": asset, "shader": "",
        "positions": compact([n for point in vertices for n in point]), "normals": directions,
        "indices": [i for face in faces for i in face], "uvs": [0] * (count * 2),
        "joints": [0] * (count * 4), "weights": [0] * (count * 4),
    }


def floor_groups(models, layout):
    cells, _ = portal_layout(models._form(layout))
    for cell in cells[1:]:
        if asset := cell["floor"]:
            try:
                yield preview_mesh(asset, *floor_mesh(models._form(asset)))
            except ASSET_ERRORS:
                continue


def geometry_preview(models, appearance):
    root, asset = models._appearance_form(appearance)
    collision = descendant(root, "CMPT") or descendant(root, "CMSH")
    groups = [preview_mesh(asset, points, faces) for points, faces in collision_geometry(collision)
              if faces] if collision else []
    if groups:
        return asset, groups, "Client collision mesh preview"
    box = descendant(root, "EXBX")
    if box and (points := box_vertices(box)) and points[0][1] < points[-1][1]:
        return asset, [preview_mesh(asset, points, BOX_FACES)], "Client appearance bounds"
    return asset, [], ""


def render_inventory_model(models, kind, ident, profile):
    refs = model_refs(kind, profile)
    attempted = list(refs)
    failures = []
    for ref in refs:
        model = models.get(ref)
        if model["status"] == "available":
            return {**model, "chosen": ref, "attempted": attempted}
        failures.append(model)
    if kind == "ships" and (display := SHIP_DISPLAYS.get(ident.removeprefix("player_"))):
        attempted.append(display)
        model = models.get(display)
        if model["status"] == "available":
            return {**model, "chosen": display, "geometry_kind": "display",
                    "model_selection": "In-game display model. The flight mesh is unavailable.",
                    "attempted": attempted}
    appearances = tuple(dict.fromkeys(model["appearance"] for model in failures if model.get("appearance")))
    if profile.get("form") != "intangible":
        result = fallback_geometry(models, appearances, profile.get("layout_mismatch"))
        if result:
            return {**result, "attempted": attempted}
    failure = next((model for model in failures if model.get("appearance")), failures[0] if failures else {})
    return {"status": "missing", "message": "No authored client model is linked to this entry.",
            **failure, "attempted": attempted}


def render_model_profile(models, kind, ident, profile, variant=""):
    if kind != "npcs":
        if variant:
            raise ValueError("Unknown appearance choice.")
        return render_inventory_model(models, kind, ident, profile)
    ref = (profile.get("model_asset") or profile.get("client_template") or
           profile.get("shared_template") or profile.get("template"))
    choices = profile.get("model_choices") or []
    if variant:
        if variant not in choices:
            raise ValueError("Unknown appearance choice.")
        ref = variant
    if not ref:
        return {"status": "missing", "attempted": profile.get("model_attempts", []),
                "message": "No authored client model is linked to this entry."}
    result = models.get(ref, profile)
    for choice in choices if not variant and result.get("status") != "available" else ():
        if choice != ref:
            preview = models.get(choice, profile)
            if preview.get("status") == "available":
                result, ref = preview, choice
                break
    return {**result, "chosen": ref, "choices": choices, "model_source": profile.get("model_source"),
            "model_selection": profile.get("model_selection"), "missing_hull_model": profile.get("missing_hull_model"),
            "attempted": profile.get("model_attempts", [])}


def fallback_geometry(models, appearances, layout_mismatch):
    bounds = None
    for appearance in appearances:
        if appearance.endswith(".pob") and not layout_mismatch:
            try:
                floors = list(floor_groups(models, appearance))
                if floors:
                    return models._finalize(
                        {"appearance": appearance, "geometry_kind": "floors", "model_selection":
                         "Client interior floor meshes. The exterior render mesh is unavailable."},
                        floors, [], [], [], max_triangles=MAX_FLOOR_TRIANGLES,
                    )
            except ASSET_ERRORS:
                pass
        try:
            asset, groups, label = geometry_preview(models, appearance)
            if groups:
                fallback = {"appearance": asset, "geometry_kind": "collision" if label.startswith("Client collision") else "bounds",
                            "model_selection": f"{label}. The render mesh is unavailable."}
                if fallback["geometry_kind"] == "collision":
                    return models._finalize(fallback, groups, [], [], [])
                bounds = bounds or (fallback, groups)
        except ASSET_ERRORS:
            continue
    if bounds:
        return models._finalize(*bounds, [], [], [])
    return None
