"""Package the Galactic Archive and its indexed client assets."""

import argparse
from contextlib import ExitStack
from hashlib import sha1, sha256
from importlib.metadata import version
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
from shutil import copyfileobj
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile
from zlib import crc32

from archive_config import PROJECT, SourceRepository, add_source_options, select_profile
from catalog_client_assets import ClientAssetPath
from catalog_db import Catalog, packed
from catalog_provenance import HASH, SourceCatalog, TEXT_TYPES, UPSTREAM_INDEX, valid_source
from deployment_assets import BLOCK, MANIFEST, file_hash
from deployment_trace import asset_destinations, audit_in_parallel
from recover_item_models import UpstreamAssets, write_report
from serve import ASSETS


def trace(catalog, profile, checkpoint=None):
    client = profile.clientdata
    tools, game_data = (profile.root(role) for role in ("client-tools", "game-data"))
    overlay = profile.overlay
    stat = catalog.path.stat()
    identity = {"database": str(catalog.path), "size": stat.st_size, "modified": stat.st_mtime_ns,
                "revision": catalog.metadata()["revision"]}
    if profile.key:
        identity["source_key"] = profile.key
    if checkpoint:
        previous = json.loads(checkpoint.read_text(encoding="utf-8"))
        if previous["paths"] != identity:
            raise ValueError("The catalog or source selection changed since the asset trace was created.")
        def traced_source(row):
            if isinstance(client, ClientAssetPath) and row["dest"].startswith("clientdata/"):
                return client / row["dest"].removeprefix("clientdata/")
            return Path(row["source"]).resolve()

        assets = asset_destinations(
            {(row["ref"], traced_source(row)) for row in previous["assets"]},
            client, tools, game_data, overlay,
        )
        for row in previous["assets"]:
            path = assets[row["dest"].lower()][1]
            value = path.stat()
            if value.st_size != row["bytes"] or value.st_mtime_ns != row["modified"]:
                raise ValueError(f"{row['dest']} changed since the asset trace was created.")
        return identity, assets, previous["audit"]

    audit, files = audit_in_parallel(catalog.path, client, tools, game_data, overlay)
    return identity, asset_destinations(files, client, tools, game_data, overlay), audit


def github_assets(profile, paths):
    """Resolve traced worktree files to their actual paths and object IDs on GitHub."""
    for role, destination in (("clientdata", "clientdata/"), ("client-tools", "client-tools/"),
                              ("game-data", "game/data/")):
        repo = profile.repositories.get(role)
        if not repo or not repo.github:
            continue
        root = repo.root.resolve()
        selected = {repo.repository_path(path.relative_to(root).as_posix()): target
                    for target, (_, path) in paths.items()
                    if target.startswith(destination) and path.is_relative_to(root)}
        if not selected:
            continue
        for remote_path, oid in repo.tree():
            if target := selected.get(remote_path):
                yield target, (oid, {"repository": repo.github, "revision": repo.revision, "path": remote_path})


def describe_assets(paths, snapshots, published=()):
    assets = []
    published = dict(published)
    for index, (target, (ref, path)) in enumerate(paths.items(), 1):
        checksum = sha256()
        pinned = published.get(target)
        object_id = sha1(f"blob {path.stat().st_size}\0".encode("ascii")) if pinned else None
        crc = length = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(BLOCK), b""):
                length += len(chunk)
                if snapshots:
                    crc = crc32(chunk, crc)
                if object_id:
                    object_id.update(chunk)
                checksum.update(chunk)
        report = {"path": target, "native_path": ref, "bytes": length, "sha256": checksum.hexdigest()}
        if pinned and object_id.hexdigest() == pinned[0]:
            report["github"] = pinned[1]
        elif snapshots:
            for archive, member, repo, revision, repository_path in snapshots.members.get(ref, ()):
                if (isinstance(archive, ZipFile) and member.file_size == length and member.CRC == crc
                        and sha256(archive.read(member)).hexdigest() == report["sha256"]):
                    report["github"] = {"repository": f"SWG-Source/{repo}", "revision": revision,
                                        "path": repository_path.removeprefix("appearance/mesh/")
                                        if repo == "mesh" else repository_path}
                    break
        assets.append(report)
        if index % 5000 == 0:
            print(f"{index:,} native files hashed", flush=True)
    return assets


