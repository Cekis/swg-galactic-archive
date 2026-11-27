"""Verify that a source file selects all Archive inputs and refreshes them in place."""

from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_config import github_revision, load_profile
from build_catalog import build
from build_deployment import describe_assets, github_assets, local_source_zip, write_package
from catalog_db import Catalog
from catalog_provenance import SourceCatalog
from catalog_sources import SHARED, SERVER
from deployment_assets import verify_or_install
from deployment_trace import asset_destinations, audit_in_parallel, compare_audits
from test_model_assets import iff_chunk, iff_form
from verify_deployment import http_smoke


SERVER_PATH = SERVER.relative_to("dsrc")
SHARED_PATH = SHARED.relative_to("dsrc")


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def init(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Galactic Archive test")
    git(path, "config", "user.email", "archive@example.test")
    git(path, "config", "core.autocrlf", "false")


def commit(path):
    git(path, "add", "--all")
    git(path, "commit", "-qm", "Test source revision")
    return git(path, "rev-parse", "HEAD")


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def stf(path, **strings):
    text, keys = [], []
    for ident, (key, value) in enumerate(strings.items(), 1):
        line = value.encode("utf-16-le")
        code = key.encode("ascii")
        text.append(struct.pack("<III", ident, 0, len(line) // 2) + line)
        keys.append(struct.pack("<II", ident, len(code)) + code)
    save(path, struct.pack("<IBII", 0xABCD, 1, len(strings) + 1, len(strings)) + b"".join(text + keys))


def tab(path, *rows):
    save(path, "\n".join("\t".join(map(str, row)) for row in rows) + "\n")


def quest_file(dsrc, ident):
    return dsrc / SHARED_PATH / f"questlist/quest/{ident}.tab"


def quest(dsrc, ident, level):
    path = quest_file(dsrc, ident)
    tab(path, ("JOURNAL_ENTRY_TITLE", "LEVEL"), ("s", "i"), (f"@quest_n:{ident}", level))
    return path


def model_assets(root, texture):
    """Write a one-triangle item with a native template, shader and DDS."""
    vertices = b"".join(struct.pack("<8f", *position, 0, 0, 1, 0, 0)
                        for position in ((0, 0, 0), (1, 0, 0), (0, 1, 0)))
    info = iff_chunk(b"INFO", struct.pack("<Ii", 0x1105, 3))
    buffer = iff_form(b"VTXA", iff_form(b"0003", info, iff_chunk(b"DATA", vertices)))
    primitive = iff_form(b"0000", iff_chunk(b"INFO", struct.pack("<i", 3)), buffer)
    shader_name = b"shader/survey.sht\0"
    primitive_set = iff_form(b"0000", iff_chunk(b"NAME", shader_name), primitive)
    mesh = iff_form(b"MESH", iff_form(b"0005", iff_form(b"SPS ", iff_form(b"0001", primitive_set))))
    save(root / "appearance/mesh/survey.msh", mesh)

    material = iff_form(b"0000", iff_chunk(b"DATA", b"NIAM"),
                        iff_chunk(b"NAME", b"texture/survey.dds\0"))
    shader = iff_form(b"SSHT", iff_form(b"0000", iff_form(b"TXMS", iff_form(b"TXM ", material))))
    save(root / "shader/survey.sht", shader)
    save(root / "texture/survey.dds", texture)

    appearance = iff_chunk(b"XXXX", b"appearanceFilename\0\x01appearance/mesh/survey.msh\0")
    template = iff_form(b"SHOT", iff_form(b"0000", appearance))
    save(root / "object/tangible/shared_survey.iff", template)


class SourceProfileTests(unittest.TestCase):
    def test_refresh_updates_adds_and_removes_records_from_selected_sources(self):
        with TemporaryDirectory() as work:
            root = Path(work)
            game = root / "different-server"
            init(game)
            dsrc = game / "data-source"
            retained = quest(dsrc, "survey", 5)
            stale = quest(dsrc, "old_mission", 3)
            tab(dsrc / SERVER_PATH / "region/planets.tab",
                ("planet", "minX", "minZ", "maxX", "maxZ"), ("s", "i", "i", "i", "i"),
                ("survey_world", -500, -500, 500, 500))
            first_revision = commit(game)
            src = root / "different-engine"
            init(src)
            save(src / "native/test.cpp", "// first alternative source line\n// second line\n")
            commit(src)

            stf(root / "different-client/string/en/quest_n.stf", survey="Client title", old_mission="Old mission")
            stf(root / "tools/data/string/en/quest_n.stf", survey="Survey from tools")
            config = root / "sources.json"
            save(config, json.dumps({"sources": {
                "src": {"path": "different-engine", "subdirectory": "native"},
                "dsrc": {"path": "different-server", "subdirectory": "data-source"},
                "clientdata": {"path": "different-client"},
                "client-tools": {"path": "tools"},
            }}))

            first = load_profile(config, root)
            metadata = build(first.database, first)
            old = Catalog(first.database)
            self.assertEqual(metadata["revision"], first_revision)
            self.assertEqual(old.entity("quests", "quest/survey")["card"]["name"], "Survey from tools")
            self.assertIsNotNone(old.entity("quests", "quest/old_mission"))
            self.assertIsNotNone(old.entity("locations", "survey_world"))
            self.assertIsNone(old.entity("locations", "tatooine"))
            source = SourceCatalog(first_revision, catalog=old, profile=first)
            self.assertEqual(dict(source.excerpt("src/test.cpp", 2))[2], "// second line")
            self.assertIsNone(source.github("dsrc/" + retained.relative_to(dsrc).as_posix(), 3))

            stale.unlink()
            quest(dsrc, "survey", 12)
            quest(dsrc, "new_mission", 9)
            second_revision = commit(game)
            stf(root / "tools/data/string/en/quest_n.stf", survey="Revised survey", new_mission="New mission")
            second = load_profile(config, root)
            self.assertEqual(first.database, second.database)
            self.assertNotEqual(first.overlay, second.overlay)

            broken_config = json.loads(config.read_text(encoding="utf-8"))
            broken_config["quest_annotations"] = "invalid-annotations.json"
            save(config, json.dumps(broken_config))
            save(root / "invalid-annotations.json", "invalid\n")
            broken = load_profile(config, root)
            with self.assertRaises(ValueError):
                build(broken.database, broken)
            self.assertEqual(old.metadata()["revision"], first_revision)
            self.assertIsNotNone(old.entity("quests", "quest/old_mission"))

            del broken_config["quest_annotations"]
            save(config, json.dumps(broken_config))
            metadata = build(second.database, second)
            updated = Catalog(second.database)
            self.assertEqual(metadata["revision"], second_revision)
            record = updated.entity("quests", "quest/survey")
            self.assertEqual(record["card"]["name"], "Revised survey")
            self.assertEqual(record["card"]["level"], 12)
            self.assertEqual(record["data"]["title"]["asset"], "client-tools/data/string/en/quest_n.stf")
            self.assertIsNone(updated.entity("quests", "quest/old_mission"))
            self.assertIsNotNone(updated.entity("quests", "quest/new_mission"))
            self.assertEqual(metadata["counts"]["quests"], 2)
            self.assertEqual(git(game, "status", "--porcelain"), "")

    def test_github_branch_advances_to_a_new_immutable_checkout(self):
        with TemporaryDirectory() as work:
            root = Path(work)
            game = root / "upstream"
            init(game)
            git(game, "symbolic-ref", "HEAD", "refs/heads/archive-default")
            quest(game, "quest_one", 3)
            before = commit(game)
            branch = git(game, "symbolic-ref", "--short", "HEAD")
            stf(root / "assets/string/en/quest_n.stf", quest_one="Quest one", quest_two="Quest two")
            config = root / "sources.json"
            save(config, json.dumps({"sources": {
                "dsrc": {"github": "AlternateOrg/branch-test", "revision": branch},
                "clientdata": {"path": "assets"},
            }}))
            # Exercise Git's actual clone/fetch path using an isolated local origin.
            env = {
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": f"url.{game.as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/AlternateOrg/branch-test.git",
                "GIT_CONFIG_KEY_1": "protocol.file.allow",
                "GIT_CONFIG_VALUE_1": "always",
            }
            def select_revision():
                code = (
                    "import json, sys; from pathlib import Path; from archive_config import load_profile; "
                    "profile = load_profile(Path(sys.argv[1]), Path(sys.argv[2])); "
                    "repo = profile.repositories['dsrc']; "
                    "print(json.dumps([repo.revision, str(repo.root), profile.key]))"
                )
                result = subprocess.run([sys.executable, "-B", "-c", code, str(config), str(root)],
                                        cwd=Path(__file__).resolve().parents[1], env={**os.environ, **env},
                                        capture_output=True, text=True)
                if result.returncode:
                    raise RuntimeError(result.stderr)
                revision, checkout, key = json.loads(result.stdout.splitlines()[-1])
                return revision, Path(checkout), key

            revision, original, original_key = select_revision()
            self.assertEqual(revision, before)
            self.assertTrue(quest_file(original, "quest_one").is_file())
            quest_file(game, "quest_one").unlink()
            quest(game, "quest_two", 6)
            after = commit(game)
            revision, latest, key = select_revision()
            self.assertEqual(revision, after)
            self.assertNotEqual(key, original_key)
            self.assertFalse(quest_file(latest, "quest_one").exists())
            self.assertTrue(quest_file(latest, "quest_two").is_file())
            self.assertTrue(quest_file(original, "quest_one").is_file())

            sources = json.loads(config.read_text(encoding="utf-8"))
            sources["sources"]["dsrc"]["revision"] = "missing-archive-branch"
            save(config, json.dumps(sources))
            message = f"AlternateOrg/branch-test has no branch named 'missing-archive-branch'. Its default branch is {branch!r}."
            with self.assertRaises(RuntimeError) as missing:
                select_revision()
            self.assertIn(message, str(missing.exception))

            del sources["sources"]["dsrc"]["revision"]
            save(config, json.dumps(sources))
            self.assertEqual(select_revision(), (after, latest, key))

    def test_git_failure_with_empty_stderr_is_distinct_from_a_missing_branch(self):
        failure = subprocess.CompletedProcess(["git", "ls-remote"], 128, b"", b"")
        with patch("archive_config.subprocess.run", return_value=failure):
            with self.assertRaises(ValueError) as error:
                github_revision("AlternateOrg/offline", None)
        self.assertEqual(str(error.exception),
                         "Unable to read branches for AlternateOrg/offline. Git ls-remote failed (exit 128)")

    def test_building_cells_link_master_scripts_tables_and_dedicated_spawners(self):
        with TemporaryDirectory() as work:
            root = Path(work)
            game = root / "game"
            init(game)
            server = game / SERVER_PATH
            shared = game / SHARED_PATH
            tab(shared / "buildout/buildout_scenes.tab", ("sceneName",), ("s",), ("tatooine",))
            tab(shared / "buildout/areas_tatooine.tab",
                ("area", "x1", "z1", "x2", "z2"), ("s", "f", "f", "f", "f"),
                ("settlement", -1000, -3000, 0, -2000))
            tab(server / "mob/creatures.tab", ("creatureName",), ("s",),
                ("fixture_guard",), ("fixture_captain",), ("fixture_egg_guard",))

            building = "object/building/fixture/house.iff"
            cell = "object/cell/cell.iff"
            egg = "object/tangible/creature_spawner/fixture_egg.iff"
            prop = "object/tangible/fixture_sign.iff"
            master = "theme_park.fixture.masterspawner"
            table_script = "theme_park.fixture.table_spawner"
            egg_script = "creature_spawner.fixture_egg"
            table = "datatables/spawning/building_spawns/fixture_house.iff"
            tab(server / "buildout/tatooine/settlement.tab",
                ("objid", "container", "server_template_crc", "cell_index", "px", "py", "pz",
                 "qw", "qx", "qy", "qz", "scripts", "objvars"),
                ("i", "i", "h", "i", "f", "f", "f", "f", "f", "f", "f", "s", "p"),
                (101, 0, building, 0, 25, 8, 40, 1, 0, 0, 0, master, f"fixture.table|4|{table}|"),
                (102, 101, cell, 1, 3, 0, 4, 1, 0, 0, 0, "", ""),
                (103, 102, egg, 1, 7, 0, 8, 1, 0, 0, 0, "", ""))
            save(server.parent / "object/building/fixture/house.tpf",
                 '@base object/building/fixture/base.iff\n'
                 'scripts = +["theme_park.fixture.decorator"]\n'
                 'sharedTemplate = "object/building/fixture/shared_house.iff"\n')
            save(server.parent / "object/building/fixture/base.tpf", f'scripts = ["{table_script}"]\n')
            save(shared.parent / "object/building/fixture/shared_house.tpf",
                 'portalLayoutFilename = "appearance/fixture_house.pob"\n')
            save(server.parent / "object/tangible/creature_spawner/fixture_egg.tpf", f'scripts = ["{egg_script}"]\n')
            save(server.parent / "object/tangible/fixture_sign.tpf", "")
            save(server.parent / "script/theme_park/fixture/masterspawner.java", f"""
                public int OnInitialize(obj_id self) {{
                    spawn("fixture_guard", 2.0f, .5f, -4.0f, getCellId(self, "lobby"));
                    spawnCaptain(self);
                    return SCRIPT_CONTINUE;
                }}
                private void spawnCaptain(obj_id building) {{
                    location where = getLocation(building);
                    where.x = 3;
                    where.y = 2;
                    where.z = -1;
                    where.cell = getCellId(building, "lobby");
                    create.staticObject("fixture_captain", where);
                }}
                private obj_id spawn(String ref, float x, float y, float z, obj_id cell) {{
                    location where = new location(x, y, z, "tatooine", cell);
                    return ref.endsWith(".iff") ? createObject(ref, where) : create.object(ref, where);
                }}
            """)
            save(server.parent / "script/theme_park/fixture/table_spawner.java", """
                public int OnInitialize(obj_id self) {
                    String table = getStringObjVar(self, "fixture.table");
                    spawning.spawnObjectsInDungeonFromTable(self, "tatooine", table);
                    String unused = "datatables/spawning/building_spawns/fixture_unused.iff";
                    return SCRIPT_CONTINUE;
                }
            """)
            save(server.parent / "script/creature_spawner/fixture_egg.java", """
                public int OnInitialize(obj_id self) {
                    create.object("fixture_egg_guard", getLocation(self));
                    return SCRIPT_CONTINUE;
                }
            """)
            tab(server / "spawning/building_spawns/fixture_house.tab",
                ("object", "room", "loc_x", "loc_y", "loc_z"), ("s", "s", "f", "f", "f"),
                (prop, "LOBBY", -2, 0.25, 6), (prop, "missing_room", 1, 2, 3))
            tab(server / "spawning/building_spawns/fixture_unused.tab",
                ("object", "room", "loc_x", "loc_y", "loc_z"), ("s", "s", "f", "f", "f"),
                (prop, "lobby", 99, 99, 99))
            stf(root / "client/string/en/fixture.stf", name="Fixture")
            cells = iff_form(b"CELS", *(iff_form(b"CELL", iff_form(b"0005",
                          iff_chunk(b"DATA", b"\0" * 5 + name + b"\0\0\0"))) for name in (b"exterior", b"lobby")))
            save(root / "client/appearance/fixture_house.pob",
                 iff_form(b"PRTO", iff_form(b"0004", cells, iff_chunk(b"CRC ", struct.pack("<I", 123)))))
            commit(game)
            config = root / "sources.json"
            save(config, json.dumps({"sources": {"dsrc": {"path": "game"}, "clientdata": {"path": "client"}}}))
            profile = load_profile(config, root)

            build(profile.database, profile)
            catalog = Catalog(profile.database)
            ident = "tatooine/building/101"
            data = catalog.entity("locations", ident)["data"]
            lobby, = data["cells"]
            self.assertEqual(lobby["name"], "lobby")
            self.assertCountEqual([obj["template"] for obj in lobby["contents"]],
                                  [prop, egg, "fixture_guard", "fixture_captain"])
            captain = next(obj for obj in lobby["contents"] if obj["template"] == "fixture_captain")
            self.assertEqual(captain["coordinates"], [-969, 10, -2957])
            sign = next(obj for obj in lobby["contents"] if obj["template"] == prop)
            self.assertEqual(sign["target"], ["items", prop])
            self.assertEqual(sign["coordinates"], [-974, 8.25, -2950])
            self.assertEqual(sign["spawn_origin"]["type"], "Building master spawner")
            self.assertEqual(sign["spawn_origin"]["script"], table_script)
            self.assertEqual(sign["spawn_origin"]["table_source"]["line"], 3)
            self.assertEqual(sign["spawn_origin"]["attachments"][0]["kind"], "template")
            self.assertTrue(sign["spawn_origin"]["attachments"][0]["path"].endswith("/fixture/base.tpf"))
            only_sign, = catalog.placements("items", prop)["rows"]
            self.assertEqual((only_sign["map_location"], only_sign["cell_index"], only_sign["method"]),
                             (ident, 1, "Building master spawner"))

            stranger, = data["unresolved_cells"]["missing_room"]
            self.assertIsNone(stranger["coordinates"])
            self.assertEqual(stranger["local_coordinates"], [1, 2, 3])
            guard = next(obj for obj in lobby["contents"] if obj["template"] == "fixture_guard")
            self.assertEqual(guard["target"], ["npcs", "fixture_guard"])
            self.assertEqual(guard["coordinates"], [-970, 8.5, -2960])
            self.assertEqual(guard["spawn_origin"]["attachments"][0]["kind"], "buildout")
            self.assertEqual(guard["spawn_origin"]["caller_source"]["line"], 3)
            direct, = catalog.placements("npcs", "fixture_guard", scene="tatooine")["rows"]
            self.assertEqual((direct["map_location"], direct["spawn_origin"]["script"]), (ident, master))
            egg_row = next(obj for obj in lobby["contents"] if obj["template"] == egg)
            configured, = egg_row["spawner"]["entries"]
            self.assertEqual(configured["npc"], "fixture_egg_guard")
            self.assertEqual(egg_row["origin"], "server_buildout")
            from_egg, = catalog.placements("npcs", "fixture_egg_guard", scene="tatooine")["rows"]
            self.assertEqual(from_egg["spawner_template"], egg)

    def test_selected_github_repository_keeps_paths_in_an_extracted_deployment(self):
        with TemporaryDirectory() as work:
            root = Path(work)
            server, client = root / "tool-server", root / "tool-client"
            init(server)
            init(client)
            dsrc = server / "published-dsrc"
            record = quest(dsrc, "online", 7)
            tab(dsrc / SHARED_PATH / "buildout/buildout_scenes.tab", ("sceneName",), ("s",), ("test_scene",))
            tab(dsrc / SHARED_PATH / "buildout/areas_test_scene.tab",
                ("area", "x1", "z1", "x2", "z2"), ("s", "i", "i", "i", "i"), ("center", -500, -500, 500, 500))
            save(server / "src/fixture.cpp", "// Local engine source for the selected profile\n")
            server_revision = commit(server)
            assets = client / "published-client"
            stf(assets / "string/en/quest_n.stf", online="Published survey")
            save(assets / "ui/ui_planet_map.inc",
                 "<Page Name='test_scene' MapWidth='1000,1000'><Image SourceResource='ui_map_test_scene' "
                 "SourceRect='0,0,8,8'></Image></Page>")
            image = BytesIO()
            Image.new("RGB", (8, 8), "green").save(image, format="DDS")
            native_path = assets / "texture/ui_map_test_scene.dds"
            save(native_path, image.getvalue())
            model_assets(assets, image.getvalue())
            client_revision = commit(client)
            config = root / "sources.json"
            save(config, json.dumps({"sources": {
                "src": {"path": "tool-server", "subdirectory": "src"},
                "dsrc": {"github": "AlternateOrg/server", "revision": server_revision, "subdirectory": "published-dsrc"},
                "clientdata": {"github": "AlternateOrg/client", "revision": client_revision, "subdirectory": "published-client"},
            }}))

            with patch("archive_config.github_checkout", side_effect=lambda slug, *_: server if slug.endswith("/server") else client):
                profile = load_profile(config, root)
            build(profile.database, profile)
            catalog = Catalog(profile.database)
            source = SourceCatalog(server_revision, catalog=catalog)
            proof = "dsrc/" + record.relative_to(dsrc).as_posix()
            target = f"https://github.com/AlternateOrg/server/blob/{server_revision}/{record.relative_to(server).as_posix()}#L3"
            self.assertEqual(source.github(proof, 3), target)
            self.assertIsNone(source.github("dsrc/no_such_file.tab"))
            self.assertFalse(source.github("src/not_configured.cpp"))

            path = "clientdata/texture/ui_map_test_scene.dds"
            clientdata, tools, game_data = (profile.root(role) for role in ("clientdata", "client-tools", "game-data"))
            expected, used = audit_in_parallel(profile.database, clientdata, tools, game_data, profile.overlay)
            files = asset_destinations(used, clientdata, tools, game_data, profile.overlay)
            rows = describe_assets(files, None, github_assets(profile, files))
            by_path = {row["path"]: row for row in rows}
            self.assertEqual(by_path[path]["github"]["repository"], "AlternateOrg/client")
            self.assertEqual(by_path[path]["github"]["path"], native_path.relative_to(client).as_posix())
            self.assertIn("clientdata/texture/survey.dds", by_path)
            excerpts, count = local_source_zip(server_revision, profile)
            self.assertEqual(count, 1)
            package = root / "source-profile.zip"
            identity = {"revision": server_revision, "source_key": profile.key}
            write_package(package, "github", files, rows, profile.database,
                          sha256(profile.database.read_bytes()).hexdigest(), identity, excerpts, {})
            extracted = root / "extracted"
            with ZipFile(package) as archive:
                self.assertNotIn("upstream_source_index.json.gz", archive.namelist())
                self.assertNotIn(path, archive.namelist())
                archive.extractall(extracted)
            pinned_zip = root / f"snapshots/AlternateOrg/client-{client_revision}.zip"
            pinned_zip.parent.mkdir(parents=True)
            with ZipFile(pinned_zip, "w") as archive:
                for row in rows:
                    repository_path = row["github"]["path"]
                    archive.write(client / repository_path, f"client-{client_revision}/{repository_path}")
            self.assertEqual(verify_or_install(extracted, root / "snapshots"), (len(rows), len(rows)))
            self.assertEqual((extracted / path).read_bytes(), native_path.read_bytes())

            results = http_smoke(extracted, expected)
            self.assertEqual(results["maps"]["test_scene|full"], expected["maps"]["test_scene|full"])
            self.assertTrue(results["github_source"].startswith("https://github.com/AlternateOrg/"))
            self.assertEqual(results["local_source"], "src/fixture.cpp")
            self.assertEqual(results["models"]["items"]["triangles"], 1)
            self.assertEqual(results["models"]["items"]["textures"], 1)
            unpacked, _ = audit_in_parallel(extracted / ".local/catalog.sqlite3", extracted / "clientdata",
                                            extracted / "client-tools", extracted / "game/data", extracted / ".local/model-assets")
            compare_audits(expected, unpacked)
