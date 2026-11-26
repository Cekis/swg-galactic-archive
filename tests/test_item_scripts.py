"""Keep script call provenance separate from item form and guessed runtime values."""

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_entities import Items
from catalog_item_scripts import ItemScripts, describe_storage
from catalog_sources import Files, SERVER, SCRIPTS, source


def index(scripts, tables=None, known_templates=()):
    files = Files()
    blobs = {SCRIPTS / name: text for name, text in scripts.items()}
    blobs.update({SERVER / name: text for name, text in (tables or {}).items()})
    files.blobs = {source(path)["path"]: text.encode("utf-8") for path, text in blobs.items()}
    items = Items(files, None)
    for template in known_templates:
        items.ensure(template)
    stats = ItemScripts(files, items).index()
    return items.records, stats


class ItemScriptAcquisitionTest(unittest.TestCase):
    def test_destination_follows_the_authored_container(self):
        records, stats = index({
            "systems/example.java": """public class example {
                private static final String PHYSICAL = "object/tangible/test/" + "carried.iff";
                public int OnInitialize(obj_id self, obj_id player) {
                    obj_id inventory = utils.getInventoryContainer(player);
                    createObject(PHYSICAL, inventory, "");
                    obj_id pad = getObjectInSlot(player, constants.PAD_SLOT);
                    createObject(constants.CONTROL_DEVICE, pad, "");
                    createObject("object/intangible/test/world_entry.iff", getLocation(self));
                    createObject("object/tangible/test/crate.iff", self, "");
                    return 0;
                }
            }""",
            "library/constants.java": """public class constants {
                public static final String PAD_SLOT = "datapad";
                public static final String CONTROL_DEVICE = "object/intangible/test/control_device.iff";
            }""",
        })
        carried = records["object/tangible/test/carried.iff"]
        device = records["object/intangible/test/control_device.iff"]
        self.assertEqual(describe_storage(carried), ("physical", {"inventory": 1}))
        self.assertEqual(describe_storage(device), ("intangible", {"datapad": 1}))
        self.assertEqual(describe_storage(records["object/intangible/test/world_entry.iff"]), ("intangible", {"world": 1}))
        self.assertEqual(records["object/tangible/test/crate.iff"]["acquisitions"][0]["destination"], "container")
        self.assertEqual(device["acquisitions"][0]["source"]["line"], 7)
        self.assertEqual(stats["non_tool_sources"], 4)

    def test_table_and_branch_results_keep_their_proof_and_uncertainty(self):
        records, _ = index({
            "quest/supplies.java": """public class supplies {
                private static final String TABLE = "datatables/quest/example.iff";
                public int grant(obj_id player, String faction, int row) {
                    dictionary entry = dataTableGetRow(TABLE, row);
                    String reward = entry.getString("reward");
                    createObjectInInventoryAllowOverload(reward, player);
                    String token = null;
                    if (faction.equals("a")) {
                        token = "object/intangible/test/a.iff";
                    } else {
                        token = "object/intangible/test/b.iff";
                    }
                    createObject(token, utils.getPlayerDatapad(player), "");
                    return 0;
                }
            }""",
        }, {"quest/example.tab": "id\treward\tunused\ns\ts\ts\nfirst\tobject/tangible/test/reward.iff\titem_unrelated\n"})
        reward = records["object/tangible/test/reward.iff"]["acquisitions"][0]
        self.assertEqual(reward["selection"], "possible table result")
        self.assertEqual(reward["table_source"]["line"], 3)
        self.assertEqual(reward["destination"], "inventory")
        self.assertNotIn("item_unrelated", records)
        for faction in ("a", "b"):
            entry = records[f"object/intangible/test/{faction}.iff"]["acquisitions"][0]
            self.assertEqual((entry["selection"], entry["destination"]), ("possible assigned value", "datapad"))

    def test_effective_source_overloads_and_tool_entries(self):
        records, stats = index({
            "item/sample.java": r"""public class sample {
                private static final String ITEM = "item_field";
                public int choose(obj_id player, String ITEM) {
                    createObjectInInventoryAllowOverload(ITEM, player);
                    // createObjectInInventoryAllowOverload("item_comment", player);
                    String text = "createObjectInInventoryAllowOverload(\"item_example\", player)";
                    return 0;
                }
                public int choose(obj_id player) {
                    createObjectInInventoryAllowOverload(ITEM, player);
                    return 0;
                }
            }""",
            "item/sample.script": """int oldVersion(obj_id player) {
                createObjectInInventoryAllowOverload("item_shadowed_source", player);
            }""",
            "developer/spawn.script": """int spawn(obj_id player) {
                createObjectInInventoryAllowOverload("item_test_reward", player);
            }""",
        })
        self.assertEqual(set(records), {"item_field", "item_test_reward"})
        field, tool = (records[name]["acquisitions"][0] for name in ("item_field", "item_test_reward"))
        self.assertEqual(field["source"]["line"], 10)
        self.assertNotIn("development", field)
        self.assertTrue(tool["development"])
        self.assertEqual(describe_storage(records["item_test_reward"]), (None, {}))
        self.assertEqual((stats["java_files"], stats["legacy_files"], stats["unresolved_calls"]), (1, 1, 1))

    def test_loot_call_follows_exact_columns_and_reports_a_possible_roll(self):
        records, _ = index({
            "systems/chest.java": """public class chest {
                public int onOpen(obj_id self) {
                    loot.makeLootInContainer(self, "chest:select", 2, 30);
                    createObjectInCell("object/tangible/test/chest.iff", self, "storage_room");
                    return 0;
                }
            }""",
        }, {
            "loot/loot_types/chest.tab": "strItems\tselect\tstrRequiredItems\ns\ts\ts\nunused\tchest_roll:items\titem_always\n",
            "loot/loot_items/chest_roll.tab": "strItemType\titems\ns\ts\nitem_wrong\tloot_items/drops:choice\n",
            "loot/loot_items/drops.tab": "choice\ns\nitem_possible\nloot_items/drops:choice\n",
        })
        self.assertEqual(set(records), {"item_always", "item_possible", "object/tangible/test/chest.iff"})
        result = records["item_possible"]["acquisitions"][0]
        self.assertEqual((result["selection"], result["destination"]), ("possible loot roll", "container"))
        self.assertTrue(result["table_source"]["path"].endswith("loot_items/drops.tab"))
        required = records["item_always"]["acquisitions"][0]
        self.assertEqual(required["selection"], "required loot entry")
        placed = records["object/tangible/test/chest.iff"]["acquisitions"][0]
        self.assertEqual((placed["destination"], placed["cell"]), ("cell", "storage_room"))

    def test_constructed_control_device_name_requires_a_real_template(self):
        device = "object/intangible/pet/companion.iff"
        records, _ = index({
            "item/pet.java": """public class pet {
                public int createDevice(obj_id player, String key) {
                    String template = "object/intangible/pet/" +
                        dataTableGetString("datatables/mob/example.iff", key, "template");
                    createObject(template, utils.getPlayerDatapad(player), "");
                    return 0;
                }
            }""",
        }, {"mob/example.tab": "id\ttemplate\ns\ts\nfirst\tcompanion.iff\nsecond\tnpc_without_device.iff\n"}, [device])
        self.assertEqual(set(records), {device})
        entry = records[device]["acquisitions"][0]
        self.assertEqual((entry["destination"], entry["selection"]), ("datapad", "possible table result"))