def local_source_zip(revision, profile):
    """Package excerpts from the selected local repositories that have no GitHub link."""
    if not HASH.fullmatch(revision):
        raise ValueError("The catalog does not use a pinned dsrc revision.")
    if profile.key:
        local = {role: repo for role, repo in profile.repositories.items()
                 if repo.revision and not repo.github and role != "game-data"}
        published = set()
    else:
        local = {"dsrc": SourceRepository(profile.legacy_game, "dsrc", revision),
                 "src": SourceRepository(profile.legacy_game, "src", revision)}
        published = SourceCatalog(revision).published_paths

    result = BytesIO()
    count = 0
    with ZipFile(result, "w", compression=ZIP_DEFLATED, compresslevel=5) as excerpts:
        excerpts.comment = (profile.key or revision).encode("ascii")
        for role, repo in local.items():
            prefixes = ("sku.0",) if role == "dsrc" and not profile.key else ()
            prefix = repo.subdirectory + "/" if repo.subdirectory else ""
            selected = (
                (path, oid) for name, oid in repo.tree(*prefixes)
                if (path := role + "/" + name.removeprefix(prefix)) not in published
                and valid_source(path) and PurePosixPath(path).suffix.lower() in TEXT_TYPES
            )
            for path, data in repo.blobs(selected):
                excerpts.writestr(path, data)
                count += 1
    return result.getvalue(), count


def install_notes(mode):
    intro = """# Star Wars Galaxies Galactic Archive

Extract the whole ZIP to a new directory. The archive runs with Python 3.10 or newer.

```text
python -m pip install -r requirements.txt
"""
    if mode == "github":
        intro += "python deployment_assets.py --download\n"
    intro += """python -B serve.py
```

Open http://127.0.0.1:8768/ in a browser. Set `--port` to select a different port.
The SQLite catalog and local-only source excerpts are included.
Published source links point to the pinned repository on GitHub.

The Agent data guide is available at /llms.txt. The bundled catalog_agent.py
command reads .local/catalog.sqlite3 to search and browse the index offline
or export a ZIP of JSON Lines files.
"""
    if mode == "github":
        intro += """
The installer fetches only the missing files at their pinned GitHub revisions
and checks their hashes. If you already have the repository ZIPs, reuse them
with `python deployment_assets.py --snapshot-dir PATH`. After installation
the archive renders maps and models from local files.
"""
    else:
        intro += "\nAll native preview assets are included. The site can render them offline.\n"
    return intro


