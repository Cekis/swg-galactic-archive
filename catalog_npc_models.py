"""Link NPC model previews to the choices made by the game creation routines."""

from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import PurePosixPath
import re

from catalog_models import mob_template, normalize_ref
from catalog_sources import SERVER, SERVER_OBJECTS, SHARED_OBJECTS


CUSTOM = SERVER / "npc_customization"
SHARED = re.compile(r'^\s*sharedTemplate\s*=\s*"(object/[^"]+\.iff)"', re.M)
APPEARANCE = re.compile(r'^\s*(?:appearanceFilename|portalLayoutFilename)\s*=\s*"(appearance/[^"]+)"', re.M)
BASE = re.compile(r"^@base\s+(object/[^\s]+\.iff)", re.M)
MOBILE = ("object/mobile/", "object/creature/")
RENAMED_MOBILES = {
    # The supplied client has one Gorax object and its male Gorax appearance.
    "object/mobile/gorax_male.iff": "object/mobile/gorax.iff",
}


class NpcModels:
    def __init__(self, files, models):
        self.files = files
        self.models = models
        self.by_stem = defaultdict(set)
        self.space = {row.get("strIndex"): row for row in self._table(SERVER / "space_mobile/space_mobile.tab")}
        for directory in ("mobile", "creature"):
            for root in self.models.roots:
                for path in (root / "object" / directory).rglob("*.iff"):
                    ident = path.stem.lower().removeprefix("shared_")
                    self.by_stem[ident].add(path.relative_to(root).as_posix().lower())
            for path in files.paths(SERVER_OBJECTS / directory, "*.tpf", recursive=True):
                self.by_stem[path.stem.lower()].add("object/" + path.relative_to(SERVER_OBJECTS).with_suffix(".iff").as_posix())

    @lru_cache(maxsize=256)
    def _table(self, path):
        return self.files.tab(path) if self.files.exists(path) else ()

    @lru_cache(maxsize=4096)
    def _choices(self, key, previous=()):
        if key in previous or len(previous) >= 5:
            return ()
        if ref := mob_template(key):
            return ((ref, None),)
        if key.endswith("iff") and "/" not in key:
            key = key[:-3].rstrip(".") + ".iff"
            if self.models.available(mob_template(key)):
                return ((mob_template(key), None),)
        if not re.fullmatch(r"[\w-]+", key):
            return ()
        variants = []
        group = CUSTOM / f"dressed_species_{key[0]}.tab"
        for row in self._table(group):
            if value := row.get(key):
                variants.append((value, row.source))
        if not variants:
            for row in self._table(CUSTOM / f"{key}.tab"):
                variants.extend((value, row.source) for value in tuple(row.cells.values())[:2] if value)
        choices = {}
        for variant, proof in variants:
            for ref, origin in self._choices(variant, (*previous, key)):
                choices.setdefault(ref, origin or proof)
        if not choices:
            if match := self._unique_mobile(key):
                choices[match] = None
        return tuple(choices.items())

    def _unique_mobile(self, key):
        for name in (key, "dressed_" + key):
            families = {str(PurePosixPath(ref).with_name(PurePosixPath(ref).name.removeprefix("shared_")))
                        for ref in self.by_stem.get(name, ())}
            if len(families) == 1:
                return next(iter(families))
        return None

    @lru_cache(maxsize=4096)
    def _candidates(self, ref, previous=()):
        if ref in previous or len(previous) >= 8:
            return ()
        output = [ref]
        if not ref.startswith("object/"):
            return tuple(output)
        parts = PurePosixPath(ref)
        if alias := RENAMED_MOBILES.get(ref):
            output.append(alias)
        if ref.startswith(MOBILE) and (match := self._unique_mobile(parts.stem.removeprefix("shared_"))) and ref != match:
            output.append(match)
        roots = (SHARED_OBJECTS, SERVER_OBJECTS) if parts.name.startswith("shared_") else (SERVER_OBJECTS, SHARED_OBJECTS)
        for root in roots:
            path = root.joinpath(*parts.with_suffix(".tpf").parts[1:])
            if not self.files.exists(path):
                continue
            text = self.files.text(path)
            if match := APPEARANCE.search(text):
                output.append(match[1])
            elif match := SHARED.search(text):
                output.extend(self._candidates(match[1], (*previous, ref)))
            elif match := BASE.search(text):
                output.extend(self._candidates(match[1], (*previous, ref)))
        return tuple(dict.fromkeys(output))

    def _find(self, choices):
        candidates = {}
        resolved = {}
        for ref, proof in choices:
            chain = self._candidates(ref)
            for candidate in chain:
                candidates.setdefault(candidate, proof)
            if match := next((candidate for candidate in chain if self.models.available(candidate)), None):
                resolved.setdefault(match, proof)
        return list(resolved.items()), tuple(candidates)

    @lru_cache(maxsize=256)
    def _pilots(self, variant):
        return tuple((template, row.source) for row in self._table(SERVER / "space_mobile/space_appearances.tab")
                     if (template := mob_template(row.get(variant))))

    def index(self, npcs):
        stats = Counter()
        fallback = defaultdict(list)
        by_identity = defaultdict(list)
        for ident, npc in npcs.items():
            original = normalize_ref(npc.get("template"))
            templates = (original, *npc.get("variant_templates", ()))
            row = self.space.get(ident.removeprefix("space/")) if ident.startswith("space/") else None
            if row and (hull := row.get("strShip")):
                templates = ("object/ship/" + hull + ".iff",)
            choices = [(ref, proof) for key in templates if key and
                       (key.startswith(MOBILE + ("object/ship/", "object/tangible/", "appearance/")) or not key.startswith("object/"))
                       for ref, proof in (((key, None),) if key.startswith("appearance/") else self._choices(key))]
            if not choices and original and original.startswith("object/"):
                choices = [(original, None)] if npc["category"] != "Placement" else []
            if not choices:
                choices = list(self._choices(ident))
            resolved, candidates = self._find(choices)
            pilot = False
            if row and not resolved and row.get("strAppearance"):
                resolved, alternate = self._find(self._pilots(row.get("strAppearance")))
                candidates = (*candidates, *alternate)
                pilot = bool(resolved)
            if not resolved and ident != original:
                same_name = self._choices(ident)
                if same_name:
                    resolved, alternate = self._find(same_name)
                    candidates = (*candidates, *alternate)
            if resolved:
                primary, proof = resolved[0]
                npc.update(model_asset=primary, model_source=proof, model_choices=[ref for ref, _ in resolved],
                           model_status="resolved")
                if proof:
                    npc["model_selection"] = "Authored appearance choice"
                if row:
                    npc["model_source"] = proof if pilot else row.source
                    npc["model_selection"] = "Pilot communications appearance" if pilot else "Authored ship hull"
                    if pilot:
                        npc["missing_hull_model"] = templates[0]
                        stats["pilot_appearances"] += 1
                stats["resolved"] += 1
                for key in {self._name_key(ident), self._name_key(npc["name"])} - {""}:
                    by_identity[key].append(npc)
                for script in npc["scripts"]:
                    if script:
                        fallback[script].append(npc)
            else:
                npc.update(model_status="missing", model_attempts=list(dict.fromkeys(candidates)))
        for ident, npc in npcs.items():
            if npc["model_status"] != "missing":
                continue
            script = ident if ident.startswith("conversation.") else None
            matches = fallback.get(script, ()) if script else ()
            match_type = "Appearance of the NPC using this conversation"
            if not matches and npc["category"] == "Quest contact":
                matches = {choice["model_asset"]: choice for key in (self._name_key(ident), self._name_key(npc["name"]))
                           for choice in by_identity[key]}.values()
                match_type = "Appearance of the matching authored quest contact"
            if len(matches) == 1:
                match = next(iter(matches))
                npc.update(model_asset=match["model_asset"], model_source=match.get("model_source") or
                           next(iter(match["sources"]), None), model_choices=match["model_choices"],
                           model_status="resolved", model_selection=match_type)
                stats["resolved"] += 1
            else:
                stats["missing"] += 1
                stats[f"missing_{npc['category'].lower().replace(' ', '_')}"] += 1
        return dict(stats)

    @staticmethod
    def _name_key(value):
        return re.sub(r"[^\w]", "", value.casefold().replace("_", ""))
