"""Recover and audit the original render geometry used by archive item pages."""

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
from functools import cache
from hashlib import md5
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
from zipfile import ZipFile
import zlib

from archive_config import add_source_options, select_profile
from catalog_db import Catalog
from catalog_model_previews import model_refs, render_inventory_model
from catalog_models import MAX_ASSET_BYTES, ModelLibrary, normalize_ref
from catalog_sources import GAME_MODEL_PATHS


MODEL_SUFFIXES = {
    ".apt", ".lod", ".msh", ".cmp", ".pob", ".ssa", ".sat", ".mgn", ".lmg", ".skt",
    ".lat", ".ans", ".lsb", ".prt", ".spr", ".sht", ".eft", ".dds", ".cdf",
}
RENDER_WRAPPERS = {".lod", ".msh", ".cmp", ".mgn", ".lmg", ".sat"}
ASSET_PREFIXES = ("appearance/", "shader/", "effect/", "texture/", "clientdata/", "object/")
SNAPSHOT_NAME = re.compile(r"([a-z-]+)-([0-9a-f]{40})(?:\.zip)?\Z")


def published_assets(path):
    references = {}
    if path is not None and path.is_file():
        with path.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) == 9 and re.fullmatch(r"[a-fA-F0-9]{32}", parts[6]):
                    reference = normalize_ref(parts[-1])
                    if reference and reference.startswith(ASSET_PREFIXES):
                        references[reference] = parts[6].lower()
    return references


class ClientTree:
    """Read native files from a SWG client patch TRE (TreeFile_SearchNode)."""

    HEADER = struct.Struct("<4s4s7I")
    ENTRY = struct.Struct("<6I")
    MAX_SIZE = 64 * 1024 * 1024

    def __init__(self, data):
        self.data = data
        token, version, count, toc_at, toc_compressor, toc_size, names_compressor, names_size, names_length = (
            self.HEADER.unpack_from(data)
        )
        if token != b"EERT" or version not in {b"4000", b"5000"} or count > 300_000:
            raise ValueError("Unsupported client TRE index.")
        toc_length = count * self.ENTRY.size
        names_at = toc_at + (toc_size if toc_compressor else toc_length)
        toc = self.unpack(toc_at, toc_compressor, toc_size, toc_length)
        names = self.unpack(names_at, names_compressor, names_size, names_length)
        self.entries = {}
        for _, length, offset, compressor, size, name_at in self.ENTRY.iter_unpack(toc):
            stored = size if compressor else length
            if not 12 <= length <= MAX_ASSET_BYTES or offset + stored > toc_at:
                continue
            end = names.find(b"\0", name_at)
            if end == -1:
                raise ValueError("Missing client TRE filename.")
            ref = normalize_ref(names[name_at:end].decode("ascii"))
            if ref and ref.startswith(ASSET_PREFIXES):
                self.entries[ref] = (offset, compressor, size, length)

    def unpack(self, offset, compressor, size, length):
        stored = size if compressor else length
        if not self.HEADER.size <= offset <= len(self.data) or offset + stored > len(self.data) or length > self.MAX_SIZE:
            raise ValueError("Invalid client TRE file bounds.")
        block = self.data[offset:offset + stored]
        if compressor == 0:
            return block
        if compressor != 2:
            raise ValueError("Unknown client TRE compressor.")
        decoder = zlib.decompressobj()
        result = decoder.decompress(block, length + 1)
        if len(result) != length or not decoder.eof or decoder.unused_data:
            raise ValueError("Invalid client TRE compressed file.")
        return result

    def read(self, name):
        return self.unpack(*self.entries[name])


