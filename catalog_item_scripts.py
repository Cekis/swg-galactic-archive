"""Trace authored item creation calls to their templates and destinations."""

from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
import re

from catalog_entities import item_ref
from catalog_loot import LootTables
from catalog_sources import SERVER, SHARED, SCRIPTS, arguments, script_paths, source
from archive_quests.manifest_scripts import COMMENT, METHOD, QUOTED, balanced_end, literal


# The native overloads live in script/base_class.java. The qualified helpers
# use the argument order and container behavior of their library definitions.
CREATORS = {
    "createObject": (0, "create"),
    "createObjectAt": (0, "create_at_object"),
    "createObjectInCell": (0, "cell"),
    "createObjectHyperspace": (0, "create"),
    "createObjectOverloaded": (0, "container"),
    "createObjectInInventoryAllowOverload": (0, "inventory"),
    "createSchematic": (0, "schematic"),
    "create.object": (0, "spawn"),
    "static_item.createNewItemFunction": (0, "static_item"),
    "static_item.makeDynamicObject": (0, "dynamic_item"),
    "space_utils.createObjectTrackItemCount": (0, "container"),
    "space_utils.createShipControlDevice": (1, "ship_device"),
    "space_quest.giveReward": (4, "inventory"),
    "loot.grantFactionalCollectible": (1, "collectible"),
    "loot.createLootItem": (1, "loot_item"),
    "loot.makeLootInContainer": (1, "loot_table"),
}
METHOD_NAMES = "|".join(sorted({key.rsplit(".", 1)[-1] for key in CREATORS}, key=lambda name: (-len(name), name)))
CALL = re.compile(rf"(?<![\w$.])(?:(\w+)\s*\.\s*)?({METHOD_NAMES})\s*\(")
FUNCTION = re.compile(r"(?:(\w+)\s*\.\s*)?(\w+)\s*\(")
IDENTIFIER = r"[A-Za-z_$][\w$]*"
VARIABLE = re.compile(IDENTIFIER)
QUALIFIED = re.compile(rf"({IDENTIFIER})\s*\.\s*({IDENTIFIER})")
DECLARATION = re.compile(
    rf"\b(String|string|obj_id|location|transform|int|dictionary)\s*(?:\[\s*\]\s*)?\s+({IDENTIFIER})\s*(?=[=;,)])"
)
WRITE = re.compile(rf"(?<![\w$.])(?:this\s*\.\s*)?({IDENTIFIER})\s*(\[[^;\]\n]+\]\s*)?([+\-*%/&|^]?=)(?!=)")
ARRAY = re.compile(rf"({IDENTIFIER})\s*\[(.*)\]", re.DOTALL)
ARRAY_LITERAL = re.compile(r"(?:new\s+(?:String|string)\s*\[\s*\]\s*)?\{(.*)\}", re.DOTALL)
STRING_OPERATOR = re.compile(QUOTED + r"|[()+\[\]{}]")
SCOPED_CONTROL = {"if", "for", "while", "switch", "catch", "synchronized"}
STORAGE_SLOTS = {"datapad": "datapad", "inventory": "inventory", "bank": "bank", "mission_bag": "mission_bag"}
STORAGE_GETTERS = {
    "getDatapad": "datapad", "getPlayerDatapad": "datapad", "getInventoryContainer": "inventory",
    "getDroidInventoryContainer": "inventory", "getMissionBag": "mission_bag",
}
DEV_DIRS = {"beta", "content_tools", "csr", "debug", "demo", "developer", "e3demo", "gm", "test", "testing", "qa", "working"}
DEV_SCRIPTS = {"library.load_test", "terminal.terminal_character_builder"}
PHYSICAL_ROOTS = {"tangible", "weapon", "resource_container", "static"}
INTANGIBLE_ROOTS = {"intangible", "mission", "waypoint"}
DYNAMIC_TABLES = {"dynamic_weapon": "weapons", "dynamic_armor": "armor", "dynamic_clothing": "clothing"}


def mask(match):
    return re.sub(r"[^\n]", " ", match[0])


def source_views(text):
    clean = COMMENT.sub(lambda match: mask(match) if match[0].startswith("/") else match[0], text)
    return clean, COMMENT.sub(mask, text)


