import {
  code, columnHeading, escapeHTML as h, facts, href, integer, link, loadingIndicator, localized, locationLink, placementSection, plainObject,
  dropChance, dropDetails, pagination, readable, relatedRows, resourceIcon, section, sectionSources, sourceText, tag,
} from "./ui.js";

const stack = content => `<div class="stack">${content}</div>`;
const article = (content, sidebar) => `<div class="article-grid">${stack(content)}<aside class="stack">${sidebar}</aside></div>`;
const empty = message => `<p class="muted">${h(message)}</p>`;
const val = value => typeof value === "number" ? integer(value) : h(value ?? "—");
const yesNo = value => value == null ? null : value ? "Yes" : "No";
const nameCode = label => label?.code ? code(label.code) : null;
const destinations = {
  datapad: "Datapad",
  inventory: "Inventory",
  bank: "Bank",
  mission_bag: "Mission bag",
  world: "World or building cell",
  cell: "Building cell",
  object_location: "At another object",
  slot: "Object slot",
  container: "Container",
  container_or_inventory: "Container or inventory",
  unknown: "Unresolved",
};

function table(label, columns, rows, fallback, sort) {
  if (!rows.length) return empty(fallback || "No authored values indexed.");
  const headings = columns.map(column => Array.isArray(column) ? columnHeading(...column, sort) : columnHeading(column));
  return `<div class="table-wrap" tabindex="0" role="region" aria-label="${h(label)}">
    <table class="data-table"><thead><tr>${headings.join("")}</tr></thead>
    <tbody>${rows.map(row => `<tr>${row.map(cell => `<td>${cell ?? "—"}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function rich(title, detail = "", proof) {
  return `<div class="rich-row"><div>${title}</div>${detail ? `<div class="rich-detail">${detail}</div>` : ""}${sourceText(proof)}</div>`;
}

function relatedPanel(kind, entity, role, title, fallback = "No linked records were recovered.") {
  return section(title, relatedRows(kind, entity.card.id, entity, role, fallback));
}

function optionalRelated(kind, entity, panels) {
  return panels.filter(([role]) => entity.related[role]?.count)
    .map(([role, title]) => relatedPanel(kind, entity, role, title)).join("");
}

function relatedEntry(entity, role, ident) {
  return entity.related[role]?.rows.find(row => row.id === ident);
}

function linkedRef(entity, role, kind, id, fallback = id) {
  return id ? link(kind, id, relatedEntry(entity, role, id)?.card?.name || fallback) : code(fallback);
}

export function modelPanel(title) {
  return section(title, `<div id="entity-model" aria-live="polite" aria-busy="true">${loadingIndicator("Loading model…")}</div>`);
}

function itemOverview(entity) {
  const { data, related } = entity;
  const native = Object.entries(data.native_attributes || {}).map(([key, item]) =>
    [h(readable(key)), val(item.value), sourceText(item.source)]);
  const crafting = optionalRelated("items", entity, [["crafted_by", "Crafted by"], ["schematic", "Schematic details"]]);
  const ships = optionalRelated("items", entity, [["ship_deeds", "Ship deed for"], ["ship_products", "Chassis for"],
    ["ship_devices", "Control device for"], ["ship_components", "Default component for"], ["deploys", "Deploys"],
    ["deeds", "Deployment deeds"], ["holds_resources", "Holds resources"]]);
  const values = Object.keys(data.attributes).length ? section("Item attributes", plainObject(data.attributes)) : "";
  const nativeValues = native.length ? section("Template attributes", table("Native item attributes",
    ["Attribute", "Value", "Source"], native)) : "";
  const form = { physical: "Physical item template", intangible: "Intangible template" }[data.form];
  const storage = ["datapad", "inventory", "bank", "mission_bag"]
    .filter(destination => data.script_destinations?.[destination])
    .map(destination => destinations[destination]).join(", ");
  const shortcuts = [
    (hasLoot(entity) || data.acquisitions.length || related.reward_items_quests?.count) &&
      `<a href="${href("items", data.id)}?view=acquisition">How to get this item</a>`,
    `<a href="${href("items", data.id)}?view=locations">Where to find this item</a>`,
    (related.used_in_recipes?.count || related.recipe_references?.count) &&
      `<a href="${href("items", data.id)}?view=crafting">View other crafting links</a>`,
  ].filter(Boolean);
  return article(section("Description", localized(data.description, "The item description is unresolved.")) +
    crafting + ships + values + nativeValues,
  section("Item facts", facts([
    ["Type", h(data.category)], ["Required level", data.level == null ? null : integer(data.level)],
    ["Item form", form && h(form)], ["Scripted storage", storage && h(storage)],
    ["Template", data.template && code(data.template)],
    ["Client template", data.client_template && code(data.client_template)],
    ["Appearance", data.appearance && code(data.appearance)],
    ["Base definition", data.base_template && code(data.base_template)], ["String code", nameCode(data.name_code)],
  ]) + shortcuts.map(value => `<p class="fact-action">${value}</p>`).join("")) + sectionSources(data.sources));
}

function scriptAcquisitionTable(label, entries, fallback) {
  const rows = entries.map(row => [
    h(destinations[row.destination] || destinations.unknown) + (row.cell ? `<small class="entity-sub">${h(row.cell)}</small>` : ""),
    `${code(row.call)} <small class="entity-sub">${h(row.method)}()</small>`,
    sourceText(row.source),
    [row.selection && h(readable(row.selection)), sourceText(row.table_source)].filter(Boolean).join(" "),
  ]);
  return table(label, ["Destination", "Creation call and method", "Script source", "Item selection"], rows, fallback);
}

function hasLoot(entity) {
  return entity.has_loot || entity.data.acquisitions.some(row => row.kind === "loot");
}

function lootTables(entity) {
  const rows = entity.data.acquisitions.filter(row => row.kind === "loot")
    .map(row => [h(readable(row.table)), code(row.column), sourceText(row.source)]);
  return rows.length ? `<details class="panel section"><summary>Loot table records</summary>
    ${table("Loot tables", ["Table", "Column", "Source"], rows)}</details>` : "";
}

function lootSources() {
  return section("Dropped by", `<p class="muted">Drop chance is the chance of at least one copy per eligible defeat at the authored base level.
    Open a row to see its rolls, conditions, and source records.</p>
    <form id="item-loot-filter" class="placement-filters" role="search" aria-label="Filter NPC drop sources">
      <label class="filter-label">NPC name<input class="control-input" name="q" type="search" maxlength="160"></label>
      <button class="button" type="submit">Filter</button>
    </form>
    <div id="loot-npc-results" tabindex="-1" aria-live="polite" aria-busy="true">
      ${loadingIndicator("Loading loot sources…")}</div>`) +
    '<div id="other-loot-results" hidden></div>';
}

function itemAcquisition(entity) {
  const scripts = entity.data.acquisitions.filter(row => row.kind === "script");
  const game = scripts.filter(row => !row.development);
  const tools = scripts.filter(row => row.development);
  const gameLabel = "Scripted grants and spawns";
  const scriptsPanel = scripts.length ? section(gameLabel, (game.length ?
    scriptAcquisitionTable(gameLabel, game) : "") +
    (tools.length ? `<details><summary>Development and GM scripts (${integer(tools.length)})</summary>
      ${scriptAcquisitionTable("Development and GM scripts", tools)}</details>` : "")) : "";
  const vendors = entity.data.acquisitions.filter(row => row.kind === "vendor")
    .map(row => [h(readable(row.table)), code(row.column), plainObject(row.terms), sourceText(row.source)]);
  const vendorPanel = vendors.length ? section("Vendor listings",
    table("Vendor listings", ["Table", "Column", "Listing terms", "Source"], vendors)) : "";
  const sources = (hasLoot(entity) ? lootSources() : "") + optionalRelated("items", entity,
    [["reward_items_quests", "Quest rewards"]]) + vendorPanel + scriptsPanel + lootTables(entity);
  return stack((sources || section("Acquisition", empty("No acquisition sources were recovered for this item."))) +
    optionalRelated("items", entity, [["variants", "Item variants"]]));
}

export function lootNPCResults(result, filtered, sort = "chance-desc") {
  const rows = result.rows.map(npc => [
      `${link("npcs", npc.id, npc.name, "entity-name")}<small class="entity-sub">${h(npc.category)}</small>`,
      npc.level == null ? "—" : integer(npc.level),
      `<span class="drop-chance">${dropChance(npc)}</span>`, dropDetails(npc),
  ]);
  const count = result.total ? `<p class="count-label">${integer(result.total)} ${result.total === 1 ? "NPC" : "NPCs"}</p>` : "";
  const columns = [["NPC", "name"], ["Level", "level"], ["Drop chance", "chance"], "Rolls and source records"];
  return count + table("NPC drop sources", columns, rows,
    filtered ? "No NPC drop sources match this search." : "No NPC drop chance was recovered for this item.", sort) +
    pagination(result, "loot-page");
}

export function otherLootResults(sources) {
  return sources.length ? section("Other loot sources", table("Other loot sources",
    ["Source", "Map levels", "Chance", "Rolls and source records"], sources.map(entry => [
      `<strong>${h(entry.name)}</strong><small class="entity-sub">${h(entry.condition)}</small>`,
      entry.level == null ? "—" : h(`${entry.level}–${entry.max_level}`),
      `<span class="drop-chance">${dropChance(entry)}</span>`,
      `${dropDetails(entry)}${sourceText(entry.source)}`,
    ]))) : "";
}

function itemView(entity, view) {
  if (view === "crafting") return stack(
    relatedPanel("items", entity, "crafted_by", "Crafted by", "No authored recipe explicitly produces this item.") +
    optionalRelated("items", entity, [["recipe_references", "Client recipe references"], ["schematic", "Schematic details"],
      ["used_in_recipes", "Used in recipes"], ["teaches_schematics", "Teaches these recipes"],
      ["base_item", "Item template"], ["variants", "Item variants"]]));
  if (view === "acquisition") return itemAcquisition(entity);
  if (view === "locations") return placementSection("items", entity);
  if (view === "equipment") return relatedPanel("items", entity, "equipment_npcs", "NPC equipment");
  if (view === "model") return modelPanel("In-game item model");
  if (view === "quests") return stack([["reward_items_quests", "Rewarded by"], ["required_items_quests", "Required by"],
    ["starter_items_quests", "Starts"], ["objective_items_quests", "Objectives"]].map(([role, title]) =>
    relatedPanel("items", entity, role, title, "No linked quests.")).join(""));
  return itemOverview(entity);
}

function outputName(entity, output) {
  return output.catalog_id ? linkedRef(entity, "products", "items", output.catalog_id) : code(output.item_id);
}

function recipeProducts(entity, outputs = entity.data.outputs) {
  const rows = outputs.map(output => [outputName(entity, output),
    output.quantity == null ? "—" : integer(output.quantity) + (output.quantity_kind === "prototype_objects" ? " prototype" : ""),
    output.units_per_object?.min != null ? range(output.units_per_object) : "—",
    h(output.catalog_id ? readable(output.status) : "Unresolved template"), sourceText(output.source)]);
  return table("Crafted outputs", ["Item", "Quantity", "Units per object", "Evidence", "Source"],
    rows, "No product template is resolved.");
}

function range(value) {
  const minimum = value.min;
  const maximum = value.max;
  if (minimum === maximum) return val(minimum);
  return `${val(minimum)}–${val(maximum)}`;
}

function recipeOverview(entity) {
  const { data } = entity;
  const station = data.station;
  const notices = data.issues.length ? section("Coverage", `<ul class="source-issues">${data.issues.map(issue =>
    `<li>${h(issue)}</li>`).join("")}</ul>`) : "";
  const xp = data.xp.length ? section("Crafting experience", table("Crafting experience", ["Type", "XP", "Level", "Source"],
    data.xp.map(row => [code(row.type), val(row.amount), val(row.level), sourceText(row.source)]))) : "";
  const primaryOutputs = new Set(data.outputs.map(row => row.item_id));
  const otherOutputs = data.table_recipes.flatMap(row => row.outputs.filter(output => !primaryOutputs.has(output.item_id)));
  const time = Object.entries(data.craft_time).map(([phase, value]) => [
    readable(phase) + " seconds per complexity", val(value.seconds_per_complexity),
  ]);
  return article((data.description?.text || data.description?.code ? section("Description", localized(data.description)) : "") +
    section("Crafted outputs", recipeProducts(entity)) +
    (otherOutputs.length ? section("Additional crafting table outputs", recipeProducts(entity, otherOutputs)) : "") + xp +
    optionalRelated("schematics", entity, [["ships", "Player ships"], ["schematic_item", "Schematic item"]]) + notices,
  section("Crafting facts", facts([
    ["Crafting type", station?.category && h(readable(station.category))],
    ["Minimum crafting access", station?.minimum_access && h(readable(station.minimum_access))],
    ["Minimum crafting level", station?.minimum_crafting_level != null ? val(station.minimum_crafting_level) : null],
    ["Station required", station?.when_schematic_filtering_enabled && station?.required != null ?
      yesNo(station.required) + " when schematic filtering is enabled" : yesNo(station?.required)],
    ["Complexity", data.complexity != null ? val(data.complexity) : null],
    ["Items per factory crate", data.items_per_container != null ? val(data.items_per_container) : null],
    ["Name string code", nameCode(data.name_label)], ...time,
  ])) + sectionSources(data.sources));
}

function ingredientRow(entity, ingredient) {
  const target = ingredient.resource_link ? relatedEntry(entity, "resources", ingredient.resource_link) : null;
  const title = ingredient.resource_link
    ? linkedRef(entity, "resources", "resources", ingredient.resource_link, ingredient.name)
    : ingredient.catalog_id ? linkedRef(entity, "ingredients", "items", ingredient.catalog_id, ingredient.name) : h(ingredient.name);
  const sprite = resourceIcon(target?.card?.icon, "resource-icon");
  return `<li class="ingredient-line">${sprite}<div>${title || code(ingredient.ingredient)}
    ${ingredient.ingredient ? `<small>${code(ingredient.ingredient)}</small>` : ""}${nameCode(ingredient.name_label) || ""}
    </div><strong>${ingredient.count == null ? "—" : val(ingredient.count) + " ×"}</strong></li>`;
}

function recipeIngredients(entity, slots = entity.data.slots) {
  if (!slots.length) return empty("No ingredient slots were recovered.");
  return `<div class="recipe-slots">${slots.map((slot, index) => {
    const options = slot.options.map((option, optionIndex) => {
      const label = readable(String(option.ingredient_type || "").replace(/^IT_/, ""));
      const heading = slot.options.length > 1 ? `<b>Alternative ${optionIndex + 1}</b> ` : "";
      const ingredients = option.ingredients.length ? `<ul class="ingredient-list">${option.ingredients.map(value =>
        ingredientRow(entity, value)).join("")}</ul>` : empty("Ingredient unresolved.");
      return `<div class="recipe-option"><p>${heading}${h(label)}${option.skill_command ? " · " + code(option.skill_command) : ""}</p>${ingredients}</div>`;
    }).join("");
    return `<div class="recipe-slot"><div class="slot-heading"><h3>${h(slot.name || "Slot " + (index + 1))}</h3>
      ${slot.optional == null ? "" : tag(slot.optional ? "Optional" : "Required")}</div>
      ${nameCode(slot.name_label) || ""}${slot.optional_skill_command ? code(slot.optional_skill_command) : ""}
      ${options || empty("Ingredient options are unresolved.")}${sourceText(slot.source)}</div>`;
  }).join("")}</div>`;
}

function recipeTraining(entity) {
  const { data } = entity;
  function grants(label, rows) {
    return section(label, table(label, ["Skill", "Schematic group", "Source"], rows.map(grant =>
      [h(grant.label?.text || readable(grant.skill_id)) + " " + code(grant.skill_id), code(grant.group),
        sourceText(grant.source) + sourceText(grant.group_source)])));
  }
  const tools = ["compatible_stations", "compatible_tools"].flatMap(field =>
    (data.station?.[field] || []).map(entry => [h(readable(field)), entry.catalog_id ?
      linkedRef(entity, "crafting_tools", "items", entry.catalog_id) : code(entry.item_id), sourceText(entry.source)]));
  const limits = data.limited_uses.map(entry => [
    entry.catalog_id ? linkedRef(entity, "learned_from", "items", entry.catalog_id) : code(entry.raw_item || entry.item_id),
    val(entry.uses), sourceText(entry.source),
  ]);
  const missions = data.crafting_missions.map(entry => [h(entry.name || entry.profession), val(entry.level),
    code(entry.item_id), sourceText(entry.source)]);
  const scripts = data.manufacture_scripts.concat(data.skill_commands);
  return stack(grants("Learned from skills", data.grants) +
    (data.revocations.length ? grants("Removed by skills", data.revocations) : "") +
    (tools.length ? section("Compatible stations and tools", table("Compatible stations and tools", ["Kind", "Template", "Source"], tools)) : "") +
    (limits.length ? section("Limited-use schematics", table("Limited-use schematics", ["Source item", "Uses", "Source"], limits)) : "") +
    (missions.length ? section("Crafting missions", table("Crafting missions", ["Name or profession", "Level", "Item", "Source"], missions)) : "") +
    (scripts.length ? section("Crafting commands and scripts", scripts.map(value => rich(code(value))).join("")) : "") +
    sectionSources(data.sources));
}

function experimentation(entity) {
  const rows = entity.data.experimentation.map(entry =>
    [h(entry.name || entry.key), h(entry.experiment || "—"), range(entry), sourceText(entry.source)]);
  const tableRows = entity.data.table_recipes.flatMap(recipe => (recipe.attributes || []).map(entry =>
    [h(entry.name || entry.key), h(recipe.kind || "—"), range(entry), sourceText(entry.source)]));
  return stack(section("Experimentation", table("Experimental attributes", ["Attribute", "Group", "Range", "Source"], rows)) +
    (tableRows.length ? section("Additional crafting table ranges", table("Crafting table ranges",
      ["Attribute", "Recipe entry", "Range", "Source"], tableRows)) : ""));
}

function schematicView(entity, view) {
  if (view === "recipe") {
    const otherRows = entity.data.table_recipes.filter(recipe => recipe.slots?.length);
    return stack(section("Ingredients", recipeIngredients(entity)) + otherRows.map(recipe =>
      section("Additional crafting table entry", sourceText(recipe.source) + recipeIngredients(entity, recipe.slots))).join(""));
  }
  if (view === "training") return recipeTraining(entity);
  if (view === "experimentation") return experimentation(entity);
  return recipeOverview(entity);
}

function resourceOverview(entity) {
  const { data } = entity;
  const icon = data.icon;
  const art = icon ? `<figure class="resource-art">
    ${resourceIcon(icon.class, "resource-art-image", data.name + " game icon")}
    <figcaption>${icon.class !== data.id ? "Game icon from " +
      (icon.class === data.parent ? linkedRef(entity, "parent", "resources", data.parent) : code(icon.class)) : "Game icon"}
      ${sourceText({ path: icon.sprite })}${sourceText({ path: icon.texture })}</figcaption></figure>` : "";
  const parent = data.parent ? linkedRef(entity, "parent", "resources", data.parent) : null;
  const values = data.attributes.map(row => [h(row.name), code(row.id), val(row.min), val(row.max)]);
  return article(section("Resource attributes", table("Resource attribute ranges",
    ["Attribute", "Code", "Minimum", "Maximum"], values, "This class has no authored attribute ranges.")) +
    optionalRelated("resources", entity, [["containers", "Resource container"]]),
  section("Resource facts", art + facts([["Type", h(data.category)], ["Parent class", parent],
    ["Resource index", val(data.index)], ["Random name class", code(data.random_name_class)],
    ["Recycled", yesNo(data.recycled)], ["Permanent", yesNo(data.permanent)],
    ["String code", nameCode(data.name_label)]])) +
  (Object.keys(data.population).length ? section("Population limits", plainObject(data.population)) : "") +
  sectionSources(data.sources));
}

function resourceView(entity, view, metadata) {
  const { data } = entity;
  if (view === "distribution") {
    const pools = data.distribution.map(row => [locationLink(row.scene, metadata),
      val(row.pool_min), val(row.pool_max), sourceText(row.source)]);
    const prices = data.station_prices.map(row => [locationLink(row.scene, metadata), val(row.price), sourceText(row.source)]);
    return stack((pools.length || !prices.length ? section("Resource distribution", table("Authored world distribution",
      ["World", "Minimum pool size", "Maximum pool size", "Source"], pools,
      "This class has no direct entry in the distribution table.")) : "") +
      (prices.length ? section("Mining station prices", table("Mining station prices",
        ["Space zone", "Credits per unit", "Source"], prices)) : ""));
  }
  if (view === "schematics") return relatedPanel("resources", entity, "schematics", "Recipes requiring this resource class",
    "No ingredient slot directly names this class. Recipes can also accept one of its parent classes.");
  if (view === "children") return relatedPanel("resources", entity, "children", "Resource subclasses",
    "This resource has no subclasses.");
  return resourceOverview(entity);
}

function shipOverview(entity) {
  const { data } = entity;
  const spawn = Object.fromEntries(Object.entries(data.attributes).filter(([key]) => key.startsWith("default_spawn_"))
    .map(([key, value]) => [key.replace("default_spawn_", ""), value]));
  const flight = Object.fromEntries(Object.entries(data.attributes).filter(([key]) => !key.startsWith("default_spawn_")));
  const cert = data.certifications.map(value => rich(code(value))).join("");
  return article((data.description?.text || data.description?.code ? section("Description", localized(data.description)) : "") +
    (data.look_at_text?.text ? section("Inspection text", localized(data.look_at_text)) : "") +
    section("Chassis tuning", plainObject(flight)) +
    (Object.keys(spawn).length ? section("Default spawn configuration", plainObject(spawn)) : "") +
    optionalRelated("ships", entity, [["chassis", "Chassis family"], ["variants", "Ship variants"]]),
  section("Ship facts", facts([
    ["Chassis tier", data.attributes.chassis_tier != null ? val(data.attributes.chassis_tier) : null],
    ["Role", h(data.category)], ["Ship template", data.template && code(data.template)],
    ["Client template", data.shared_template && code(data.shared_template)],
    ["Model asset", data.model_asset && code(data.model_asset)], ["Cockpit", data.cockpit_asset && code(data.cockpit_asset)],
    ["Interior", data.interior_asset && code(data.interior_asset)], ["String code", nameCode(data.name_ref)],
  ])) + (cert ? section("Pilot certifications", cert) : "") + sectionSources(data.sources));
}

function shipView(entity, view) {
  const { data } = entity;
  if (view === "components") {
    const details = data.slots.map(slot => [
      h(slot.name), h((slot.compatibility_names || slot.compatibility).join(", ")),
      val(slot.hit_weight), yesNo(slot.targetable), slot.catalog_id ?
        linkedRef(entity, "components", "items", slot.catalog_id) + " " + code(slot.default_component) : code(slot.default_component),
      Object.keys(slot.default_component_stats).length ? plainObject(slot.default_component_stats) : "—",
    ]);
    return section("Component slots", table("Ship component slots",
      ["Slot", "Compatible components", "Hit weight", "Targetable", "Default component", "Default values"], details));
  }
  if (view === "acquisition") {
    const proof = data.relationships.map(row => {
      const role = { deed: "deeds", product: "products", control_device: "control_devices", schematic: "schematics" }[row.kind];
      const target = relatedEntry(entity, role, row.catalog_id || row.item);
      const name = target?.card ? link(target.kind, target.id, target.card.name) : code(row.item);
      const staticItem = row.static_item ? " · " + link("items", row.static_item, row.static_item) : "";
      return rich(h(readable(row.kind)) + " · " + name + staticItem,
        (row.attributes && Object.keys(row.attributes).length ? plainObject(row.attributes) : "") +
        sourceText(row.attribute_source), row.source);
    }).join("");
    return stack(section("Deeds, schematics, and control devices", proof || empty("No acquisition mechanism was recovered.")) +
      optionalRelated("ships", entity, [["schematics", "Crafting recipes"], ["deeds", "Deed items"]]));
  }
  if (view === "model") return modelPanel("In-game ship model");
  return shipOverview(entity);
}

export function inventoryDetail(kind, entity, view, metadata) {
  if (kind === "items") return itemView(entity, view);
  if (kind === "schematics") return schematicView(entity, view);
  if (kind === "ships") return shipView(entity, view);
  return resourceView(entity, view, metadata);
}
