"""Serve the read-only Galactic Archive on the local loopback address."""

import argparse
from functools import lru_cache
from hashlib import blake2s
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from catalog_db import BROWSE_SORT, DIALOGUE_SORT, PLACEMENT_SORT, Catalog, KINDS, SCHEMA, packed
from catalog_locations import MapLibrary
from catalog_map import AREA_LAYERS, LAYERS, bounds_from_query
from catalog_model_previews import render_model_profile
from catalog_models import ModelLibrary
from catalog_provenance import SourceCatalog, valid_source
from catalog_sources import DEFAULT_CLIENT, DEFAULT_TOOLS, GAME_MODEL_PATHS, PROJECT, ROOT


DEFAULT_DB = PROJECT / ".local/catalog.sqlite3"
DEFAULT_PORT = 8768
STATIC = PROJECT / "static"
ASSETS = {"index.html", "site.css", "app.js", "ui.js", "views.js", "inventory-views.js",
           "map-viewer.js", "model-viewer.js", "building-viewer.js"}
MAP_ROUTE = re.compile(r"/api/map/([a-zA-Z][a-zA-Z0-9_-]*)(?:/(image|markers))?")
VERSION_PLACEHOLDER = b"__ARCHIVE_VERSION__"


def archive_version(directory, catalog_signature):
    """Identify the front end and the index that the running server loaded."""
    signatures = [catalog_signature]
    for name in sorted(ASSETS):
        try:
            stat = (directory / name).stat()
            signatures.append((name, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signatures.append((name, None))
    return blake2s(packed(signatures).encode("ascii"), digest_size=8).hexdigest()


def normalize_int(value, default, lower, upper):
    try:
        return min(upper, max(lower, int(value)))
    except (ValueError, TypeError):
        return default


class ArchiveServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, catalog, clientdata, client_tools):
        super().__init__(address, ArchiveHandler)
        self.catalog = catalog
        stat = catalog.path.stat()
        self.catalog_signature = (stat.st_mtime_ns, stat.st_size)
        self.meta = catalog.meta()
        self.sources = SourceCatalog(self.meta["revision"])
        self.meta["map_layers"] = {**LAYERS, **AREA_LAYERS}
        for chart in self.meta["maps"].values():
            scene = chart["scene"]
            chart["area_layers"] = catalog.map_area_counts(scene, chart["bounds"])
            if chart.get("grid_bounds"):
                chart["grid_area_layers"] = catalog.map_area_counts(scene, chart["grid_bounds"])
            if chart.get("cities"):
                chart["cities"] = catalog.with_map_details(scene, chart["cities"])
        self.maps = MapLibrary(clientdata)
        self.models = ModelLibrary(clientdata, client_tools, (ROOT / path for path in GAME_MODEL_PATHS),
                                   asset_overlay=PROJECT / ".local/model-assets")

    def version(self):
        return archive_version(STATIC, self.catalog_signature)

    @lru_cache(maxsize=8)
    def render_model(self, kind, ident, variant=""):
        profile = self.catalog.model_profile(kind, ident)
        if profile is None:
            return None
        return render_model_profile(self.models, kind, ident, profile, variant)


class ArchiveHandler(BaseHTTPRequestHandler):
    server_version = "StarWarsGalaxiesArchive"

    def respond(self, body, kind, status=200, *, cache="no-cache", location=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if location:
            self.send_header("Location", location)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                         "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        try:
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except ConnectionError:
            pass

    def json(self, data, status=200):
        self.respond(packed(data).encode("utf-8"), "application/json; charset=utf-8", status)

    def missing(self, subject):
        self.json({"error": f"{subject} not found."}, HTTPStatus.NOT_FOUND)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        request = urlsplit(self.path)
        path = request.path.rstrip("/") or "/"
        if path == "/source":
            try:
                fields = parse_qs(request.query, max_num_fields=4)
                reference = fields.get("path", [""])[0]
                if len(reference) > 512 or not valid_source(reference):
                    raise ValueError("Invalid archive source path.")
                line = normalize_int(fields.get("line", [None])[0], None, 1, 1_000_000)
                location = self.server.sources.github(reference, line)
                if location:
                    self.respond(b"", "text/plain", HTTPStatus.FOUND, location=location)
                else:
                    self.respond(self.server.sources.local_page(reference, line), "text/html; charset=utf-8")
            except ValueError as error:
                self.json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if path.startswith("/api/"):
            try:
                fields = parse_qs(request.query, max_num_fields=30)
                self.api(path, fields)
            except ValueError as error:
                self.json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        name = path.removeprefix("/")
        if name in ASSETS:
            file = STATIC / name
        elif path == "/" or name.split("/", 1)[0] in KINDS:
            file = STATIC / "index.html"
        else:
            self.missing("Page")
            return
        if not file.is_file():
            self.missing("Asset")
            return
        content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if file.suffix == ".js":
            content_type = "application/javascript"
        body = file.read_bytes()
        if file.name == "index.html":
            body = body.replace(VERSION_PLACEHOLDER, self.server.version().encode("ascii"))
        self.respond(body, content_type + "; charset=utf-8", cache="no-store")

    def api(self, path, fields):
        def field(name, default="", maximum=240):
            return fields.get(name, [default])[0][:maximum]

        def sort_order(columns, default=""):
            sort = field("sort") or default
            if sort and sort.removesuffix("-desc") not in columns:
                raise ValueError("Unknown sort order.")
            return sort

        catalog = self.server.catalog
        if path == "/api/health":
            health = {"status": "ready", "revision": self.server.meta["revision"],
                      "schema": SCHEMA, "version": self.server.version()}
            self.respond(packed(health).encode("utf-8"), "application/json; charset=utf-8", cache="no-store")
            return
        if path == "/api/meta":
            self.json(self.server.meta)
            return
        if match := MAP_ROUTE.fullmatch(path):
            metadata = self.server.meta["maps"].get(match[1])
            if not metadata:
                self.missing("Map")
            elif match[2] == "markers":
                self.map_markers(metadata, field)
            elif match[2] == "image":
                image = self.server.maps.map_image(metadata, thumbnail=field("thumbnail") == "1")
                if image:
                    mime, contents = image
                    self.respond(contents, mime, cache="private, max-age=86400")
                else:
                    self.missing("Map artwork")
            else:
                self.json(metadata)
            return
        if path == "/api/model":
            supplied = [(kind, value) for key, kind in
                        (("npc", "npcs"), ("ship", "ships"), ("item", "items"), ("building", "locations"))
                        if (value := field(key))]
            if len(supplied) != 1:
                raise ValueError("Supply one NPC, ship, item, or building ID.")
            model = self.server.render_model(*supplied[0], field("variant"))
            self.json(model) if model is not None else self.missing("Model entry")
            return
        if path == "/api/resource-image":
            icon = self.server.meta["resource_icons"].get(field("class"))
            pixels = self.server.models.texture_png(icon["texture"], True) if icon else None
            if pixels:
                self.respond(pixels, "image/png", cache="private, max-age=86400")
            else:
                self.missing("Resource artwork")
            return
        page = normalize_int(field("page"), 1, 1, 20_000)
        size = normalize_int(field("size"), 30, 1, 80)
        query = field("q", maximum=160)
        status = field("status")
        if path == "/api/dialogue":
            result = catalog.dialogue(field("table"), query, status, page, size, sort_order(DIALOGUE_SORT))
            self.json(result) if result is not None else self.missing("Conversation")
            return
        if path == "/api/search":
            self.json(catalog.search(query))
            return
        if path == "/api/placement":
            ident = field("id")
            result = catalog.placement(ident) if re.fullmatch(r"[0-9a-f]{24}", ident) else None
            self.json(result) if result is not None else self.missing("Placement")
            return
        kind = field("type")
        if kind not in KINDS:
            raise ValueError("Unknown catalog section.")
        if path == "/api/list":
            self.json(catalog.list(
                kind, query=query, scene=field("scene"), category=field("category"), status=status,
                min_level=normalize_int(field("min"), None, 0, 999),
                max_level=normalize_int(field("max"), None, 0, 999),
                acquisition=field("acquisition"), storage=field("storage"), page=page, size=size,
                sort=sort_order(BROWSE_SORT, "name"),
            ))
        elif path == "/api/entity":
            result = catalog.entity(kind, field("id"))
            self.json(result) if result is not None else self.missing("Entry")
        elif path == "/api/item-loot-npcs" and kind == "items":
            self.json(catalog.item_loot_npcs(field("id"), page, size, query=query,
                                             sort=sort_order(("name", "level", "chance"), "chance-desc")))
        elif path == "/api/related":
            self.json(catalog.links(kind, field("id"), field("role"), page, size))
        elif path == "/api/placements" and kind in ("items", "locations", "npcs"):
            self.json(catalog.placements(kind, field("id"), scene=field("scene"), query=query,
                                         spawn_type=field("spawn"), page=page, size=size, sort=sort_order(PLACEMENT_SORT)))
        elif path == "/api/npc-drops" and kind == "npcs":
            self.json(catalog.npc_drops(field("id"), query, page, size,
                                       sort_order(("name", "chance"), "chance-desc")))
        else:
            self.missing("Endpoint")

    def map_markers(self, metadata, field):
        chart = metadata["bounds"]
        if field("extent") == "full":
            chart = metadata.get("grid_bounds", chart)
        selected = set(field("layers", maximum=256).split(","))
        layers = tuple(layer for layer in LAYERS if layer in selected)
        listing = field("list") == "1"
        bounds = bounds_from_query(field("bounds"), chart, listing)
        query = field("q", maximum=100)
        catalog = self.server.catalog
        scene = metadata["scene"]
        if listing:
            page = normalize_int(field("page"), 1, 1, 50_000)
            result = catalog.map_list(scene, layers, bounds, query, page, 20)
        else:
            columns = normalize_int(field("columns"), 14, 1, 28)
            rows = normalize_int(field("rows"), 12, 1, 24)
            result = catalog.map_markers(scene, layers, bounds, query, columns, rows)
            area_layers = set(field("areas").split(",")) & AREA_LAYERS.keys()
            result["areas"] = catalog.map_areas(scene, area_layers, bounds, query)
        self.json(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--clientdata", type=Path, default=DEFAULT_CLIENT)
    parser.add_argument("--client-tools", type=Path, default=DEFAULT_TOOLS)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    if (PROJECT / "asset-manifest.json").is_file():
        from deployment_assets import missing_reference_assets

        if missing_reference_assets(PROJECT):
            parser.error("Install GitHub-referenced assets with python deployment_assets.py --download.")
    if not args.database.is_file():
        parser.error("Run build_catalog.py to create the local index before starting the server.")
    catalog = Catalog(args.database)
    if catalog.meta().get("schema") != SCHEMA:
        parser.error("Rebuild the catalog for the current schema.")
    with ArchiveServer(("127.0.0.1", args.port), catalog, args.clientdata, args.client_tools) as server:
        print(f"Local: http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