def function(expression):
    match = FUNCTION.match(expression)
    if match and balanced_end(expression, match.end() - 1) == len(expression) - 1:
        return match[1], match[2], arguments(expression, match.end() - 1)
    return "", "", []


def script_methods(clean, code):
    methods = []
    fields = []
    end = -1
    for match in METHOD.finditer(code):
        if match.start() >= end and match[1] not in SCOPED_CONTROL:
            fields.append((end + 1, match.start()))
            end = balanced_end(code, match.end() - 1)
            methods.append((match, end))
    fields.append((end + 1, len(code)))
    return methods, Bindings("\n".join(clean[start:end] for start, end in fields),
                             "\n".join(code[start:end] for start, end in fields))


class Bindings:
    """Look up values in one method (or in the fields outside all methods)."""

    def __init__(self, text, code, fields=None):
        self.fields = fields
        self.end = len(code.rstrip())
        if fields is None and code.rstrip().endswith("}"):
            self.end -= 1
        self.declarations = defaultdict(list)
        self.writes = defaultdict(list)
        self.starts = [0]
        self.blocks = [0]
        self.parents = {}
        block = 0
        for brace in re.finditer(r"[{}]", code):
            if brace[0] == "{":
                self.parents[brace.end()] = block
                block = brace.end()
            else:
                block = self.parents.get(block, 0)
            self.starts.append(brace.end())
            self.blocks.append(block)
        for match in DECLARATION.finditer(code):
            self.declarations[match[2]].append((match.start(2), match[1], self.block(match.start(2))))
        for match in WRITE.finditer(code):
            end = code.find(";", match.end())
            value = text[match.end():end].strip() if end >= 0 and not match[2] and match[3] == "=" else ""
            self.writes[match[1]].append((match.start(1), value, self.block(match.start(1))))

    def block(self, offset):
        return self.blocks[bisect_right(self.starts, offset) - 1]

    def visible(self, block, offset):
        current = self.block(offset)
        while current and current != block:
            current = self.parents[current]
        return current == block

    def declaration(self, name, at):
        declared = [decl for decl in self.declarations[name] if decl[0] < at and self.visible(decl[2], at)]
        return declared[-1] if declared else (-1, None, 0)

    def binding(self, name, at):
        declaration = self.declaration(name, at)
        latest, kind, _ = declaration
        writes = [(offset, value, block) for offset, value, block in self.writes[name]
                  if latest <= offset < at and self.declaration(name, offset + 1) == declaration]
        values = {value for _, value, _ in writes}
        visible = [(offset, value) for offset, value, block in writes if self.visible(block, at)]
        if len(values) == 1 and visible:
            offset, value = visible[-1]
            return self, [(offset, value)], kind, False
        if kind in {"String", "string"} and len(values) > 1 and "" not in values:
            return self, [(offset, value) for offset, value, _ in writes], kind, True
        if latest == -1 and not writes and self.fields:
            return self.fields.binding(name, self.fields.end)
        return self, [], kind, False


@dataclass(frozen=True)
class Value:
    text: str | int
    selection: str = ""
    table_source: str = ""
    line: int = 0
    constructed: bool = False


