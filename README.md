# Star Wars Galaxies Galactic Archive

The archive is a standalone local reference site for Star Wars Galaxies.
It links the game checkout's quest manifest to NPCs, locations, dialogue,
item templates, crafting recipes, resources, and player ships.

Open [the local archive](http://127.0.0.1:8768/) while the server is running.
The home dashboard explains what you can find in each section, shows
counts from the current index, and searches all sections from one field.

## Run

Run the launcher from the archive directory on Windows.

~~~powershell
./start.ps1
~~~

The first launch builds the SQLite index in the archive's `.local/` directory.
Pass `-Background` to run the server in a hidden process and record its PID
and logs there. The default game checkout is the sibling
`whitengold-ai/whitengold` directory under the same projects folder. Pass
`-GameRoot` or set `SWG_GAME_ROOT` to specify another checkout.
Use `-ClientData` and `-ClientTools` to select extracted asset directories.
The server listens on `127.0.0.1`, and `-Port` overrides the default port 8768.

Run these commands from the archive directory on WSL or Linux.

~~~bash
python3 -B build_catalog.py
python3 -B serve.py --port 8768
~~~

Python 3.10 or newer provides the web server, index, and asset decoders.
Pillow decodes the client resource icons, map images, and model textures. The browser uses
WebGL for models and can show a geometry preview when WebGL is unavailable.

Use `-Rebuild` to refresh an existing index after the schema, manifest, or supplied assets change. The
game source revision comes from `source_revision` in the manifest.
The game checkout provides `docs/quests/manifest.json`, source parsers,
and source records. The archive reads the game checkout and client asset
directories in place and stores its generated files in the archive directory.

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
  links directly to the major planets and zones with client map thumbnails
  where available. Each location has a description with sources. The archive
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
  Hoth and Ord Mantell use coordinate charts. The supplied client layout has
  no coordinate-aligned map artwork for either scene.
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
  and configured building spawn tables supply room contents and placements.
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
- **Source references** open files in `SWG-Source/whitengold` at a verified
  published commit. Text links include a line anchor when the published file
  matches the archive snapshot. Local recovery files open an excerpt from the
  indexed checkout. Coordinates and string codes with a copy icon still copy
  their displayed values.
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

Use `--overlay` for a staging directory and `--seed` to copy an existing
overlay into it. Review the JSON from `--report` before making the staging
directory the live `.local/model-assets/` overlay. Restart the archive to
pick up new files. The overlay records the repository, revision, asset path,
and manifest match for each recovered file in `item-model-recovery-sources.json`.
The recovery uses the supplied asset checkouts as read-only inputs.

## Portable deployment

Build a ZIP from the current SQLite catalog and the native files used by
the viewer.

```powershell
python -B build_deployment.py --output .local/deployments
```

The builder checks every indexed model and NPC appearance choice, each
map image and thumbnail, and every resource icon. It ships the original
asset bytes along with a SHA-256 manifest. Published source links open
GitHub and local source excerpts are included for the catalog revision.

Add `--with-github` to make a second ZIP that references files whose
bytes match the pinned SWG Source snapshots. Supply snapshot ZIPs with
repeatable `--snapshot` arguments or use the existing recovery cache.
On the deployment host, `python deployment_assets.py --download` fetches
only the missing files from their pinned GitHub revisions. Use
`--snapshot-dir PATH` to reuse existing repository ZIPs.

Use `verify_deployment.py` with the ZIP and `--work PATH` to test a
fresh extraction against the original viewer audit. Pass
`--github-bundle ZIP` to verify the reference package as well.

## Verify

Once the index exists, run the tests from the archive directory.

~~~powershell
python -B -m unittest discover -s tests -v
~~~

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
