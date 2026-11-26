"""Package the Galactic Archive and its indexed client assets."""

import argparse
from contextlib import ExitStack
from hashlib import sha256
from importlib.metadata import version
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import subprocess
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile
from zlib import crc32

from catalog_db import Catalog, packed
from catalog_provenance import HASH, SourceCatalog, TEXT_TYPES, UPSTREAM_INDEX, valid_source
from catalog_sources import DEFAULT_CLIENT, DEFAULT_TOOLS, PROJECT, ROOT
from deployment_assets import BLOCK, MANIFEST, file_hash
from deployment_trace import asset_destinations, audit_in_parallel
from recover_item_models import UpstreamAssets, write_report
from serve import ASSETS


HELPERS = ("devtools/quests/manifest_sources.py", "devtools/quests/manifest_scripts.py",
           "devtools/docker/validate_resource_distribution.py")
OVERLAY = PROJECT / ".local/model-assets"
DEFAULT_DB = PROJECT / ".local/catalog.sqlite3"
DEFAULT_OUTPUT = PROJECT / ".local/deployments"


def trace(catalog, client, tools, checkpoint=None):
    stat = catalog.path.stat()
    identity = {"database": str(catalog.path), "size": stat.st_size, "modified": stat.st_mtime_ns,
                "revision": catalog.meta()["revision"]}
    if checkpoint:
        previous = json.loads(checkpoint.read_text(encoding="utf-8"))
        if previous["paths"] != identity:
            raise ValueError("The catalog changed since the asset trace was created.")
        assets = asset_destinations(
            {(row["ref"], Path(row["source"]).resolve()) for row in previous["assets"]},
            client, tools, ROOT, OVERLAY,
        )
        for row in previous["assets"]:
            path = assets[row["dest"].lower()][1]
            value = path.stat()
            if value.st_size != row["bytes"] or value.st_mtime_ns != row["modified"]:
                raise ValueError(f"{row['dest']} changed since the asset trace was created.")
        return identity, assets, previous["audit"]

    audit, files = audit_in_parallel(catalog.path, client, tools, ROOT, OVERLAY)
    return identity, asset_destinations(files, client, tools, ROOT, OVERLAY), audit


def describe_assets(paths, snapshots):
    assets = []
    for index, (target, (ref, path)) in enumerate(paths.items(), 1):
        checksum = sha256()
        crc = length = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(BLOCK), b""):
                length += len(chunk)
                crc = crc32(chunk, crc)
                checksum.update(chunk)
        report = {"path": target, "native_path": ref, "bytes": length, "sha256": checksum.hexdigest()}
        if snapshots:
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