class ScriptValues:
    """Resolve literals, bindings and datatable selectors in game scripts."""

    def __init__(self, files):
        self.files = files
        self.paths = script_paths(files)
        self.library_fields = {}

    @lru_cache(maxsize=512)
    def table(self, reference):
        if not reference.startswith("datatables/") or not reference.endswith(".iff"):
            return (), ()
        path = PurePosixPath(reference.removeprefix("datatables/"))
        if ".." in path.parts:
            return (), ()
        for root in (SERVER, SHARED):
            candidate = root.joinpath(*path.with_suffix(".tab").parts)
            if self.files.exists(candidate):
                header = self.files.text(candidate).split("\n", 1)[0].rstrip("\r").split("\t")
                return header, self.files.tab(candidate)
        return (), ()

    def table_values(self, args, bindings, at, depth, *, column_call=False, index=None):
        if len(args) < (2 if column_call else 3):
            return ()
        table = self.single(args[0], bindings, at, depth)
        column = self.single(args[1 if column_call else 2], bindings, at, depth)
        index = index if column_call else self.single(args[1], bindings, at, depth)
        return self.table_cells(table, column, index)

    def table_cells(self, table, column, index):
        if not isinstance(table, str) or column is None:
            return ()
        header, rows = self.table(table)
        if not rows:
            return ()
        if isinstance(column, int):
            column = header[column] if 0 <= column < len(header) else None
        if column not in header:
            return ()
        exact = isinstance(index, (str, int))
        if isinstance(index, int):
            rows = rows[index:index + 1] if index >= 0 else ()
        elif isinstance(index, str):
            rows = [row for row in rows if row.get(header[0]) == index]
        return tuple(Value(row.get(column), "table row" if exact else "possible table result",
                           row.source["path"], row.line) for row in rows if row.get(column))

    def fields(self, module):
        if module not in self.library_fields:
            path = self.paths.get("library." + module)
            self.library_fields[module] = script_methods(*source_views(self.files.text(path)))[1] if path else None
        return self.library_fields[module]

    def single(self, expression, bindings, at, depth=0):
        values = self.values(expression, bindings, at, depth)
        return values[0].text if len(values) == 1 and not values[0].selection.startswith("possible") else None

    def values(self, expression, bindings, at, depth=0, array_index=False):
        if depth >= 8:
            return ()
        depth += 1
        expression = expression.strip()
        if isinstance(value := literal(expression, {}), (str, int)):
            return (Value(value),)
        if expression.startswith("(") and balanced_end(expression, 0) == len(expression) - 1:
            return self.values(expression[1:-1], bindings, at, depth, array_index)
        if VARIABLE.fullmatch(expression):
            owner, choices, _, possible = bindings.binding(expression, at)
            values = (value for offset, choice in choices
                      for value in self.values(choice, owner, offset, depth, array_index))
            return tuple(Value(value.text,
                               "possible assigned value" if possible and value.selection in ("", "table row")
                               else value.selection, value.table_source, value.line, value.constructed)
                         for value in values)
        if match := QUALIFIED.fullmatch(expression):
            if match[1] == "this":
                fields = bindings.fields
            else:
                fields = self.fields(match[1])
            return self.values(match[2], fields, fields.end, depth, array_index) if fields else ()
        if match := ARRAY.fullmatch(expression):
            index = self.single(match[2], bindings, at, depth)
            return self.values(match[1], bindings, at, depth, index)
        if array_index is not False and (match := ARRAY_LITERAL.fullmatch(expression)):
            choices = arguments("(" + match[1] + ")", 0)
            exact = isinstance(array_index, int)
            if exact:
                choices = choices[array_index:array_index + 1] if array_index >= 0 else []
            return tuple(Value(value.text, value.selection or ("" if exact else "possible array result"),
                               value.table_source, value.line, value.constructed)
                         for choice in choices for value in self.values(choice, bindings, at, depth))
        module, method, args = function(expression)
        if not module and method in {"getStringCrc", "crcString"} and len(args) == 1:
            return self.values(args[0], bindings, at, depth)
        if module in (None, "utils") and method == "dataTableGetString":
            return self.table_values(args, bindings, at, depth)
        if not module and method in {"dataTableGetStringColumn", "dataTableGetStringColumnNoDefaults"} and array_index is not False:
            index = array_index if method == "dataTableGetStringColumn" else None
            return self.table_values(args, bindings, at, depth, column_call=True, index=index)
        if module and method == "getString" and len(args) == 1:
            owner, choices, kind, _ = bindings.binding(module, at)
            if kind == "dictionary" and len(choices) == 1:
                offset, row = choices[0]
                _, getter, row_args = function(row)
                if getter == "dataTableGetRow" and len(row_args) == 2:
                    return self.table_cells(self.single(row_args[0], owner, offset, depth),
                                            self.single(args[0], bindings, at, depth),
                                            self.single(row_args[1], owner, offset, depth))
        if "+" in expression and not method:
            level = 0
            splits = []
            for token in STRING_OPERATOR.finditer(expression):
                if token[0] in ("(", "[", "{"):
                    level += 1
                elif token[0] in (")", "]", "}"):
                    level -= 1
                elif token[0] == "+" and level == 0:
                    splits.append(token.start())
            if splits:
                parts = [expression[left + 1:right] for left, right in zip([-1, *splits], [*splits, len(expression)])]
                values = [self.values(part, bindings, at, depth) for part in parts]
                if all(part and all(isinstance(value.text, str) for value in part) for part in values):
                    derived = [i for i, part in enumerate(values) if len(part) > 1 or part[0].selection]
                    if not derived:
                        return (Value("".join(part[0].text for part in values)),)
                    if len(derived) == 1:
                        i = derived[0]
                        prefix = "".join(part[0].text for part in values[:i])
                        suffix = "".join(part[0].text for part in values[i + 1:])
                        return tuple(Value(prefix + value.text + suffix, value.selection, value.table_source, value.line, True)
                                     for value in values[i])
        return ()