class UpstreamAssets:
    """Index pinned repository ZIPs by original game path without extracting their trees."""

    def __init__(self, stack, paths, manifest):
        self.members = defaultdict(list)
        self.revisions = {}
        self.manifest = published_assets(manifest)
        for filename in paths:
            archive = stack.enter_context(ZipFile(filename))
            label = archive.infolist()[0].filename.split("/", 1)[0]
            match = SNAPSHOT_NAME.fullmatch(label)
            if not match or match[1] not in {"mesh", "serverdata", "client-assets"}:
                raise ValueError(f"Expected a pinned SWG-Source snapshot, got {filename}.")
            repository, revision = match.groups()
            self.revisions[repository] = revision
            for member in archive.infolist():
                head, sep, reference = member.filename.partition("/")
                if not sep or head != label or member.is_dir():
                    continue
                if repository == "client-assets" and reference == "swgsource_3.0.tre":
                    if member.file_size > ClientTree.MAX_SIZE:
                        raise ValueError("The client patch TRE is larger than the supported recovery limit.")
                    tree = ClientTree(archive.read(member))
                    for logical in tree.entries:
                        if self.supports(logical):
                            self.members[logical].append((tree, logical, repository, revision, f"{reference}#{logical}"))
                    continue
                if not 12 <= member.file_size <= MAX_ASSET_BYTES:
                    continue
                if repository == "mesh":
                    if "/" in reference:
                        continue
                    reference = "appearance/mesh/" + reference
                logical = normalize_ref(reference)
                if not logical or not logical.startswith(ASSET_PREFIXES):
                    continue
                if self.supports(logical):
                    self.members[logical].append((archive, member, repository, revision, reference))

    @staticmethod
    def supports(ref):
        return PurePosixPath(ref).suffix in MODEL_SUFFIXES or ref.startswith("object/") and ref.endswith(".iff")

    def original(self, ref):
        expected = self.manifest.get(ref)
        candidate = None
        seen = set()
        for archive, member, repo, rev, relative in self.members.get(ref, ()):
            if isinstance(archive, ZipFile):
                signature = (member.file_size, member.CRC)
                if signature in seen:
                    continue
                seen.add(signature)
            data = archive.read(member)
            tag = data[8:12]
            if not (data.startswith(b"FORM") or ref.startswith("texture/") and data.startswith(b"DDS ")):
                continue
            if data.startswith(b"FORM") and (tag == b"APPR" or len(data) <= 12):
                continue
            checksum = md5(data).hexdigest()
            record = {"repo": f"SWG-Source/{repo}", "revision": rev,
                      "repository_path": relative.removeprefix("appearance/mesh/") if repo == "mesh" else relative,
                      "bytes": len(data), "md5": checksum,
                      "manifest": "match" if checksum == expected else "different_revision" if expected else "unlisted"}
            if not expected or checksum == expected:
                return data, record
            if candidate is None:
                candidate = data, record
        return candidate


