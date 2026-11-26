"""Resolve authored monster populations, spawner tables and space squads."""

from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import PurePosixPath
import re

from catalog_locations import _numeric, _object_id, _vector
from catalog_sources import SERVER


TABLE_REF = re.compile(r"datatables/[\w/-]+(?:\.iff)?")
MANGLED = re.compile(r"^(strSpawns|strWaves)_mangled\.segment\.(\d+)$")


def table_ref(value):
    if TABLE_REF.fullmatch(value):
        return value if value.endswith(".iff") else value + ".iff"
    return None


def _mangled(settings, name):
    parts = sorted((int(match[2]), value) for key, value in settings.items()
                   if (match := MANGLED.fullmatch(key)) and match[1] == name)
    return [word for word in "".join(value for _, value in parts).split(":") if word]


def authored_count(row, *columns):
    return next((count for key in columns if (count := _object_id(row.get(key))) and count > 0), None)


class SpawnTables:
    def __init__(self, files, spatial, entry):
        self.files, self.spatial, self.entry = files, spatial, entry
        self.squads = {row.get("strSquadName"): row for row in spatial._table(SERVER / "space_content/spawners/squads.tab")
                       if row.get("strSquadName")}
        self.waves = tuple(spatial._table(SERVER / "space_content/spawners/waves.tab"))

    @lru_cache(maxsize=4096)
    def lair(self, name):
        if not re.fullmatch(r"[\w/-]+", name):
            return ()
        path = SERVER / "npc_lair" / f"{name}.tab"
        rows = self.spatial._table(path)
        # A surviving lair table has two columns named bossMonster. Preserve
        # both source cells even though normal tab rows are keyed by header.
        bosses = ()
        if rows:
            header = self.files.text(path).splitlines()[0].lstrip("\ufeff").split("\t")
            bosses = tuple(i for i, label in enumerate(header) if label == "bossMonster")
        raw = self.files.text(path).splitlines() if len(bosses) > 1 else ()
        output = []
        for row in rows:
            maximum = authored_count(row, "spawnLimit")
            if mob := self.entry(row.get("mobiles"), row.source):
                output.append({**mob, "maximum": maximum, "role": "Lair population"})
            boss_slots = [row.get("bossMonster")]
            if raw:
                cells = raw[row.line - 1].split("\t")
                boss_slots = [cells[index].strip('" ') if index < len(cells) else "" for index in bosses]
            for value in boss_slots:
                if boss := self.entry(value, row.source):
                    output.append({**boss, "role": "Possible lair boss"})
        return tuple(output)

    def ships(self, name, proof):
        if row := self.squads.get(name):
            counts = Counter(row.get(f"strShip{i}") for i in range(1, 11) if row.get(f"strShip{i}"))
            return [{**entry, "count": count, "role": name, "selector_source": proof}
                    for ship, count in counts.items() if (entry := self.entry("space/" + ship, row.source))]
        if ship := self.entry("space/" + name, proof):
            return [{**ship, "count": 1}]
        return []

    def wave(self, name):
        return [(entry, row.source) for row in self.waves if (entry := row.get(name))]

    def space_spawner(self, settings, proof, script):
        mode = settings.get("strSpawnerType")
        wave_names = _mangled(settings, "strWaves")
        if mode == "wave":
            options = [option for key in wave_names for option in self.wave(key)]
        else:
            options = [(value, proof) for value in _mangled(settings, "strSpawns")]
            if not options and settings.get("strSpawns"):
                options = [(value, proof) for value in settings["strSpawns"].split(":") if value]
        entries = []
        for choice, selector in options:
            for entry in self.ships(choice, selector):
                entries.append({**entry, "role": entry.get("role") or choice})
        count = _numeric(settings.get("intSpawnCount"))
        return {
            "type": "Space wave spawner" if mode == "wave" else "Space squad spawner",
            "name": settings.get("strSpawnerName"), "entries": entries, "source": proof, "script_source": script,
            "selection": "conditional" if mode == "wave" else "random", "count": count if mode != "wave" else None,
            "count_label": "Squads per activation", "radius": _numeric(settings.get("fltMaxSpawnDistance")),
            "delay_min": _numeric(settings.get("fltMinSpawnTime")), "delay_max": _numeric(settings.get("fltMaxSpawnTime")),
            "configuration_status": "configured" if entries else "scripted",
        }

    def pool(self, row):
        entries = []
        if entry := self.entry(row.get("strTemplateToSpawn"), row.source):
            entries.append({**entry, "count": authored_count(row, "intNumToSpawn"), "role": "Herd population"})
        if lair := row.get("strLairType"):
            entries.extend({**entry, "selector_source": row.source} for entry in self.lair(lair))
        if not entries and (entry := self.entry(row.get("strTemplate"), row.source)):
            entries.append(entry)
        return entries


