# Star Wars Galaxies Galactic Archive

The archive is a standalone local reference site for Star Wars Galaxies.
It indexes the configured game data and links quests to NPCs, locations, dialogue,
item templates, crafting recipes, resources, and player ships.

Open [the local archive](http://127.0.0.1:8768/) while the server is running.
The home dashboard explains what you can find in each section, shows
counts from the current index, and searches all sections from one field.

## Configure the source repositories

From the Archive checkout, copy [the configuration template](archive-sources.example.json)
to `.local/sources.json` and replace the example paths. On Windows, use
these commands the first time you set up the Archive.

~~~powershell
New-Item -ItemType Directory -Path .local -Force | Out-Null
Copy-Item archive-sources.example.json .local/sources.json
~~~

The top-level `sources` object selects your inputs. Paths may be absolute
or relative to the directory containing the configuration file. Windows
paths can use forward slashes, as in `F:/SWGProjects/my-server`.

| Input | What it supplies |
| --- | --- |
| `dsrc` | Required Git checkout containing `sku.0/sys.server/compiled/game`. The indexer reads server and shared datatables, scripts, and object templates at the selected commit. |
| `clientdata` | Required extracted client data in its original layout, with directories such as `object/`, `appearance/`, and `texture/`. This supplies maps, models, icons, and compiled tables. |
| `client-tools` | Optional supplemental client assets. English STF files here take precedence over `clientdata/string/en`. One of those inputs must contain the English strings. |
| `src` | Optional native source checkout for code references. |
| `game-data` | Optional staged, compiled `data` directory for additional model files. |

For each input, set either `path` for a local directory or `github`
for a repository in `OWNER/REPOSITORY` form. Set optional inputs to
`null` or omit them. Use `subdirectory` when the input is nested
inside the selected repository. In a split `dsrc` checkout, `sku.0/`
is typically at the repository root.

This example reads server code and data from subdirectories of one GitHub
repository and client assets from an extracted local directory.

~~~json
{
  "sources": {
    "src": {"github": "your-organization/swg-server", "revision": "main", "subdirectory": "src"},
    "dsrc": {"github": "your-organization/swg-server", "revision": "main", "subdirectory": "dsrc"},
    "clientdata": {"path": "../../game-inputs/clientdata"}
  }
}
~~~

- **GitHub sources** accept a branch name or a full commit SHA in
  `revision`. Omit it to follow the default branch. The Archive resolves
  branches through GitHub and caches each selected commit locally. If a named
  branch is missing, the error identifies the repository and its default branch. Private
  repositories use your Git credential helper. When using the upstream
  [SWG-Source repositories](https://github.com/SWG-Source), omit the revision
  to follow the default branch. The old client data repository is published
  as [SWG-Source/clientdata_OLD](https://github.com/SWG-Source/clientdata_OLD).
  This repository supplies some client assets. Original game maps also
  require `ui/ui_planet_map.inc` and its referenced `texture/ui_map_*.dds`
  files in the selected client data. A complete extracted client directory
  can supply those files alongside GitHub server sources.
- **Local server sources** use their current Git commit by default.
  Set `revision` to select another branch or commit already available in
  the checkout.
- **Local client assets** are read from the directory you select. If you
  specify a Git revision for one of those asset directories, check out
  that revision before using it.

The indexer generates the quest manifest from the selected data and scripts.
An optional top-level `quest_annotations` path can supply manually
documented script-only quests or additional evidence. That JSON file can
contain `quests`, `annotations`, and `systems` arrays.

## Run locally

Install Python 3.10 or newer, Git, and Pillow. Pillow decodes the client
artwork and textures.

~~~powershell
python -m pip install Pillow
./start.ps1
~~~

The Windows launcher finds `.local/sources.json` automatically. It builds
the index on first launch and when the selected revisions or configuration
change. Select another file with `-SourcesConfig PATH`. Use `-Port`
to choose a port or `-Background` to run a hidden process with its PID
and logs under `.local/`.

## Regenerate the catalog

1. Update the selected source checkouts or edit the configuration. Commit
   local `src` and `dsrc` changes first. For a GitHub branch, the
   build resolves its latest commit. For a pinned source, edit the
   `revision` field to choose a newer commit.
2. Stop the Archive instance serving that catalog.
3. From the Archive checkout, rebuild the index and start the server with
   the same configuration file. These commands work on Windows, Linux,
   and WSL.

~~~text
python -B build_catalog.py --sources .local/sources.json
python -B serve.py --sources .local/sources.json
~~~

On Windows, `./start.ps1 -Rebuild` does both. Use it after changing
loose client assets even when the selected Git revisions are the same.

Each build invocation clears `.local/build.log` and mirrors its progress and
warnings to stdout. Warnings identify missing map assets, buildout and portal
files, unresolved model assets encountered during indexing, and unreadable
resource or string tables. A launch that reuses a catalog lists its recorded warnings
in the fresh log. An explicit `--database` path writes a `.build.log` beside
that database.

Every rebuild creates a fresh SQLite catalog in a temporary file. A
successful build replaces the previous catalog, so changed records and
links are updated and deleted source records disappear. A failed build
leaves the last successful catalog available. The generated catalog and
recovered asset cache live under `.local/profiles/` for each configuration.

Open [the catalog metadata](http://127.0.0.1:8768/api/meta) after starting
the server to check the resolved source revisions and record counts. To
update an extracted deployment, build a new ZIP from the regenerated catalog
and install it in a fresh directory. See [Portable deployment](#portable-deployment).

Existing installations can continue using `SWG_GAME_ROOT` or
`-GameRoot` with the original manifest-backed catalog.

## Agent access

Give a web agent [the Agent data guide](http://127.0.0.1:8768/llms.txt).
Its [route manifest](http://127.0.0.1:8768/api/agent/manifest) covers all eight
collections. An agent can search for a name, open its stable game ID, and
follow the returned relationships for tasks such as tracing a quest chain,
finding a creature's spawn locations, or comparing crafting ingredients.
The APIs also expose full dialogue, map layers, models, and source records.

For file access, generate a JSON Lines bundle from the current catalog.

~~~powershell
python -B catalog_agent.py --sources .local/sources.json export --output .local/agent-data/catalog.zip
~~~

Extract the ZIP and start with its `llms.txt` and `manifest.json`.
Use `names.jsonl` to find IDs and open the matching collection for
the complete record. Separate files carry relationships, placements,
loot rules, map geometry, and source evidence. The site serves artwork
and renderable models through its asset APIs.

An agent with command access can query the generated catalog directly.
These examples use the current configuration. Pass `--database PATH`
when working from a generated SQLite file or a portable deployment.

~~~powershell
python -B catalog_agent.py --sources .local/sources.json search "Watto"
python -B catalog_agent.py --sources .local/sources.json list quests --scene tatooine
python -B catalog_agent.py --sources .local/sources.json placements npcs watto --scene tatooine
~~~

Use `entity KIND ID` to read details, `related KIND ID ROLE` to
page through a relationship, and `dialogue ID --query TEXT` to find
a line or string code. For loot comparisons, `loot --item ITEM_ID`
returns ranked NPCs and links to evidence. It keeps each item ID and
scripted condition separate. Use `--output PATH.json` with `loot`
to save a lookup to a shareable file.

Rerun the export after regenerating the catalog to refresh the shareable ZIP.

## What the pages show

Select a sortable column header to reorder the matching records across pages.
Select it again to reverse the order.

- **Quests** carry all manifest IDs, including historical and development
  records. Their tabs expose journal text, authored objectives, linked
  dialogue, and typed progression edges. Rewards retain the authored timing
  and alternate selections. Recommended levels and actual level gates are
  separate fields. Resolved starts link to the exact site when the game source
  identifies a building or landmark. Location Quests tabs include starts on
  that site and within its recorded child locations.
- **Dialogue** pairs each recovered `@table:key` with the English client
  line, the caption in the server script when available, and source lines.
  Conversation pages link to the NPCs whose scripts use the dialogue.
  The alignment filter surfaces missing keys and differences without
  rewriting either source. The importer checks the expected string paths
  in `client-tools` first and uses the supplied `clientdata/string/en`,
  which contains the English STF files for this installation. Unresolved
  runtime expressions remain visible. A quest reference identifies the
  associated table. The dialogue branches determine which lines can play.
- **NPCs** include authored creature profiles, space mobiles, placed NPCs,
  and quest contacts. Their Quests tab shows series starters by default,
  with continuations listed as other offers. A start is based on the
  incoming progression in the recovered data. The level shown is the
  quest's recommended level, with a minimum shown where an authored gate
  exists. Combat facts show the authored profile and matching base stat
  curve when available. Weapon assignments distinguish a fixed template,
  a possible selection from a weapon group, and unresolved assignments.
  **Locations** lists each indexed spawn origin and supports world and
  spawner-type filters. Placement rows link to their world map or building
  cell. Regional lair and herd rows link to the region. The index keeps
  definition-only, development, and runtime-position sources with their
  evidence. Buildout settings, spawn eggs and patrols, building and heroic
  tables, mission and lair populations, script calls, and space squads and
  waves contribute to the same reverse index.
  **Loot** shows possible drops, the chance per table roll, and the combined
  chance of at least one copy from an eligible defeat. A repeated row keeps
  its full share of the roll, including rows in nested tables. Required
  items, collection rolls, level gates, and known encounter-only grants
  have separate evidence. The displayed defeat chance uses the authored
  base level and standard server settings. Conditional script probabilities
  are shown on their own where the script supplies the roll.
- **Locations** include worlds, named regions, buildout areas, transport
  points, and the original client's points of interest. The landing page
  links directly to the major planets and selected zones with client map
  thumbnails where available. Other scenes are available in the location
  browser. Each location has a description with sources. The archive
  uses the client POI text when present, published lore for known places,
  and local location records for other areas.
  Planet and settlement pages show their child locations in the overview
  and Locations tab. Breadcrumbs link to the same parents. Children are
  keyed by location ID and use authored scene membership and boundaries.
  Hoth's runtime scene ID, adventure2, resolves to its single location page.
  Matching travel and building records share a directory entry and link to
  each other's detail views.
  Distinct registrations with the same name show their IDs in child lists.
- **Maps** fill the location page width and use the supplied game artwork.
  Locations with coordinate-aligned artwork in the selected client show the
  game map. Other mapped locations show a coordinate chart.
  Open Layers to choose markers, add the coordinate grid, or toggle region,
  no-build, and building outlines. Region outlines use the original circles
  and rectangles. The no-build layer also includes circles defined by placed
  objects' server templates. Hover an outline or pin for its description and
  extent. Right-click to copy that point's X and Z or inspect features under
  the pointer. Shift+F10 opens the same panel from a focused map or pin.
  Major cities and settlements have visible
  labels and a city picker where the scene has those locations. A
  numbered group opens a list of its markers
  and can be zoomed in. Drag to pan, use the zoom buttons or pinch,
  and use Fit entire chart for the full extent. Fullscreen and keyboard
  controls are available. Placement coordinates use game X, Y, and Z in
  meters. Server buildouts, compiled client buildouts and snapshots,
  quest waypoints, and other authored tables supply the coordinates.
  When a server placement uses a room recovered from the client assets,
  the marker shows both source records. Spawn markers indicate authored
  anchors. Rows with unresolved coordinate frames have no plotted position.
- **Building pages** open from the teal outlines on city-scale maps or the
  planet's Buildings tab. Gray outlines mark scenery buildings. Footprints
  come from client radar shapes, collision geometry, or appearance bounds
  where only bounds survive. The building page identifies the source used.
  Portal layouts supply cell names and floor plans. Buildouts, snapshots,
  and master spawner scripts supply room contents and placements. The
  index follows scripts attached in buildouts and inherited from building
  templates. Each scripted spawn links to the script and attachment,
  plus the datatable row when it defines the position. Dedicated spawner
  objects show their configured targets and counts in their own rows.
  Each object shows world and local coordinates where the source resolves
  them. Local offsets for nested objects are labeled as parent-relative.
  The dashed outline uses client radar, collision, appearance bounds, or
  the recovered floor meshes. An outline based on the interior is labeled
  as such. Hover a
  cell for its name and object count. Select it to jump to its expanded
  contents, or open an item directly from its marker or table row. The
  Locations tab opens the building's placement on the world map.
  Runtime-only player placements and arbitrary script outcomes require live
  game data. A portal CRC mismatch leaves the authored cell slots visible.
- **Source references** open files in the configured GitHub repositories
  at their selected commits. Parsed data records link to their source line
  where available. Text from local checkouts opens as an excerpt. Coordinates
  and string codes with a copy icon copy their displayed values.
- **Items** include server templates, static variants, client references,
  ship components, resource containers, and records from the quest,
  equipment, vendor, loot, and crafting data. Use the Craftable acquisition
  filter to find products with an explicit authored recipe. Item pages
  link to recipes that produce or consume them, quest rewards, vendor
  and loot entries, and an item model when a client template is available.
  The Locations tab lists indexed placements, configured object spawns,
  and quest turn-ins with recovered coordinates. Filter by world or location
  name. A location link highlights the object on the world map or floor plan.
  Acquisition lists scripted grants and spawns alongside vendor, loot, and quest
  sources. Script entries include the creation call, enclosing method, copyable
  source line, and table row when the call reads an authored table. The scanner
  prefers the preserved Java and uses legacy scripts where Java is unavailable.
  It follows explicit loot-table selections, labels possible rolls and other
  conditional results, and groups development and GM scripts separately.
  Template names assembled from table values require a matching catalog template.
  Calls with unresolved runtime item names remain unlinked.
  Item facts and browse filters distinguish an object's template form from a
  script-confirmed destination such as the datapad or inventory.
  **Acquisition** links each indexed creature and space mobile using the same
  drop probabilities as their NPC pages. Expand a source to see its rolls,
  conditions, and evidence, or open **Loot table records** for the authored
  references. Treasure map chest chances are listed by map level and group
  size. Rare pickpocket sources show the chance for the rare loot roll.
  Existing item Loot links open Acquisition.
  Placed spawners show their configured creatures or objects, population,
  radius, and respawn timing when authored values are available. Spawn eggs
  resolve their own object IDs against the scene's POI spawn table. Static mob
  spawners resolve the scene's ground spawn table. Area and patrol spawners
  list their possible creatures and the total population for that placement.
  A creature's Locations tab links to those same spawner records.
- **Schematics** show crafted outputs, ingredient alternatives, linked
  resource classes, learning skills, station and tool requirements,
  crafting XP, and experimental ranges. Prototype count, stack yield,
  and factory crate capacity each have their own fields. Partial definitions
  and references from the client or crafting tables retain their evidence.
- **Resources** show the authored resource hierarchy, quality ranges,
  population limits, world distribution, mining station prices, and the
  recipes that directly name each class. Icons come from the supplied
  client sprite and its shader's main DDS texture. The atlas follows the
  client's parent-class lookup when a resource shares an icon. The two
  top-level classes have no matching client sprite.
- **Ships** link player chassis and template variants to components,
  pilot certifications, deeds, control devices, and crafting schematics.
  Chassis tuning, spawn loadouts, and deed or crafting ranges appear with
  their own sources. Additional server and client reference entries carry
  their source status. The Model tab displays render geometry when the
  supplied client assets contain it.
- **Models** are available on item, ship, building, and NPC pages. They
  resolve game templates through the supplied client files and
  the NPC customization tables. Choose an authored appearance when the
  creation script selects from a pool. A bounds-only appearance can use
  its surviving native mesh. The model viewer checks verified files in
  the archive's `.local/model-assets/` directory before the supplied
  client data, using the original game-relative paths. Space mobiles
  use their authored pilot
  communications appearance when the supplied hull geometry is unavailable.
  The viewer offers orbit, zoom,
  rest pose, and compatible native clips. It exposes the original asset
  paths and reports missing geometry, unsupported formats, and unavailable
  animation. When the client files omit render meshes, the viewer identifies
  any in-game display model, interior floor mesh, collision mesh, or bounds
  used for the preview. Runtime palette effects and live equipment
  attachments can differ from the preview.

### Recover original item meshes

Run `recover_item_models.py` to audit all indexed item pages with the same
decoder the website uses. Supply pinned SWG Source ZIPs with repeatable
`--snapshot` arguments to recover referenced native meshes, wrappers, and
materials at their original paths. This also reads the bundled client patch
TRE. The audit distinguishes rendered geometry from sprites, proxy geometry,
and unresolved entries.

Pass `--sources` to use a selected configuration's catalog and asset roots.
Use `--overlay` for a staging directory and `--seed` to copy an existing
overlay into it. The default overlay belongs to the selected configuration
and source versions. Restart the archive to pick up recovered files. The
overlay records the repository, revision, asset path, and manifest match
for each recovered file in `item-model-recovery-sources.json`.
The recovery uses the supplied asset checkouts as read-only inputs.

## Portable deployment

After regenerating the catalog, build a ZIP using the same source configuration.
Choose a fresh output directory for each build. The builder packages the current
SQLite index and the native files used by the viewer.

```powershell
python -B build_deployment.py --sources .local/sources.json --output .local/deployments/refresh-1
```

The builder checks every indexed model and NPC appearance choice, each
map image and thumbnail, and every resource icon. It ships the original
asset bytes along with a SHA-256 manifest. Published source links open
GitHub and local source excerpts are included for the catalog revision.

Add `--with-github` to make a second ZIP that refers to native files whose
bytes match the pinned GitHub checkouts. Selected files unique to local
directories are included in the package. For the original source layout,
previously downloaded recovery ZIPs can also be supplied through repeatable
`--snapshot` arguments.

On a deployment host, `python deployment_assets.py --download` fetches missing assets from the pinned
revisions and verifies their checksums. Private GitHub asset downloads can
use a read token provided in `GH_TOKEN` or `GITHUB_TOKEN`. To reuse archive
ZIPs, pass `--snapshot-dir PATH` and place each ZIP at
`OWNER/REPOSITORY-COMMIT.zip` within that directory. The site runs from the extracted
package after asset installation.

Use `verify_deployment.py` with the ZIP and `--work PATH` to test a fresh extraction
against the original viewer audit. Use `--github-bundle ZIP` to verify the
reference package as well. Pass `--download` to the verifier to fetch
references that lack a cached ZIP.

## Verify

Run the tests from the Archive checkout.

~~~powershell
python -B -m unittest discover -s tests -v
~~~

The source-configuration tests use temporary repositories. The historical
content checks use the original game and asset fixtures when available.

The tests cover manifest coverage, dialogue alignment, world-coordinate
conversion, map filters and coincident markers, location descriptions,
native assets, NPC spawn sources, nested loot rolls, resource inheritance,
item acquisitions, recipe links, and ship templates. The catalog describes
the source snapshot. Its metadata includes `spawn_coverage`,
`loot_coverage`, and `model_coverage`. The SQLite `spawn_audit` table
retains creation calls whose target expressions could not be classified.
Saved or manually created spawners and runtime-only script state require
live server evidence. Exact appearances still require the missing client
assets or a source that identifies the actor.
