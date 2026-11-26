"""Extract the packages and verify them against the original viewer outputs."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
from threading import Thread
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener
from zipfile import ZipFile

from catalog_sources import PROJECT
from deployment_assets import BLOCK, MANIFEST, asset_manifest, relative_path, verify_or_install
from deployment_trace import audit_in_parallel, compare_audits
from recover_item_models import write_report


LOOPBACK = re.compile(r"Local: (http://127[.]0[.]0[.]1:[0-9]+/)")
RECOVERY_REFERENCE = "dsrc/sku.0/sys.server/compiled/game/datatables/buildout/tatooine/snapshot_functional_restore.tab"


def verify_archive_contents(bundle):
    manifest = json.loads(bundle.read(MANIFEST))
    expected = {entry["path"]: entry["sha256"] for entry in manifest["assets"] if entry["bundled"]}
    expected[".local/catalog.sqlite3"] = manifest["database_sha256"]
    for name, checksum in expected.items():
        digest = sha256()
        with bundle.open(name) as stream:
            for chunk in iter(lambda: stream.read(BLOCK), b""):
                digest.update(chunk)
        if digest.hexdigest() != checksum:
            raise ValueError(f"Archive checksum differs from the manifest for {name}.")


def extract_package(package, target):
    target.mkdir()
    with ZipFile(package) as bundle:
        seen = set()
        for entry in bundle.infolist():
            name = relative_path(entry.filename).as_posix()
            if name.casefold() in seen:
                raise ValueError(f"Duplicate package file {name}")
            seen.add(name.casefold())
        verify_archive_contents(bundle)
        bundle.extractall(target)
    return asset_manifest(target)


def serve_command():
    return [sys.executable, "-B", "-X", "utf8", "serve.py", "--port", "0"]


def serve_environment():
    env = os.environ.copy()
    for option in ("SWG_GAME_ROOT", "SWG_CLIENTDATA", "SWG_CLIENT_TOOLS", "PYTHONPATH"):
        env.pop(option, None)
    return env


def http_smoke(stage, icon_class):
    opener = build_opener(ProxyHandler({}))
    stderr = stage.parent / (stage.name + ".stderr.log")
    with stderr.open("wb") as log, subprocess.Popen(
        serve_command(), cwd=stage, env=serve_environment(), stdout=subprocess.PIPE, stderr=log,
        text=True, encoding="utf-8",
    ) as child:
        started = Queue()
        Thread(target=lambda: started.put(child.stdout.readline()), daemon=True).start()
        try:
            try:
                line = started.get(timeout=40)
            except Empty as error:
                raise ValueError(f"The extracted archive did not start. See {stderr}.") from error
            match = LOOPBACK.fullmatch(line.strip())
            if not match:
                raise ValueError(f"Unexpected extracted archive startup. See {stderr}.")
            url = match[1]

            def read(route, **params):
                target = url + route.lstrip("/")
                if params:
                    target += "?" + urlencode(params)
                with opener.open(target, timeout=20) as response:
                    return response.read()

            assert json.loads(read("/api/health"))["status"] == "ready"
            assert b"Galactic Archive" in read("/")
            assert b"mountModelViewer" in read("/model-viewer.js")
            raw_map = read("/api/map/tatooine/image")
            assert raw_map.startswith(b"\x89PNG\r\n\x1a\n")
            assert read("/api/map/tatooine/image", thumbnail="1").startswith(b"\x89PNG")
            assert read("/api/resource-image", **{"class": icon_class}).startswith(b"\x89PNG")
            sites = {
                "bantha": {"item": "object/tangible/loot/quest/bantha_doll.iff"},
                "relic": {"item": "col_lightsaber_1h_01_03"},
                "ship": {"ship": "player_bwing"},
                "building": {"building": "tatooine/building/1189173"},
                "npc": {"npc": "watto"},
                "sprite": {"item": "object/intangible/faction_perk/shuttle_extract.iff"},
            }
            rendered = {}
            for label, query in sites.items():
                model = json.loads(read("/api/model", **query))
                assert model["status"] == "available", (label, model.get("message"))
                if label == "sprite":
                    assert model["sprite_image"].startswith("data:image/png;base64,")
                else:
                    assert model["triangles"] >= 16, label
                    if label in ("bantha", "relic", "ship", "npc"):
                        assert model["textures"], label
                if label == "npc":
                    assert model["animations"], "NPC animation tracks are missing."
                rendered[label] = {"triangles": model.get("triangles"), "textures": len(model.get("textures", {})),
                                   "animations": len(model.get("animations", ()))}
            source = read("/source", path=RECOVERY_REFERENCE, line="626")
            assert b"source-excerpt" in source and b"source-line selected" in source
            print(f"{stage.name}: unpacked HTTP routes passed", flush=True)
            return rendered
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=8)


def unpacked_audit(stage, expected):
    results, _ = audit_in_parallel(stage / ".local/catalog.sqlite3", stage / "clientdata",
                                   stage / "client-tools", stage / "game", stage / ".local/model-assets")
    compare_audits(expected, results)
    return results


def compatible_manifests(first, second):
    def files(manifest):
        return {entry["path"]: {key: value for key, value in entry.items() if key != "bundled"}
                for entry in manifest["assets"]}

    if (first["database_sha256"] != second["database_sha256"] or first["coverage"] != second["coverage"]
            or files(first) != files(second)):
        raise ValueError("Portable and GitHub packages contain different native file sets.")


def verify_reference(package, github, cache, manifest, count, icon, bundle_http):
    external = extract_package(package, github)
    compatible_manifests(manifest, external)
    attempt = subprocess.run(serve_command(), cwd=github, env=serve_environment(), capture_output=True,
                             check=False, timeout=10)
    assert attempt.returncode == 2 and b"Install GitHub-referenced assets" in attempt.stderr
    found, restored = verify_or_install(github, cache, verify_bundled=False)
    assert found == count and restored == sum(not entry["bundled"] for entry in external["assets"])
    assert http_smoke(github, icon) == bundle_http
    return {"github_bundle": str(package.resolve()), "github_installed": restored}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--github-bundle", type=Path)
    parser.add_argument("--snapshot-dir", type=Path, default=PROJECT / ".local/model-recovery")
    parser.add_argument("--audit", type=Path, help="Original viewer audit generated by build_deployment.py.")
    parser.add_argument("--work", type=Path, required=True, help="New folder for the extracted verification packages.")
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True)
    expected_path = args.audit or args.bundle.parent / "baseline-viewer-audit.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    icon = next(name for name, checksum in expected["resource_icons"].items() if checksum)
    bundled = work / "bundled"
    manifest = extract_package(args.bundle, bundled)
    count = len(manifest["assets"])
    bundle_http = http_smoke(bundled, icon)
    with ThreadPoolExecutor(max_workers=1) as verification:
        reference = None
        if args.github_bundle:
            reference = verification.submit(verify_reference, args.github_bundle, work / "github",
                                            args.snapshot_dir.resolve(), manifest, count, icon, bundle_http)
        results = unpacked_audit(bundled, expected)
        supplemental = reference.result() if reference else {}
    verified = {"bundle": str(args.bundle.resolve()), "installed_assets": count, "http": bundle_http,
                "models": len(results["models"]), "maps": manifest["coverage"]["map_images"],
                "textures": manifest["coverage"]["decoded_texture_variants"],
                "resource_icons": manifest["coverage"]["resource_icons"], **supplemental}
    write_report(work / "verification.json", verified)
    print(json.dumps(verified), flush=True)


if __name__ == "__main__":
    main()