class ItemAudit(ModelLibrary):
    """Use the viewer's geometry decoder and retain only the audit facts."""

    def __init__(self, *args, source_assets=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.source_assets = source_assets
        self.staged = {}

    @cache
    def _path(self, ref, prefix):
        current = super()._path(ref, prefix)
        logical = normalize_ref(ref)
        sources = self.source_assets
        if sources is None or logical not in sources.members:
            return current
        if current.is_file():
            if not prefix.startswith("appearance/") or current.suffix not in RENDER_WRAPPERS:
                return current
            with current.open("rb") as stream:
                header = stream.read(12)
            if header[:4] != b"FORM" or header[8:12] != b"APPR":
                return current
        original = sources.original(logical)
        if original is None:
            return current
        data, record = original
        overlay = self.roots[0]
        target = (overlay / logical).resolve()
        if not target.is_relative_to(overlay):
            raise ValueError("Asset path leaves the archive overlay.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.staged[logical] = record
        return target

    @cache
    def _material(self, shader):
        return super()._material(shader)

    def _png_texture(self, ref, use_alpha):
        if self.source_assets and ref:
            self._path(ref, "texture/")
        return None

    def _browse_animations(self, lat, rig):
        return []

    @cache
    def _geometry(self, appearance, cdf):
        model = super()._preview({"appearance": appearance}, cdf)
        brief = summarize(model)
        if model.get("meshes"):
            brief["assets"] = sorted({mesh["asset"] for mesh in model["meshes"]})
        return brief

    def _preview(self, result, cdf):
        return {**result, **self._geometry(result["appearance"], cdf)}

    @cache
    def get(self, template):
        return super().get(template)


def summarize(model):
    return {key: model[key] for key in
            ("status", "message", "appearance", "vertices", "triangles", "assets", "chosen", "model_selection",
             "geometry_kind", "sprite")
            if key in model}


def audit(catalog, models):
    with catalog.connect() as db:
        items = db.execute("SELECT id, name, data FROM entity WHERE kind='items' ORDER BY id").fetchall()
    previews, reports, totals, causes = {}, [], Counter(), Counter()
    for index, (ident, name, raw) in enumerate(items, 1):
        profile = json.loads(raw)
        refs = model_refs("items", profile)
        key = refs, profile.get("form") == "intangible"
        if key not in previews:
            model = render_inventory_model(models, "items", ident, profile)
            if model["status"] == "available":
                state = model.get("geometry_kind") or "rendered"
            elif not refs:
                state = "no_template"
            else:
                state = model["status"]
            previews[key] = state, summarize(model)
        state, detail = previews[key]
        totals[state] += 1
        if state != "rendered":
            causes[state, detail.get("message", "")] += 1
        reports.append({"id": ident, "name": name, "form": profile.get("form"), "category": profile.get("category"),
                        "definition_status": profile.get("status"), "state": state, "refs": refs, **detail})
        if index % 5000 == 0:
            print(f"{index:,} / {len(items):,} item pages checked", dict(totals),
                  f"{len(models.staged):,} files restored", flush=True)
    print("Coverage", dict(totals), flush=True)
    for (state, reason), count in causes.most_common(18):
        print(count, state, reason)
    return {"total": len(items), "counts": dict(totals), "asset_roots": [str(x) for x in models.roots],
            "items": reports}


def write_report(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    pending.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_source_options(parser)
    parser.add_argument("--overlay", type=Path, help="Write recovered assets to a staging directory.")
    parser.add_argument("--seed", type=Path, help="Copy an existing local overlay into a fresh staging directory.")
    parser.add_argument("--snapshot", type=Path, action="append", default=[], help="Pinned SWG-Source ZIP, repeatable.")
    parser.add_argument("--report", type=Path, help="Path for the JSON coverage report.")
    args = parser.parse_args()
    profile = select_profile(args)
    catalog = Catalog(args.database or profile.database)
    if catalog.metadata().get("source_key") != profile.key:
        parser.error("Rebuild the catalog with the selected source configuration before recovering model assets.")
    clientdata, tools = profile.root("clientdata"), profile.root("client-tools")
    overlay = args.overlay or profile.overlay
    report_path = args.report or profile.state / "model-recovery/item-model-coverage.json"
    if args.seed:
        if overlay.exists():
            parser.error("--seed requires a fresh staging directory.")
        shutil.copytree(args.seed, overlay)
    if args.snapshot:
        overlay.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        manifest = tools / "publish/client_data_tree_files.mft"
        sources = UpstreamAssets(stack, args.snapshot, manifest) if args.snapshot else None
        supplemental = (profile.root("game-data") / path for path in GAME_MODEL_PATHS)
        models = ItemAudit(clientdata, tools, supplemental, asset_overlay=overlay, source_assets=sources)
        report = audit(catalog, models)
        if sources:
            provenance = overlay / "item-model-recovery-sources.json"
            previous = json.loads(provenance.read_text(encoding="utf-8")) if provenance.is_file() else {}
            staged = {**previous.get("staged", {}), **models.staged}
            write_report(provenance, {"revisions": sources.revisions, "staged": staged})
            report.update(snapshots=sources.revisions, restored_files=len(staged))
            print(f"{len(staged):,} restored assets on record", flush=True)
        write_report(report_path, report)


if __name__ == "__main__":
    main()