class ItemScripts(ScriptValues):
    def __init__(self, files, items):
        super().__init__(files)
        self.items = items
        self.loot = LootTables(files)

    def dynamic_items(self, value):
        for prefix, group in DYNAMIC_TABLES.items():
            if value.text.startswith(prefix):
                rows = self.table_cells(f"datatables/item/dynamic_item/types/{group}.iff",
                                       "strBaseAppearance", value.text)
                for row in rows:
                    if row.text.endswith(".iff"):
                        yield Value(row.text, "possible dynamic item", row.table_source, row.line)
                    else:
                        for choice in self.table_cells(f"datatables/item/dynamic_item/appearances/{row.text}.iff",
                                                       "strTemplate", None):
                            yield Value(choice.text, "possible dynamic item", choice.table_source, choice.line)
                return

    def loot_items(self, value):
        if value.text.startswith("dynamic_"):
            yield from self.dynamic_items(value)
        elif value.text.startswith("loot_items/"):
            for entry in self.loot.nested_items(value.text):
                yield from self.loot_items(Value(entry.value, "possible loot roll",
                                                 entry.source["path"], entry.source["line"]))
        else:
            yield value

    def loot_table_items(self, value):
        for entry in self.loot.ground_items(value.text):
            selection = "required loot entry" if entry.required else "possible loot roll"
            yield from self.loot_items(Value(entry.value, selection, entry.source["path"], entry.source["line"]))

    def created_items(self, behavior, values):
        for value in values:
            if not isinstance(value.text, str):
                continue
            if behavior == "loot_table":
                yield from self.loot_table_items(value)
            elif behavior == "loot_item":
                yield from self.loot_items(value)
            elif behavior == "dynamic_item":
                yield from self.dynamic_items(value)
            elif behavior == "ship_device":
                template = f"object/intangible/ship/{value.text.lower()}_pcd.iff"
                if template in self.items.records:
                    yield Value(template, value.selection, value.table_source, value.line)
            else:
                yield value

    def destination(self, expression, bindings, at, ident, depth=0):
        if depth >= 8:
            return ""
        expression = expression.strip()
        if VARIABLE.fullmatch(expression):
            owner, choices, kind, _ = bindings.binding(expression, at)
            if kind in {"location", "transform"}:
                return "world"
            if len(choices) == 1:
                offset, value = choices[0]
                return self.destination(value, owner, offset, ident, depth + 1)
        if re.match(r"new\s+(?:location|transform)\s*\(", expression):
            return "world"
        module, method, args = function(expression)
        if module == "utils" or (not module and ident == "library.utils"):
            if destination := STORAGE_GETTERS.get(method):
                return destination
        if not module and method in {"getLocation", "getWorldLocation"}:
            return "world"
        if not module and method == "getObjectInSlot" and len(args) == 2:
            slot = self.single(args[1], bindings, at)
            return STORAGE_SLOTS.get(slot, "")
        return ""

    def call_destination(self, behavior, args, bindings, at, ident):
        if behavior in {"inventory", "ship_device", "collectible"}:
            return "datapad" if behavior == "ship_device" else "inventory"
        if behavior == "create_at_object":
            return "object_location"
        if behavior == "cell":
            return "cell"
        if len(args) < 2:
            return "unknown"
        container = self.destination(args[0 if behavior in {"loot_table", "loot_item"} else 1], bindings, at, ident)
        if behavior == "create":
            if len(args) == 2 or container == "world":
                return "world"
            if container:
                return container
            slot = self.single(args[2], bindings, at)
            return "slot" if slot else "container"
        if behavior == "spawn" and (len(args) in {2, 3} or container == "world"):
            return "world"
        if behavior == "static_item" and len(args) == 3 and args[2] != "null":
            pos = self.destination(args[2], bindings, at, ident)
            return "world" if pos == "world" else "unknown"
        return container or ("container_or_inventory" if behavior == "static_item" else "container")

    def index(self):
        stats = Counter()
        items_with_sources = set()
        seen = set()
        for ident, path in sorted(self.paths.items()):
            stats["java_files" if path.suffix == ".java" else "legacy_files"] += 1
            text = self.files.text(path)
            if not CALL.search(text):
                continue
            clean, code = source_views(text)
            methods, fields = script_methods(clean, code)
            development = bool(DEV_DIRS.intersection(path.relative_to(SCRIPTS).parts)) or ident in DEV_SCRIPTS
            for method, end in methods:
                start = method.end()
                body = clean[start:end]
                body_code = code[start:end]
                calls = list(CALL.finditer(body_code))
                if not calls:
                    continue
                # Include formal parameters so unresolved inputs shadow fields.
                header = method[0]
                bindings = Bindings(clean[method.start():end], code[method.start():end], fields)
                for call in calls:
                    qualifier, name = call.groups()
                    key = f"{qualifier}.{name}" if qualifier else name
                    if not qualifier and key not in CREATORS:
                        key = f"{ident.removeprefix('library.')}.{name}"
                    if key not in CREATORS:
                        continue
                    arg, behavior = CREATORS[key]
                    args = arguments(body, call.end() - 1)
                    if len(args) <= arg:
                        continue
                    at = call.start() + len(header)
                    values = self.values(args[arg], bindings, at)
                    if not values:
                        stats["unresolved_calls"] += 1
                        continue
                    destination = self.call_destination(behavior, args, bindings, at, ident)
                    line = clean.count("\n", 0, start + call.start()) + 1
                    proof = source(path, line)
                    for value in self.created_items(behavior, values):
                        raw = value.text.lower()
                        item = self.items.resolve_item(raw) if item_ref(raw) else raw if raw in self.items.records else None
                        if not item:
                            continue
                        if value.constructed and item not in self.items.records:
                            if item not in self.items.template_fields and not (item.startswith("object/") and self.items.asset_for(item)):
                                continue
                        unique = (item, ident, line, key, value.table_source, value.line)
                        if unique in seen:
                            continue
                        seen.add(unique)
                        acquisition = {"kind": "script", "method": method[1], "call": key, "script": ident,
                                       "behavior": behavior, "destination": destination, "source": proof}
                        if value.selection:
                            acquisition["selection"] = value.selection
                        if value.table_source:
                            acquisition["table_source"] = {"path": value.table_source, "line": value.line}
                        if behavior == "cell" and len(args) >= 3:
                            if isinstance(cell := self.single(args[2], bindings, at), str) and cell:
                                acquisition["cell"] = cell
                        if development:
                            acquisition["development"] = True
                        self.items.ensure(item)["acquisitions"].append(acquisition)
                        items_with_sources.add(item)
                        stats["development_sources" if development else "non_tool_sources"] += 1
                        stats["java_sources" if path.suffix == ".java" else "legacy_sources"] += 1
                        stats[f"destination_{destination}"] += 1
        stats["items_with_sources"] = len(items_with_sources)
        self.table.cache_clear()
        return dict(stats)


def describe_storage(item):
    template = item["template"] or ""
    root = template.split("/")[1] if template.startswith("object/") else ""
    form = "intangible" if root in INTANGIBLE_ROOTS else "physical" if root in PHYSICAL_ROOTS else None
    destinations = Counter(row["destination"] for row in item["acquisitions"]
                           if row["kind"] == "script" and not row.get("development"))
    return form, destinations
