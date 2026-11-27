"""Read explicit creature creation calls and spawn-table references in game scripts."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import re

from catalog_item_scripts import Bindings, DEV_DIRS, DEV_SCRIPTS, ScriptValues, VARIABLE, function, script_methods, source_views
from catalog_locations import OBJECT_ASSET, _numeric
from catalog_sources import SCRIPTS, arguments, source


# (target argument, location argument, selector). These follow create.scriptlib,
# space_create.scriptlib, and the native bindings in base_class.java.
CREATORS = {
    "create.object": (0, 1, "creature"),
    "create.staticObject": (0, 1, "creature"),
    "create.createNpc": (0, 2, "creature"),
    "createObject": (0, 1, "creature"),
    "createObjectAt": (0, 1, "creature"),
    "createObjectInCell": (0, 1, "cell"),
    "createObjectHyperspace": (0, 1, "creature"),
    "create.objectWithYaw": (0, 1, "creature"),
    "space_create.createShip": (0, 1, "ship"),
    "space_create.createShipHyperspace": (0, 1, "ship"),
    "space_create.createSquad": (1, 2, "squad"),
    "space_create.createSquadHyperspace": (1, 2, "squad"),
    "lair.createDefaultLair": (0, 1, "lair"),
}
NAMES = "|".join(sorted({name.rsplit(".", 1)[-1] for name in CREATORS}, key=lambda name: (-len(name), name)))
CREATE = re.compile(rf"(?<![\w$.])(?:(\w+)\s*\.\s*)?({NAMES})\s*\(")
CALL = re.compile(r"(?<![\w$.])(\w+)\s*\(")
TABLE = re.compile(r'"(datatables/[\w/-]+\.iff)"')
STRING = re.compile(r'"(?:\\.|[^"\\])*"')
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?[fd]?", re.I)
POSITION = re.compile(r"new\s+location\s*\(")
LOCATION_WRITE = re.compile(r"\b(\w+)\s*\.\s*([xyz]|area|cell)\s*(?:([+*-]?=)(?!=)|\+\+|--)")
ROOM_TABLE_READ = re.compile(
    r"(?<![\w$.])(?:(?:utils\s*\.\s*)?dataTableGet(?:String|Float|Int)"
    r"|(spawning\s*\.\s*spawnObjectsInDungeonFromTable))\s*\("
)
ROOM_COLUMNS = {"room", "cell", "cellname", "loc_x", "locx", "x_offset"}
OBJECT_DISPATCH = re.compile(
    r'\b(\w+)\s*\.\s*(?:contains|endsWith)\s*\(\s*"\.iff"\s*\)\s*'
    r'(?:\?|(?=\)\s*\{))'
)
OBJECT_DISPATCH_ELSE = re.compile(r'\)\s*(?::|;\s*\}\s*else\s*\{\s*(?:\w+\s*=\s*)?)\s*$')


@dataclass(frozen=True, slots=True)
class Position:
    scene: str | None = None
    xyz: tuple | None = None
    room: str | None = None
    frame: str = "runtime"


@dataclass(frozen=True, slots=True)
class Creation:
    path: dict
    method: str
    call: str
    mode: str
    target: str
    table: dict | None
    position: Position
    possible: bool
    development: bool
    caller: dict | None


@dataclass(slots=True)
class Frame:
    bindings: Bindings
    at: int
    args: dict
    caller: Frame | None = None
    uses: dict | None = None


def _parameters(header):
    return tuple(match.group(1) for arg in arguments(header, header.index("("))
                 if (match := re.search(r"(\w+)(?:\s*\[\])?\s*$", arg)))


def _is_receiver(value):
    return value in {"self", "getSelf()"}


class SpawnScripts(ScriptValues):
    def __init__(self, files, creatures, templates, scenes, interior_scripts=()):
        super().__init__(files)
        self.creatures, self.templates, self.scenes = creatures, templates, scenes
        self.interior_scripts = set(interior_scripts)
        self.by_script = defaultdict(list)
        self.by_table = defaultdict(dict)
        self.room_tables = defaultdict(dict)
        self.unresolved = []
        self.stats = Counter()
        self._visited = set()
        self._selectors = {}

    def _values(self, expression, frame, depth=0):
        if depth == 8:
            return ()
        epoch, at = frame.bindings, frame.at
        values = self.values(expression, epoch, at)
        if values:
            return tuple((value.text, {"path": value.table_source, "line": value.line} if value.table_source else None,
                          value.selection.startswith("possible")) for value in values)
        expression = expression.strip()
        if VARIABLE.fullmatch(expression):
            owner, choices, _, possible = epoch.binding(expression, at)
            if choices:
                return tuple((value, proof, possible or conditional)
                             for offset, choice in choices
                             for value, proof, conditional in self._values(choice, Frame(owner, offset, frame.args, frame.caller), depth + 1))
            if frame.caller and expression in frame.args:
                return self._values(frame.args[expression], frame.caller, depth + 1)
        module, method, args = function(expression)
        if method in {"getStringObjVar", "getStringScriptVar"} and len(args) == 2 and _is_receiver(args[0]):
            if key := self.single(args[1], epoch, at):
                return (("@objvar/" + key, None, True),)
        if not module and not args and method in self._selectors:
            return self._selectors[method]
        return ()

    def _value(self, expression, frame):
        values = self._values(expression, frame)
        return values[0][0] if len(values) == 1 and not values[0][2] else None

    def _number(self, expression, frame, depth=0):
        if depth == 8:
            return None
        if NUMBER.fullmatch(expression):
            return _numeric(expression.rstrip("fFdD"))
        if VARIABLE.fullmatch(expression):
            owner, choices, _, _ = frame.bindings.binding(expression, frame.at)
            if len(choices) == 1:
                at, value = choices[0]
                return self._number(value, Frame(owner, at, frame.args, frame.caller), depth + 1)
            if frame.caller and expression in frame.args:
                return self._number(frame.args[expression], frame.caller, depth + 1)
        return _numeric(self._value(expression, frame))

    def _receiver(self, expression, frame, depth=0):
        if depth == 8:
            return False
        if frame.caller and expression in frame.args:
            return self._receiver(frame.args[expression], frame.caller, depth + 1)
        return _is_receiver(expression)

    def _room(self, expression, frame, depth=0):
        if depth == 8:
            return None
        _, method, args = function(expression)
        if method == "getCellId" and len(args) == 2 and self._receiver(args[0], frame):
            name = self._value(args[1], frame)
            return name if isinstance(name, str) else None
        if VARIABLE.fullmatch(expression):
            owner, choices, _, _ = frame.bindings.binding(expression, frame.at)
            if len(choices) == 1:
                offset, value = choices[0]
                return self._room(value, Frame(owner, offset, frame.args, frame.caller), depth + 1)
            if frame.caller and expression in frame.args:
                return self._room(frame.args[expression], frame.caller, depth + 1)
        return None

    def _assigned_position(self, updates, frame):
        if len(updates) != 4 or {field for _, field, _ in updates} != {"x", "y", "z", "cell"}:
            return Position()
        resolved = {}
        for at, field, value in updates:
            if value is None or not frame.bindings.visible(frame.bindings.block(at), frame.at):
                return Position()
            context = Frame(frame.bindings, at, frame.args, frame.caller)
            resolved[field] = self._room(value, context) if field == "cell" else self._number(value, context)
        xyz = tuple(resolved[axis] for axis in "xyz")
        return Position(xyz=None if None in xyz else xyz, room=resolved["cell"], frame="cell")

    def _position(self, expression, frame, depth=0):
        if depth == 8:
            return Position()
        if match := POSITION.match(expression):
            values = arguments(expression, match.end() - 1)
            if len(values) in (4, 5):
                xyz = tuple(self._number(part, frame) for part in values[:3])
                xyz = None if None in xyz else xyz
                scene = self._value(values[3], frame)
                scene = scene if isinstance(scene, str) and scene in self.scenes else None
                cell = values[4] if len(values) == 5 else ""
                if cell in ("", "null", "0", "obj_id.NULL_ID"):
                    return Position(scene, xyz, frame="world" if scene and xyz else "runtime")
                return Position(scene, xyz, self._room(cell, frame), "cell")
        elif VARIABLE.fullmatch(expression):
            owner, choices, _, _ = frame.bindings.binding(expression, frame.at)
            if len(choices) == 1:
                offset, value = choices[0]
                updates = [update for update in (frame.uses or {}).get(expression, ())
                           if offset < update[0] < frame.at]
                if updates:
                    return self._assigned_position(updates, frame)
                return self._position(value, Frame(owner, offset, frame.args, frame.caller, frame.uses), depth + 1)
            if frame.caller and expression in frame.args:
                return self._position(frame.args[expression], frame.caller, depth + 1)
        _, method, values = function(expression)
        if (method in {"getLocation", "getWorldLocation", "getTransform_o2p"} and len(values) == 1
                and self._receiver(values[0], frame)):
            return Position(frame="anchor")
        return Position()

    def _target(self, mode, value):
        if not isinstance(value, str):
            return False
        if value.startswith("@objvar/"):
            return True
        if mode == "ship":
            return "space/" + value in self.creatures
        if mode == "squad":
            return bool(re.fullmatch(r"[a-zA-Z0-9_]+", value))
        if mode == "lair":
            return value.startswith("datatables/npc_lair/")
        return value in self.creatures or bool(OBJECT_ASSET.fullmatch(value) and (
            value in self.templates or value.startswith(("object/mobile/", "object/creature/"))
        ))

    def _record(self, ident, path, method, key, mode, target, table, position, possible, development, caller):
        signature = (ident, path["line"], key, target, (table or {}).get("line"), (table or {}).get("path"), position)
        if signature not in self._visited:
            self._visited.add(signature)
            self.by_script[ident].append(Creation(path, method, key, mode, target, table, position,
                                                 possible, development, caller))

    def scan(self):
        for ident, path in sorted(self.paths.items()):
            self.stats["scripts"] += 1
            raw = self.files.text(path)
            reads_rooms = ident in self.interior_scripts and ROOM_TABLE_READ.search(raw)
            if not reads_rooms and not CREATE.search(raw) and not TABLE.search(raw):
                continue
            clean, code = source_views(raw)
            development = bool(DEV_DIRS.intersection(path.relative_to(SCRIPTS).parts)) or ident in DEV_SCRIPTS
            for match in TABLE.finditer(clean):
                self.by_table[match[1]].setdefault(ident, (source(path, clean.count("\n", 0, match.start()) + 1), development))
            if CREATE.search(code) or reads_rooms:
                self._scan_script(ident, path, clean, code, development)
        self.stats["resolved_calls"] = len({(call.path["path"], call.path["line"]) for calls in self.by_script.values() for call in calls})
        self.stats["unresolved_calls"] = len(self.unresolved)
        self._visited.clear()
        return self

    def _scan_script(self, ident, path, clean, code, development):
        methods, fields = script_methods(clean, code)
        callers = defaultdict(list)
        names = {method[1] for method, _ in methods}
        parsed = {}
        self._selectors = {}
        for method, end in methods:
            if not method[0].lstrip().lower().startswith("string ") or _parameters(method[0]):
                continue
            offset = method.end() - method.start()
            bindings = Bindings(clean[method.start():end], code[method.start():end], fields)
            choices = []
            for result in re.finditer(r"\breturn\s+([^;]+);", code[method.end():end]):
                start, stop = result.span(1)
                expression = clean[method.end() + start:method.end() + stop].strip()
                proof = source(path, clean.count("\n", 0, method.end() + start) + 1)
                choices.extend((value.text, proof, True)
                               for value in self.values(expression, bindings, offset + start)
                               if value.text in self.creatures)
            if choices:
                self._selectors[method[1]] = tuple(dict.fromkeys((item, p["path"], p["line"]) for item, p, _ in choices))
                self._selectors[method[1]] = tuple((item, {"path": path, "line": line}, True)
                                                 for item, path, line in self._selectors[method[1]])
        for index, (method, end) in enumerate(methods):
            start = method.end()
            body = code[start:end]
            direct = list(CREATE.finditer(body))
            local = [call for call in CALL.finditer(body) if call[1] in names]
            room_reads = list(ROOM_TABLE_READ.finditer(body)) if ident in self.interior_scripts and not development else []
            if not direct and not local and not room_reads:
                continue
            bindings = Bindings(clean[method.start():end], code[method.start():end], fields)
            writes = defaultdict(list)
            for match in LOCATION_WRITE.finditer(body):
                end = body.find(";", match.end())
                value = clean[start + match.end():start + end].strip() if match[3] == "=" and end >= 0 else None
                writes[match[1]].append((start - method.start() + match.start(), match[2], value))
            parsed[index] = (direct, bindings, writes, room_reads)
            for call in local:
                args = arguments(clean, start + call.end() - 1)
                at = start - method.start() + call.start()
                callers[(call[1], len(args))].append((index, args, at, start + call.start()))

        def contexts(index, at, visited=()):
            method, _ = methods[index]
            _, bindings, writes, _ = parsed[index]
            yield Frame(bindings, at, {}, uses=writes), None
            if index in visited or len(visited) >= 3:
                return
            params = _parameters(method[0])
            for parent, args, offset, absolute in callers[method[1], len(params)]:
                if parent != index:
                    proof = source(path, clean.count("\n", 0, absolute) + 1)
                    for frame, outer in contexts(parent, offset, (*visited, index)):
                        yield Frame(bindings, at, dict(zip(params, args)), frame, writes), outer or proof

        # A legacy script's triggers and message handlers are runtime entry
        # points. A helper with no caller is retained as a definition.
        for index, (direct, _, _, room_reads) in parsed.items():
            method, _ = methods[index]
            invoked = (path.suffix != ".script" or method[0].lstrip().startswith(("trigger ", "messageHandler "))
                       or bool(callers[method[1], len(_parameters(method[0]))]))
            if invoked:
                for read in room_reads:
                    absolute = method.end() + read.start()
                    args = arguments(clean, method.end() + read.end() - 1)
                    if len(args) < 3:
                        continue
                    proof = source(path, clean.count("\n", 0, absolute) + 1)
                    for frame, _ in contexts(index, absolute - method.start()):
                        if read[1]:
                            if not self._receiver(args[0], frame):
                                continue
                            table_arg = args[2]
                        else:
                            column = self._value(args[2], frame)
                            if not isinstance(column, str) or column.casefold() not in ROOM_COLUMNS:
                                continue
                            table_arg = args[0]
                        for ref, _, _ in self._values(table_arg, frame):
                            if isinstance(ref, str) and ref.startswith(("datatables/", "@objvar/")):
                                self.room_tables[ident].setdefault(ref, proof)
            dispatch_arg = None
            if len(direct) == 2 and direct[0].groups() == (None, "createObject") and direct[1].groups() == ("create", "object"):
                before = clean[method.end():method.end() + direct[0].start()]
                between = clean[method.end() + direct[0].end():method.end() + direct[1].start()]
                if (dispatch := OBJECT_DISPATCH.search(before)) and OBJECT_DISPATCH_ELSE.search(between):
                    dispatch_arg = dispatch[1]
            for call in direct:
                module, name = call.groups()
                owner = module or (ident[8:] if ident.startswith("library.") else "")
                key = f"{owner}.{name}" if owner and (module or name not in CREATORS) else name
                if key not in CREATORS:
                    continue
                target_index, location_index, mode = CREATORS[key]
                absolute = method.end() + call.start()
                args = arguments(clean, method.end() + call.end() - 1)
                if len(args) <= max(target_index, location_index):
                    continue
                proof = source(path, clean.count("\n", 0, absolute) + 1)
                resolved, had_values = False, False
                for frame, via in contexts(index, absolute - method.start()):
                    targets = self._values(args[target_index], frame)
                    had_values |= bool(targets)
                    keep_objects = ident in self.interior_scripts and mode in {"creature", "cell"}
                    candidates = [(target, table, possible, self._target(mode, target))
                                  for target, table, possible in targets if isinstance(target, str)]
                    if not any(accepted or keep_objects and OBJECT_ASSET.fullmatch(target)
                               for target, _, _, accepted in candidates):
                        continue
                    position = self._position(args[location_index], frame)
                    if key == "createObjectAt" and _is_receiver(args[location_index]):
                        position = Position(frame="anchor")
                    if mode == "cell" and len(args) > 2 and self._receiver(args[1], frame):
                        room = self._value(args[2], frame)
                        local = self._position(args[3], frame) if len(args) > 3 else Position()
                        position = Position(xyz=local.xyz, room=room if isinstance(room, str) else None, frame="cell")
                    for target, table, possible, accepted in candidates:
                        if dispatch_arg == args[target_index] and target.endswith(".iff") != (key == "createObject"):
                            continue
                        if accepted or (keep_objects and (position.room or position.frame == "anchor") and
                                        OBJECT_ASSET.fullmatch(target)):
                            self._record(ident, proof, method[1], key, mode, target, table, position, possible,
                                         development or not invoked, via)
                            resolved = True
                    if resolved and not via and position.frame not in {"runtime", "cell"}:
                        break
                if not resolved and not had_values:
                    self.unresolved.append({"source": proof, "script": ident, "method": method[1],
                                            "call": key, "expression": args[target_index], "development": development})
