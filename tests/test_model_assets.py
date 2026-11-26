"""Small decoder checks plus real client-asset fixtures when available."""

from base64 import b64decode
from io import BytesIO
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
import zlib


PROJECT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT))

from catalog_model_previews import render_inventory_model
from catalog_models import Chunk, ModelError, ModelLibrary, chunks, expand_quaternion, shader_alpha, version
from archive_config import legacy_asset

from recover_item_models import ClientTree


def iff_chunk(tag, data):
    return struct.pack(">4sI", tag, len(data)) + data


def iff_form(tag, *children):
    return iff_chunk(b"FORM", tag + b"".join(children))


class DecoderTests(unittest.TestCase):
    def test_iff_rejects_chunks_that_overrun_their_parent(self):
        data = memoryview(struct.pack(">4sI", b"FORM", 40) + b"SMAT")
        with self.assertRaisesRegex(ModelError, "Truncated"):
            list(chunks(data))

    def test_native_quaternion_identity(self):
        self.assertEqual(expand_quaternion(0, (254, 254, 254)), [0, 0, 0, 1])

    def test_model_overlay_replaces_bounds_and_falls_back_to_client_assets(self):
        ref = "appearance/mesh/doll.msh"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            client, overlay = root / "clientdata", root / "overlay"
            for folder, tag in ((client, b"APPR"), (overlay, b"MESH")):
                asset = folder / ref
                asset.parent.mkdir(parents=True)
                asset.write_bytes(struct.pack(">4sI4s", b"FORM", 4, tag))
            library = ModelLibrary(client, asset_overlay=overlay)
            self.assertEqual(library._form(ref).tag, "MESH")
            (overlay / ref).unlink()
            self.assertEqual(library._form(ref).tag, "APPR")

    def test_original_lower_detail_and_particle_mesh_use_the_native_vertices(self):
        positions = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        vertices = b"".join(struct.pack("<8f", *xyz, 0, 0, 1, 1, float("nan")) for xyz in positions)
        buffer = iff_form(b"VTXA", iff_form(b"0003", iff_chunk(b"INFO", struct.pack("<Ii", 0x1105, 3)),
                                           iff_chunk(b"DATA", vertices)))
        primitive = iff_form(b"0000", iff_chunk(b"INFO", struct.pack("<i", 3)), buffer)
        shader = iff_form(b"0000", iff_chunk(b"NAME", b"shader/fixture.sht\0"), primitive)
        mesh = iff_form(b"MESH", iff_form(b"0005", iff_form(b"SPS ", iff_form(b"0001", shader))))
        infos = iff_chunk(b"INFO", struct.pack("<iffiff", 0, 0, 10, 1, 10, 50))
        children = iff_form(b"DATA", iff_chunk(b"CHLD", struct.pack("<i", 0) + b"mesh/absent.msh\0"),
                            iff_chunk(b"CHLD", struct.pack("<i", 1) + b"mesh/detail_example.msh\0"))
        lod = iff_form(b"DTLA", iff_form(b"0008", infos, children))
        particle = iff_form(b"PEFT", iff_form(b"0003", iff_form(b"PTMH", iff_chunk(b"0000", b"lod/example.lod\0"))))
        with TemporaryDirectory() as directory:
            client = Path(directory)
            for ref, data in (("appearance/mesh/detail_example.msh", mesh), ("appearance/lod/example.lod", lod),
                              ("appearance/example.prt", particle)):
                path = client / ref
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            library = ModelLibrary(client)
            for ref, selection in (("appearance/lod/example.lod", "Authored lower-detail mesh"),
                                   ("appearance/example.prt", "Authored particle mesh")):
                with self.subTest(ref=ref):
                    model = library.get(ref)
                    self.assertEqual(model["status"], "available")
                    self.assertEqual(model["triangles"], 1)
                    self.assertEqual(model["meshes"][0]["asset"], "appearance/mesh/detail_example.msh")
                    self.assertEqual(model["meshes"][0]["uvs"], [1.0, 0.0] * 3)
                    self.assertTrue(model["model_selection"].startswith(selection))
                    json.dumps(model, allow_nan=False)

    def test_client_patch_tre_reads_the_original_file_at_its_indexed_path(self):
        payload = iff_form(b"MESH", iff_form(b"0005"))
        compressed = zlib.compress(payload)
        toc = zlib.compress(ClientTree.ENTRY.pack(0, len(payload), ClientTree.HEADER.size, 2, len(compressed), 0))
        name = b"appearance/mesh/fixture.msh\0"
        names = zlib.compress(name)
        header = ClientTree.HEADER.pack(b"EERT", b"5000", 1, ClientTree.HEADER.size + len(compressed),
                                        2, len(toc), 2, len(names), len(name))
        self.assertEqual(ClientTree(header + compressed + toc + names).read(name[:-1].decode("ascii")), payload)

    def test_untrusted_paths_stay_outside_the_model_asset_lookup(self):
        model = ModelLibrary(PROJECT)
        for path in ("../README.md", r"..\README.md", "texture/example.iff",
                     "object/mobile/../../README.md", "F:/SWGProjects/clientdata/object/example.iff"):
            with self.subTest(path=path):
                self.assertEqual(model.get(path)["status"], "missing")


class SuppliedAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clientdata = legacy_asset("clientdata", "SWG_CLIENTDATA")
        if not (clientdata / "object/mobile/shared_watto.iff").is_file():
            raise unittest.SkipTest("Point SWG_CLIENTDATA at the supplied extracted client assets.")
        cls.library = ModelLibrary(clientdata)

    def test_purvis_uses_his_authored_outfit_and_character_animation(self):
        model = self.library.get("dressed_purvis_arrison.iff")
        self.assertEqual(model["status"], "available")
        self.assertEqual(model["appearance"], "appearance/hum_m.sat")
        self.assertIn("client_shared_dressed_purvis_arrison.cdf", model["client_data"])
        self.assertTrue(any("armor_padded" in mesh["asset"] for mesh in model["meshes"]))
        self.assertTrue(any(clip["asset"].endswith("all_m_standing_conversation_m.ans")
                            for clip in model["animations"]))
        self.check_meshes(model)

    def test_purvis_opaque_clothing_keeps_color_under_its_auxiliary_alpha_mask(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("The optional Pillow preview backend is unavailable.")
        model = self.library.get("dressed_purvis_arrison.iff")
        shirt = next(mesh for mesh in model["meshes"] if mesh["shader"] == "shader/shirt_s07_hcb13.sht")
        self.assertEqual(shirt["material"], {"effect": "effect/h_color2_bump.eft"})
        with Image.open(self.library.root / shirt["texture"]) as native:
            self.assertEqual(native.getpixel((native.width // 2, native.height // 2))[3], 0)
        data = model["textures"][shirt["texture_key"]].split(",", 1)[1]
        with Image.open(BytesIO(b64decode(data))) as preview:
            self.assertEqual(preview.mode, "RGB")
            self.assertGreater(min(preview.getpixel((preview.width // 2, preview.height // 2))), 120)

    def test_native_transparent_pass_retains_blending_and_its_alpha_test(self):
        effect = version(self.library._form("effect/c_alpha.eft", "effect/"), "0001")
        bare_shader = Chunk("0000", memoryview(b""), True)
        self.assertEqual(shader_alpha(bare_shader, effect), {
            "blend": [0, 4, 5], "depth_write": False,
            "alpha_test": {"comparison": 4, "reference": 128 / 255},
        })

    def test_watto_has_real_weighted_geometry_and_flying_joint_motion(self):
        model = self.library.get("watto.iff")
        self.assertEqual(model["status"], "available")
        self.assertEqual(model["triangles"], 4442)
        wing = next(i for i, bone in enumerate(model["rig"]) if bone["name"] == "lftwing1")
        clip = next(clip for clip in model["animations"] if clip["name"] == "Locomotion")
        rotation = clip["tracks"][wing][0]
        self.assertTrue(clip["asset"].endswith("toydarian_loc_fly.ans"))
        self.assertGreater(len({tuple(key[1:]) for key in rotation}), 1)
        self.check_meshes(model)

    def test_static_mesh_reads_native_indexed_triangle_lists(self):
        asset = "appearance/mesh/a_havoc_nym_cockpit_l0.msh"
        model = self.library.get(asset)
        self.assertEqual(model["status"], "available")
        self.assertEqual([len(item["indices"]) // 3 for item in model["meshes"]], [1508, 1504])

    def test_lightsaber_wrapper_resolves_its_authored_hilt(self):
        blade = "appearance/wp_mle_sword_lightsaber_blade_mace.lsb"
        hilt = "appearance/wp_mle_sword_lightsaber_mace.apt"
        if not (self.library.root / blade).is_file():
            self.skipTest("The supplied lightsaber fixture is unavailable.")
        self.assertEqual(self.library._appearance_form(blade)[1], self.library._appearance_form(hilt)[1])

    def test_in_game_xwing_display_mesh_decodes_from_server_appearance(self):
        asset = "appearance/xwing_model.ssa"
        if not (self.library.root / asset).is_file():
            self.skipTest("The supplied display-mesh fixture is unavailable.")
        model = self.library.get(asset)
        self.assertEqual(model["status"], "available")
        self.assertEqual(model["triangles"], 4908)
        self.assertTrue(any(value != 0 for group in model["meshes"] for value in group["uvs"]))
        json.dumps(model, allow_nan=False)

    def test_building_floor_preview_has_vertices_for_all_triangle_indices(self):
        asset = "appearance/thm_tato_fort_tusken.pob"
        model = render_inventory_model(self.library, "locations", "fort_tusken", {"layout_asset": asset})
        self.assertEqual(model["status"], "available")
        self.assertGreater(model["vertices"], 0)
        self.assertGreater(model["triangles"], 0)
        for mesh in model["meshes"]:
            count = len(mesh["positions"]) // 3
            self.assertGreater(count, max(mesh["indices"]))
            self.assertEqual(len(mesh["normals"]), count * 3)

    def test_large_cavern_can_render_its_authored_interior(self):
        asset = "appearance/thm_kash_cave_myyydril_interior.pob"
        if not (self.library.root / asset).is_file():
            self.skipTest("The supplied cavern fixture is unavailable.")
        model = render_inventory_model(self.library, "locations", "myyydril", {"layout_asset": asset})
        self.assertEqual(model["status"], "available")
        if model.get("geometry_kind") == "floors":
            self.assertEqual(model["triangles"], 51429)

    def check_meshes(self, model):
        json.dumps(model, allow_nan=False)
        self.assertGreater(len(model["rig"]), 1)
        for mesh in model["meshes"]:
            count = len(mesh["positions"]) // 3
            self.assertEqual(len(mesh["normals"]), count * 3)
            self.assertEqual(len(mesh["uvs"]), count * 2)
            self.assertEqual(len(mesh["joints"]), count * 4)
            self.assertEqual(len(mesh["weights"]), count * 4)
            self.assertLess(max(mesh["indices"]), count)


if __name__ == "__main__":
    unittest.main()
