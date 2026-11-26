from fnmatch import fnmatchcase
from pathlib import Path
from struct import pack
from tempfile import TemporaryDirectory
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_schematics import load_schematics
from catalog_sources import SCRIPTS, SERVER, SERVER_OBJECTS, SHARED, SHARED_OBJECTS
from manifest_sources import read_tab


class MockFiles:
    def __init__(self, files):
        self.files = files

    def exists(self, path):
        return path in self.files

    def text(self, path):
        return self.files[path]

    def tab(self, path):
        return read_tab(path, self.text(path))

    def paths(self, directory, pattern, recursive=False):
        return sorted(path for path in self.files if path.is_relative_to(directory)
                      and (recursive or path.parent == directory) and fnmatchcase(path.name, pattern))


class MockStrings:
    def lookup(self, code):
        return {"code": code, "text": {"@crafting:example": "Example Recipe",
                                      "@crafting:composite": "Composite Frame"}.get(code)}


def chunk(tag, body):
    return tag + pack(">I", len(body)) + body


def form(kind, *chunks):
    return chunk(b"FORM", kind + b"".join(chunks))


def param(name, value):
    return chunk(b"XXXX", name + b"\0" + value)


def string_id(table, key):
    return b"\1\1" + table + b"\0\1" + key + b"\0"