def spawn_columns(relative, header):
    """Use spawn-bearing columns, not arbitrary matches in table contents."""
    if relative.startswith("spawning/static_npc/"):
        return ("npc_type",)
    if relative.startswith("missions/"):
        return tuple(key for key in ("strPickUpNpc", "strDropOffNpc", "strEscortNpc") if key in header)
    if relative.startswith("faction_perk/hq/spawn/egg/"):
        return ("small", "medium", "large")
    if relative.startswith("faction_perk/hq/spawn/exterior/"):
        return ("small", "med", "large")
    if relative.startswith("gcw/static_base/"):
        return ("rebel", "imperial")
    if relative == "spawning/patrol_spawner.tab":
        return ("template",)
    if relative.startswith("space_trainer_spawner/"):
        return ("templateName",)
    if relative.startswith("space_content/capital_ship_interiors/"):
        return ("strMobsToSpawn", "strBossesToSpawn")
    if relative.startswith("uberlair/"):
        return tuple(key for key in header if key.lower() in {"strleader", "strmobs", "strlargemobs", "strboss"})
    if relative.startswith("spawning/spawn_lists/") and "strInteriorNpcName" in header:
        return ("strInteriorNpcName", "strExteriorNpcName")
    if relative.startswith(("spawning/", "poi/", "dynamic_region_spawns/", "spacequest/", "quest/",
                            "event/", "faction_perk/hq/spawn/", "battlefield/")):
        names = {"spawns", "spawn", "spawn_who", "spawn_template", "creature", "creaturename", "npc", "npc_name",
                 "npc_type", "mob", "strmob", "mobiles", "strspawn", "strspawnname", "strcreature", "strcreaturetype"}
        if relative.startswith(("spawning/", "poi/", "dynamic_region_spawns/", "event/")):
            names.update(("object", "template", "boss"))
        return tuple(key for key in header if key.lower() in names or re.fullmatch(r"strWave\d+", key, re.I))
    # Tables outside the usual namespaces must have an actual script
    # reference. Explicit typed spawn columns can still be interpreted.
    return tuple(key for key in header if key.lower() in {"spawns", "spawn_template", "spawn_who", "strmobstospawn"})


def table_type(relative):
    for prefix, label in (
        ("spawning/building_spawns/", "Building spawn table"), ("spawning/heroic/", "Heroic encounter table"),
        ("spawning/dungeon/", "Dungeon spawn table"), ("spawning/holiday/", "Seasonal event table"),
        ("spawning/script_spawner/", "Planet script spawner"), ("spawning/restuss_event/", "Restuss event table"),
        ("spawning/static_npc/", "Legacy quest NPC"), ("spawning/ground_spawning/types/", "Ground spawn pool"),
        ("spawning/poi_spawner/", "Point of interest spawn table"), ("faction_perk/hq/spawn/", "Faction base spawner"),
        ("gcw/static_base/", "Galactic Civil War base spawner"), ("missions/", "Mission spawn pool"),
        ("npc_lair/", "Lair population"), ("uberlair/", "Imperial lair spawner"),
        ("space_content/", "Space encounter table"), ("space_trainer_spawner/", "Pilot trainer spawner"),
        ("battlefield/", "Battlefield encounter table"), ("dynamic_region_spawns/", "Dynamic region encounter"),
    ):
        if relative.startswith(prefix):
            return label
    return "Script spawn table"


def row_position(row):
    for columns in (("loc_x", "loc_y", "loc_z"), ("locx", "locy", "locz"), ("x", "y", "z"),
                    ("LOC_X", "LOC_Y", "LOC_Z"), ("X", "Y", "Z"), ("x_offset", "y_offset", "z_offset")):
        if xyz := _vector(row, columns):
            return xyz
    return None


def table_name(reference):
    return PurePosixPath(reference).stem