def local_source_zip(revision):
    """Keep only repository text that cannot be linked to its published GitHub file."""
    if not HASH.fullmatch(revision):
        raise ValueError("The catalog does not use a pinned game revision.")
    published = SourceCatalog(revision).published_paths
    tree = subprocess.check_output(["git", "ls-tree", "-r", "-z", revision, "--", "dsrc/sku.0", "src"], cwd=ROOT)
    local = []
    for row in tree.rstrip(b"\0").split(b"\0"):
        fields, _, raw = row.partition(b"\t")
        if not raw:
            continue
        _, kind, key = fields.split()
        path = raw.decode("utf-8")
        if kind == b"blob" and path not in published and valid_source(path) and PurePosixPath(path).suffix.lower() in TEXT_TYPES:
            local.append((path, key))
    result = BytesIO()
    with ZipFile(result, "w", compression=ZIP_DEFLATED, compresslevel=5) as zipfile:
        zipfile.comment = revision.encode("ascii")
        with subprocess.Popen(["git", "cat-file", "--batch"], cwd=ROOT, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE) as child:
            for path, key in local:
                child.stdin.write(key + b"\n")
                child.stdin.flush()
                header = child.stdout.readline().split()
                if len(header) != 3 or header[0] != key or header[1] != b"blob":
                    raise ValueError(f"Cannot read pinned source {path}")
                length = int(header[2])
                data = child.stdout.read(length)
                if len(data) != length or child.stdout.read(1) != b"\n":
                    raise ValueError(f"Incomplete pinned source {path}")
                zipfile.writestr(path, data)
            child.stdin.close()
            if child.wait():
                raise ValueError("Game source excerpt export failed.")
    return result.getvalue(), len(local)


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
        for name in ("serve.py", "deployment_assets.py", *(p.name for p in sorted(PROJECT.glob("catalog_*.py")))):
            bundle.write(PROJECT / name, name)
        for name in sorted(ASSETS):
            bundle.write(PROJECT / "static" / name, f"static/{name}")
        for helper in HELPERS:
            bundle.write(ROOT / helper, f"game/{helper}")
        bundle.writestr("game/dsrc/.archive-bundle", identity["revision"] + "\n")
        bundle.write(UPSTREAM_INDEX, UPSTREAM_INDEX.name)
        bundle.writestr("local_source_text.zip", local_sources, compress_type=ZIP_STORED)
        bundle.writestr("requirements.txt", f"Pillow=={version('Pillow')}\n")
        bundle.writestr("README.md", install_notes(mode))
        bundle.writestr(MANIFEST, packed(manifest).encode("utf-8"))
        for item in rows:
            if item["bundled"]:
                bundle.write(files[item["path"]][1], item["path"])
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
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--clientdata", type=Path, default=DEFAULT_CLIENT)
    parser.add_argument("--client-tools", type=Path, default=DEFAULT_TOOLS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--trace", type=Path, help="Reuse an asset trace for this exact catalog and source files.")
    parser.add_argument("--snapshot", type=Path, action="append", default=[], help="Pinned SWG Source ZIP, repeatable.")
    parser.add_argument("--with-github", action="store_true", help="Also build a GitHub reference package.")
    args = parser.parse_args()
    catalog = Catalog(args.database)
    args.output.mkdir(parents=True, exist_ok=True)
    identity, paths, audit = trace(catalog, args.clientdata.resolve(), args.client_tools.resolve(), args.trace)
    audit_path = args.output / "baseline-viewer-audit.json"
    write_report(audit_path, audit)
    snapshots = args.snapshot
    if args.with_github and not snapshots:
        snapshots = sorted((PROJECT / ".local/model-recovery").glob("*.zip"))
    with ExitStack() as stack:
        upstream = UpstreamAssets(stack, snapshots, None) if snapshots else None
        assets = describe_assets(paths, upstream)
    coverage = {"model_routes": audit["model_routes"], "model_status": audit["model_status"],
                "map_images": sum(value is not None for value in audit["maps"].values()),
                "resource_icons": sum(value is not None for value in audit["resource_icons"].values()),
                "decoded_texture_variants": sum(value is not None for value in audit["textures"].values())}
    source_zip, source_files = local_source_zip(identity["revision"])
    db_checksum = file_hash(catalog.path)
    print(f"{len(assets):,} native files and {source_files:,} local source excerpts", flush=True)
    bundle = args.output.resolve() / f"galactic-archive-{identity['revision'][:9]}.zip"
    packages = [write_package(bundle, "bundled", paths, assets, catalog.path, db_checksum, identity, source_zip, coverage)]
    if args.with_github:
        if not any("github" in entry for entry in assets):
            raise ValueError("No selected native assets matched the supplied GitHub snapshots.")
        reference = bundle.with_name(bundle.stem + "-github.zip")
        packages.append(write_package(reference, "github", paths, assets, catalog.path, db_checksum, identity, source_zip, coverage))
    result = {"coverage": coverage, "native_files": len(assets), "native_bytes": sum(row["bytes"] for row in assets),
              "source_excerpt_files": source_files, "baseline_audit": str(audit_path.resolve()), "packages": packages}
    write_report(args.output / "deployment-report.json", result)
    print(packed(result), flush=True)


if __name__ == "__main__":
    main()