def write_package(path, mode, files, assets, database, db_checksum, identity, local_sources, coverage):
    rows = [{**item, "bundled": mode != "github" or "github" not in item} for item in assets]
    manifest = {"format": 1, "mode": mode, "catalog_revision": identity["revision"],
                "database_sha256": db_checksum, "coverage": coverage, "assets": rows}
    pending = path.with_suffix(".zip.pending")
    if path.exists() or pending.exists():
        raise FileExistsError(f"Use an output directory without {path.name} or {pending.name}.")
    print(f"Writing {path.name}", flush=True)
    with ZipFile(pending, "x", compression=ZIP_DEFLATED, compresslevel=4) as bundle:
        bundle.write(database, ".local/catalog.sqlite3")
        for name in ("serve.py", "archive_config.py", "deployment_assets.py",
                     *(p.name for p in sorted(PROJECT.glob("catalog_*.py"))),
                     *(p.relative_to(PROJECT).as_posix() for p in sorted((PROJECT / "archive_quests").glob("*.py")))):
            bundle.write(PROJECT / name, name)
        for name in sorted(ASSETS):
            bundle.write(PROJECT / "static" / name, f"static/{name}")
        if not identity.get("source_key") and UPSTREAM_INDEX.is_file():
            bundle.write(UPSTREAM_INDEX, UPSTREAM_INDEX.name)
        bundle.writestr("local_source_text.zip", local_sources, compress_type=ZIP_STORED)
        bundle.writestr("requirements.txt", f"Pillow=={version('Pillow')}\n")
        bundle.writestr("README.md", install_notes(mode))
        bundle.writestr(MANIFEST, packed(manifest).encode("utf-8"))
        for item in rows:
            if item["bundled"]:
                native = files[item["path"]][1]
                if isinstance(native, ClientAssetPath):
                    with native.open("rb") as source, bundle.open(item["path"], "w") as target:
                        copyfileobj(source, target)
                else:
                    bundle.write(native, item["path"])
        unpacked = sum(item.file_size for item in bundle.infolist())
    pending.replace(path)
    referenced = [item for item in rows if not item["bundled"]]
    return {
        "path": str(path), "compressed_bytes": path.stat().st_size, "unpacked_bytes": unpacked,
        "referenced_files": len(referenced), "referenced_bytes": sum(item["bytes"] for item in referenced),
        "repositories": sorted({item["github"]["repository"] for item in referenced}),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_source_options(parser)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trace", type=Path, help="Reuse an asset trace for this exact catalog and source files.")
    parser.add_argument("--snapshot", type=Path, action="append", default=[], help="Pinned SWG Source ZIP, repeatable.")
    parser.add_argument("--with-github", action="store_true", help="Also build a GitHub reference package.")
    args = parser.parse_args()
    try:
        profile = select_profile(args)
        catalog = Catalog(args.database or profile.database)
        if catalog.metadata().get("source_key") != profile.key:
            raise ValueError("Rebuild the catalog with the selected source configuration before packaging.")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    output = args.output or profile.state / "deployments"
    output.mkdir(parents=True, exist_ok=True)
    identity, paths, audit = trace(catalog, profile, args.trace)
    audit_path = output / "baseline-viewer-audit.json"
    write_report(audit_path, audit)
    snapshots = args.snapshot
    if args.with_github and not snapshots and not profile.key:
        snapshots = sorted((PROJECT / ".local/model-recovery").glob("*.zip"))
    with ExitStack() as stack:
        upstream = UpstreamAssets(stack, snapshots, None) if snapshots else None
        published = github_assets(profile, paths) if args.with_github else ()
        assets = describe_assets(paths, upstream, published)
    if args.with_github and not any("github" in entry for entry in assets):
        parser.error("No traced files matched a configured GitHub checkout or the supplied GitHub snapshots.")
    coverage = {"model_routes": audit["model_routes"], "model_status": audit["model_status"],
                "map_images": sum(value is not None for value in audit["maps"].values()),
                "resource_icons": sum(value is not None for value in audit["resource_icons"].values()),
                "decoded_texture_variants": sum(value is not None for value in audit["textures"].values())}
    source_zip, source_files = local_source_zip(identity["revision"], profile)
    db_checksum = file_hash(catalog.path)
    print(f"{len(assets):,} native files and {source_files:,} local source excerpts", flush=True)
    revision = identity.get("source_key", identity["revision"])
    bundle = output.resolve() / f"galactic-archive-{revision[:9]}.zip"
    packages = [write_package(bundle, "bundled", paths, assets, catalog.path, db_checksum, identity, source_zip, coverage)]
    if args.with_github:
        reference = bundle.with_name(bundle.stem + "-github.zip")
        packages.append(write_package(reference, "github", paths, assets, catalog.path, db_checksum, identity, source_zip, coverage))
    result = {"coverage": coverage, "native_files": len(assets), "native_bytes": sum(row["bytes"] for row in assets),
              "source_excerpt_files": source_files, "baseline_audit": str(audit_path.resolve()), "packages": packages}
    write_report(output / "deployment-report.json", result)
    print(packed(result), flush=True)


if __name__ == "__main__":
    main()