class SchematicTests(unittest.TestCase):
    def test_effective_recipe_slots_provenance_crafting_time_grants_and_yield(self):
        ident = "object/draft_schematic/item/example.iff"
        files = MockFiles({
            SERVER_OBJECTS / "draft_schematic/item/example.tpf": """
@base object/draft_schematic/base/base_example.iff
@class draft_schematic_object_template 7
category = CT_weapon
craftedObjectTemplate = "object/tangible/component/example.iff"
slots = +[
    [name = "crafting" "composite",
     optional = true,
     options = [[ingredientType = IT_resourceClass,
                 ingredients = [[ingredient = "Metal_Ferrous", count = 12],
                                [ingredient = "iron", count = 5]]],
                [ingredientType = IT_template,
                 ingredients = [[ingredient = "object/tangible/component/composite.iff", count = 1]]]]]]
manufactureScripts = ["systems.crafting.example"]
@class object_template 8
complexity = 24
xpPoints = [[type = XP_craftingWeapons, level = 0, value = 0]]
sharedTemplate = "object/draft_schematic/item/shared_example.iff"
""",
            SERVER_OBJECTS / "draft_schematic/base/base_example.tpf": """
@class draft_schematic_object_template 7
itemsPerContainer = 25
prototypeTime = 2.0
manufactureTime = 8.0
slots = [[name = "crafting" "base_slot",
          optional = false,
          options = [[ingredientType = IT_resourceClass, ingredients = [[ingredient = "steel", count = 2]]]]]]
""",
            SHARED_OBJECTS / "draft_schematic/item/shared_example.tpf": """
@class draft_schematic_object_template 3
attributes = [[name = "crafting" "charges", experiment = "crafting" "expCount", value = 2..8],
              [name = "crafting" "xp", experiment = "" "", value = 350..350]]
slots = [[name = "crafting" "composite", hardpoint = "hp_composite"]]
@class object_template 9
objectName = "crafting" "example"
""",
            SERVER_OBJECTS / "tangible/crafting/station/public_weapon_station.tpf":
                'objvars = +["crafting.type"=CT_weapon | CT_droid]',
            SERVER_OBJECTS / "tangible/crafting/station/weapon_tool.tpf": 'objvars = +["crafting.type"=CT_weapon]',
            SCRIPTS / "systems/crafting/base_tool.script": "const float[] COMPLEXITY_LIMIT = { 15, 20, 25 };",
            SCRIPTS / "systems/crafting/example.script": "inherits systems.crafting.example_base;",
            SCRIPTS / "systems/crafting/example_base.script": """
// setCount(prototype, 900);
if ( itemAttributes[i].name.getAsciiId().equals("charges")) {
    setCount(prototype, (int) itemAttributes[i].currentValue);
}
""",
            SHARED / "crafting/schematic_group.tab": "GroupId\tSchematicName\ns\ts\nweaponGroup\t" + ident,
            SHARED / "skill/skills.tab":
                'NAME\tSCHEMATICS_GRANTED\tSCHEMATICS_REVOKED\ns\ts\ts\nweapon_skill\t"weaponGroup,otherGroup"\t\n'
                'loses_recipe\t\tweaponGroup\n',
            SERVER / "crafting/limited_use_schematics.tab":
                f"schematic_name\tschematic_uses\tsource_object\nh\ti\tc\n{ident}\t5\t"
                "object\\tangible\\loot\\example.tpf\n",
            SERVER / "quest/crafting/crafting_quests.tab":
                f"Profession\tLevel\tSchematic\tTemplate\ns\ts\ts\ts\nartisan\teasy\t{ident}\t"
                "object/tangible/component/example.iff\n",
        })

        record = load_schematics(files, MockStrings(), None)[ident]
        self.assertEqual(record["status"], "authored_recipe")
        self.assertEqual(record["name"], "Example Recipe")
        self.assertEqual(record["items_per_container"], 25)
        self.assertEqual(record["outputs"][0]["quantity"], 1)
        self.assertEqual(record["outputs"][0]["quantity_kind"], "prototype_objects")
        self.assertEqual(record["outputs"][0]["units_per_object"]["min"], 2)
        self.assertEqual(record["outputs"][0]["units_per_object"]["max"], 8)
        self.assertEqual(record["craft_time"]["prototype"]["at_authored_complexity_seconds"], 48)
        self.assertTrue(record["craft_time"]["prototype"]["source"]["path"].endswith("base_example.tpf"))
        self.assertEqual(record["xp"][0]["amount"], 350)
        self.assertEqual(record["xp"][0]["type"], "XP_craftingWeapons")
        self.assertEqual(record["xp"][0]["template_value"], 0)
        self.assertEqual(len(record["slots"]), 2)
        self.assertEqual(record["slots"][0]["options"][0]["ingredients"][0]["count"], 2)
        self.assertTrue(record["slots"][0]["source"]["path"].endswith("base_example.tpf"))
        slot = record["slots"][1]
        self.assertEqual(slot["name"], "Composite Frame")
        self.assertTrue(slot["optional"])
        self.assertEqual(slot["hardpoint"], "hp_composite")
        self.assertEqual([item["count"] for item in slot["options"][0]["ingredients"]], [12, 5])
        self.assertEqual(slot["options"][0]["ingredients"][0]["ingredient"], "Metal_Ferrous")
        self.assertEqual(slot["options"][0]["ingredients"][0]["resource_id"], "metal_ferrous")
        self.assertEqual(slot["options"][1]["ingredients"][0]["item_id"], "object/tangible/component/composite.iff")
        self.assertTrue(record["station"]["required"])
        self.assertEqual(record["station"]["minimum_access"], "public")
        self.assertEqual(record["station"]["compatible_stations"][0]["access"], "public")
        self.assertEqual(record["grants"][0]["skill_id"], "weapon_skill")
        self.assertEqual(record["grants"][0]["group_source"]["line"], 3)
        self.assertEqual(record["revocations"][0]["skill_id"], "loses_recipe")
        self.assertEqual(record["limited_uses"][0]["uses"], 5)
        self.assertEqual(record["limited_uses"][0]["item_id"], "object/tangible/loot/example.iff")
        self.assertEqual(record["crafting_missions"][0]["profession"], "artisan")

    def test_explicit_table_recipe_keeps_unknown_yield_and_individual_slot_evidence(self):
        ident = "object/draft_schematic/weapon/test_recipe.iff"
        columns = "entry_type schematic_name crc_schematic_name slots xp_type xp crafted_object_template "
        columns += "slot_type slot_optional slot_name slot_ingredient_name slot_ingredient_count"
        header = "\t".join(columns.split())
        files = MockFiles({SERVER / "crafting/weapon_schematics.tab":
                          f"{header}\ns\nweapon\ttest_recipe\t{ident}\t1\tcrafting\t15\t"
                          "object/tangible/example.iff\nslot\t\t\t\t\t\t\tresource\t0\t"
                          "metal_frame\tmetal\t20\n"})

        record = load_schematics(files, MockStrings(), None)[ident]
        self.assertEqual(record["status"], "table_recipe")
        self.assertIsNone(record["outputs"][0]["quantity"])
        self.assertEqual(record["outputs"][0]["status"], "explicit_table")
        self.assertEqual(record["xp"][0]["amount"], 15)
        slot = record["slots"][0]
        self.assertEqual(slot["name"], "Metal Frame")
        self.assertFalse(slot["optional"])
        self.assertEqual(slot["options"][0]["ingredients"][0]["resource_id"], "metal")
        self.assertEqual(slot["source"]["line"], 4)

    def test_shared_iff_recovers_presentation_data_with_unknown_server_requirements(self):
        asset = "object/draft_schematic/item/shared_archive_only.iff"
        base = "object/draft_schematic/item/shared_archive_base.iff"
        slots = form(b"0003",
                     form(b"SISS", param(b"name", string_id(b"crafting", b"composite")),
                          param(b"hardpoint", b"\1hp_composite\0")))
        recipe = form(b"SDSC", form(b"DERV", chunk(b"XXXX", base.encode() + b"\0")),
                      form(b"0003",
                           param(b"slots", b"\1" + pack("<i", 0)),
                           form(b"DSSA", param(b"name", string_id(b"crafting", b"charges")),
                                param(b"value", b"\3\x20" + pack("<ii", 10, 25))),
                           param(b"craftedSharedTemplate", b"\1object/tangible/shared_archive_only.iff\0")))
        with TemporaryDirectory() as temp:
            path = Path(temp) / asset
            path.parent.mkdir(parents=True)
            path.write_bytes(recipe)
            (Path(temp) / base).write_bytes(form(b"SDSC", slots))
            records = load_schematics(MockFiles({}), MockStrings(), Path(temp), {asset: path})
        record = records["object/draft_schematic/item/archive_only.iff"]
        self.assertEqual(record["status"], "client_reference")
        self.assertEqual(record["client_template"], asset)
        self.assertEqual(record["outputs"][0]["item_id"], "object/tangible/shared_archive_only.iff")
        self.assertIsNone(record["outputs"][0]["quantity"])
        self.assertEqual(record["slots"][0]["name"], "Composite Frame")
        self.assertEqual(record["slots"][0]["options"], [])
        self.assertEqual(record["slots"][0]["source"]["path"], base)
        self.assertEqual(record["experimentation"][0]["min"], 10)
        self.assertEqual(record["experimentation"][0]["status"], "client_compiled_range")


if __name__ == "__main__":
    unittest.main()
