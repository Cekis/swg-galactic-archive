import {
  code, columnHeading, copyMarkup, coordinateValue, escapeHTML as h, facts, href, integer, link, linkRows, loadingIndicator, localized, locationId, locationLink, locationName, opt,
  pagination, placementSection, plainObject, readable, relatedRows, resourceIcon, searchResultGroups, section, sections, sectionSources,
  sourceText, spawnDetails, spawnSummary, tag, worldBounds, worldCoordinates,
} from "./ui.js";
import { inventoryDetail, modelPanel } from "./inventory-views.js";

const statuses = {
  authored: "Authored", historical_definition: "Historical", development: "Development",
  reference_asset: "Reference asset", script_referenced_asset: "Script-referenced asset",
  disabled_in_ground_table: "Disabled",
  referenced: "Referenced item", authored_recipe: "Authored recipe", partial_source: "Partial source",
  client_reference: "Client reference", reference_only: "Unresolved reference", table_recipe: "Table recipe",
  base_template: "Base template", "server-authored": "Player ship", "server-reference": "Server reference",
  "client-reference": "Client reference",
};

const dashboardSections = {
  quests: "Check where to start a quest, what it requires, and what it rewards.",
  npcs: "Find characters, their coordinates, the quests they offer, and their models.",
  locations: "Explore game maps, cities, building interiors, and local lore.",
  items: "Look up loot, equipment, vendor items, and crafted products.",
  schematics: "Check recipe ingredients, crafting skills, and station requirements.",
  resources: "Compare material types, quality ranges, harvesting worlds, and recipes.",
  ships: "Inspect player chassis, components, pilot certifications, and deeds.",
  conversations: "Read the game's English dialogue alongside its original string codes.",
};

export function homePage(metadata, query, matches) {
  const atlas = worldMaps(metadata).find(world => world.chart?.image);
  const feature = atlas ? `<a class="archive-feature" href="${atlas.url}" aria-label="Open the game map for ${h(atlas.name)}">
      <img src="${h(atlas.chart.image)}" alt="">
      <span class="archive-feature-caption"><span><strong>${h(atlas.name)}</strong><small>Explore the game map</small></span>
        <span class="archive-arrow" aria-hidden="true">→</span></span>
    </a>` : "";
  const collections = Object.entries(dashboardSections).map(([kind, detail]) =>
    `<li><a class="archive-collection" href="${href(kind)}">
      <span class="archive-collection-top"><strong>${sections[kind].title}</strong>
        <span class="archive-count">${integer(metadata.counts[kind] ?? 0)}</span></span>
      <span class="archive-collection-detail">${h(detail)}</span>
    </a></li>`).join("");
  const paths = metadata.source_key ? [
    metadata.counts.quests && [href("quests"), "Find a quest"],
    metadata.counts.items && [href("items") + "?acquisition=crafted", "Browse craftable items"],
    metadata.counts.schematics && [href("schematics"), "Find crafting recipes"],
    metadata.map_data?.buildings?.buildings && ["/locations?category=Building", "Explore building interiors"],
    metadata.counts.ships && [href("ships"), "Explore player ships"],
  ].filter(Boolean) : [
    [href("quests", "quest/purvis_kill_zealots") + "?view=chain", "Follow a quest chain"],
    [href("items") + "?acquisition=crafted", "Browse craftable items"],
    [href("resources", "steel") + "?view=schematics", "Find recipes that use steel"],
    metadata.map_data?.buildings?.buildings ? ["/locations?category=Building", "Explore building interiors"] :
      [href("ships", "player_xwing") + "?view=components", "Explore an X-wing loadout"],
  ];
  const startingPoints = paths.map(([url, label]) => `<li><a href="${url}">${label}
    <span aria-hidden="true">→</span></a></li>`).join("");

  return `<div class="archive-home">
    <section class="archive-hero${feature ? "" : " archive-hero-text"}">
      <div class="archive-intro">
        <h1>Galactic Archive</h1>
        <p>A reference for Star Wars Galaxies, built from the game files. Find where a quest begins, explore a world map, or look up what you need to craft an item.</p>
        <form id="home-search" class="hero-search" action="/" role="search">
          <label class="search-field"><span class="glass" aria-hidden="true"></span><span class="sr-only">Search the archive</span>
            <input type="search" name="q" maxlength="160" value="${h(query)}" placeholder="Name, game ID, or string code" autocomplete="off"></label>
          <button type="submit" class="button primary">Search</button>
        </form>
      </div>
      ${feature}
    </section>
    ${matches ? `<section class="archive-results panel" aria-labelledby="archive-results-title">
      <header><h2 id="archive-results-title" tabindex="-1">Results for “${h(query)}”</h2><a href="/">Clear search</a></header>
      <div class="archive-result-groups">${searchResultGroups(matches, query)}</div>
    </section>` : ""}
    <section class="archive-directory" aria-labelledby="archive-sections-title">
      <h2 id="archive-sections-title">Browse the archive</h2>
      <ul class="archive-collections">${collections}</ul>
    </section>
    ${startingPoints ? `<section class="archive-quick panel" aria-labelledby="archive-quick-title">
      <h2 id="archive-quick-title">Start with a task</h2>
      <ul>${startingPoints}</ul>
    </section>` : ""}
  </div>`;
}

function columns(kind) {
  return ({
    quests: [["Quest", "name"], ["Quest giver", "giver"], ["Origin", "world"], ["Level", "level"], ["Rewards"]],
    npcs: [["NPC", "name"], ["Location", "world"], ["Level", "level"], ["Class", "difficulty"]],
    locations: [["Location", "name"], ["World", "world"], ["Type", "category"], ["Map", "map"]],
    items: [["Item", "name"], ["Type", "category"], ["Required level", "level"], ["Template", "template"]],
    schematics: [["Schematic", "name"], ["Category", "category"], ["Crafting type", "station"],
      ["Ingredient slots", "slots"], ["Authored outputs", "products"]],
    resources: [["Resource", "name"], ["Category", "category"], ["Parent class", "parent"], ["Worlds", "worlds"]],
    ships: [["Ship", "name"], ["Role", "category"], ["Tier", "level"], ["Component slots", "slots"], ["Template", "template"]],
    conversations: [["Conversation", "name"], ["Lines", "lines"], ["Different", "different"], ["Missing", "missing"]],
  })[kind];
}

function miniReward(record) {
  const coins = record.reward_credits?.length ? `<b>${record.reward_credits.map(integer).join(" / ")} cr</b>` : "";
  const xp = record.reward_experience?.length ? `<em>${record.reward_experience.map(integer).join(" / ")} XP</em>` : "";
  const items = record.reward_items ? `<span>${integer(record.reward_items)} item${record.reward_items === 1 ? "" : "s"}</span>` : "";
  return `<span class="reward-mini">${coins || xp || items ? coins + xp + items : "—"}</span>`;
}

function rowCells(kind, record, metadata) {
  const tier = record.level == null ? "—" : integer(record.level);
  const scene = locationLink(record.scene, metadata);
  if (kind === "quests") return [h(record.givers?.join(", ") || "—"), scene,
    record.min_level != null ? `${tier}<small class="entity-sub">Req. ${integer(record.min_level)}</small>` : tier, miniReward(record)];
  if (kind === "npcs") return [scene, tier, h(record.difficulty || record.category || "—")];
  if (kind === "items") {
    const details = [record.storage?.datapad && "Datapad", record.storage?.inventory && "Inventory",
      record.crafted && "Craftable"].filter(Boolean).join(" · ");
    return [h(record.category) + (details ? `<small class="entity-sub">${h(details)}</small>` : ""), tier, code(record.template)];
  }
  if (kind === "schematics") return [h(record.category), h(readable(record.station) || "—"),
    integer(record.slots), integer(record.products)];
  if (kind === "resources") return [h(record.category), h(record.parent || "—"),
    record.worlds ? integer(record.worlds) : "—"];
  if (kind === "ships") return [h(record.category), tier, integer(record.slots), code(record.template)];
  if (kind === "locations") return [scene, h(record.category), record.has_map ? tag("Map", "good") : "—"];
  return [integer(record.line_count), record.different ? tag(integer(record.different), "warn") : "—",
    record.missing ? tag(integer(record.missing), "bad") : "—"];
}

const featuredWorlds = [
  "tatooine", "corellia", "dantooine", "dathomir", "endor", "hoth", "kashyyyk", "lok",
  "mustafar", "naboo", "ord_mantell", "rori", "talus", "yavin4",
  "kashyyyk_hunting", "kashyyyk_dead_forest", "kashyyyk_rryatt_trail",
];

function worldMaps(metadata) {
  const otherWorlds = metadata.ground_worlds || [];
  const ids = new Set([...featuredWorlds, ...otherWorlds]);
  const worlds = [];
  for (const id of ids) {
    const location = metadata.locations[id];
    if (!location?.has_map || location.alias_for) continue;
    const scene = metadata.maps[location.map_id || (id === "kashyyyk" ? "kashyyyk_main" : id)];
    const chart = scene?.image ? scene : (scene?.regions || []).map(key => metadata.maps[key]).find(map => map?.image);
    const query = chart && chart !== scene ? "?" + new URLSearchParams({ chart: chart.id }) : "";
    worlds.push({ name: location.name, chart, url: href("locations", id) + h(query) });
  }
  return worlds;
}

function worldMapCards(metadata) {
  const cards = worldMaps(metadata).map(({ name, chart, url }) => `
    <li><a class="world-card" href="${url}">
      ${chart?.image ? `<img src="${h(chart.image)}?thumbnail=1" alt="" loading="lazy" decoding="async">` : ""}
      <span class="world-card-label"><strong>${h(name)}</strong>
        <small>${chart ? "Game map" : "Coordinate map"}</small></span>
      <span class="world-card-arrow" aria-hidden="true">→</span>
    </a></li>`).join("");
  return cards ? `<section class="world-directory" aria-labelledby="world-directory-title">
    <header><h2 id="world-directory-title">Major worlds and zones</h2><a href="#browse-results-title">Browse all locations ↓</a></header>
    <ul>${cards}</ul>
  </section>` : "";
}

export function browsePage(kind, filters, result, metadata) {
  const which = sections[kind];
  const map = ["quests", "npcs"].includes(kind) ? worldMaps(metadata).find(world => world.chart?.image) : null;
  const isLocations = kind === "locations";
  const worldCards = isLocations && result.page === 1 && ![filters.q, filters.scene, filters.category, filters.status].some(Boolean) ?
    worldMapCards(metadata) : "";
  const heroArt = map ? `<a class="hero-art" href="${map.url}">
    <img alt="" src="${h(map.chart.image)}"><span class="hero-art-label"><strong>${h(map.name)} ↗</strong><small>Game map</small></span></a>` : "";
  const worlds = Object.entries(metadata.locations).filter(([, world]) => !isLocations || !world.alias_for)
    .sort(([, a], [, b]) => a.name.localeCompare(b.name));
  const worldNames = new Map();
  for (const [, world] of worlds) worldNames.set(world.name, (worldNames.get(world.name) || 0) + 1);
  const levelFilter = ["quests", "npcs", "items", "ships"].includes(kind);
  const worldFilter = ["quests", "npcs", "locations", "resources"].includes(kind);
  const visibleStatuses = metadata.statuses[kind] || [];
  const filtersHtml = `<form id="browse-filters" class="panel filters">
      <div class="filter-heading"><h2>Filters</h2><a href="${href(kind)}">Clear</a></div>
      ${worldFilter ? `<label class="filter-label">${kind === "quests" ? "Origin world" : "World"}
        <select name="scene">${opt("", "All worlds", filters.scene)}${worlds.map(([id, value]) =>
          opt(id, worldNames.get(value.name) > 1 ? value.name + " (" + id + ")" : value.name,
            isLocations ? locationId(filters.scene, metadata) : filters.scene)).join("")}</select></label>` : ""}
      <label class="filter-label">${kind === "quests" ? "Quest family" : "Category"}<select name="category">
        ${opt("", "All categories", filters.category)}${metadata.categories[kind].map(category => opt(category, category, filters.category)).join("")}</select></label>
      ${levelFilter ? `<fieldset class="level-filter"><legend>${kind === "quests" ? "Recommended level" : kind === "ships" ? "Chassis tier" : "Level"}</legend>
        <div class="range"><input name="min" type="number" aria-label="Minimum ${kind === "ships" ? "tier" : "level"}" placeholder="Min" min="0" max="999" value="${h(filters.min)}">
        <input name="max" type="number" aria-label="Maximum ${kind === "ships" ? "tier" : "level"}" placeholder="Max" min="0" max="999" value="${h(filters.max)}"></div></fieldset>` : ""}
      ${kind === "items" ? `<label class="filter-label">Acquisition<select name="acquisition">
        ${[["", "Any source"], ["crafted", "Craftable"], ["quest", "Quest reward"], ["vendor", "Vendor table"],
           ["loot", "Loot table"], ["script", "Scripted grant or spawn"]]
          .map(([id, text]) => opt(id, text, filters.acquisition)).join("")}</select></label>
        <label class="filter-label">Storage or form<select name="storage">
          ${[["", "Any storage or form"], ["datapad", "Datapad (scripted)"], ["inventory", "Inventory (scripted)"],
             ["physical", "Physical template"], ["intangible", "Intangible template"]]
            .map(([id, text]) => opt(id, text, filters.storage)).join("")}</select></label>` : ""}
      ${visibleStatuses.length > 1 ? `<label class="filter-label">Source status<select name="status">${opt("", "All statuses", filters.status)}
        ${visibleStatuses.map(id => opt(id, statuses[id] || readable(id), filters.status)).join("")}</select></label>` : ""}
      <button class="button" type="submit">Apply filters</button>
    </form>`;
  const headers = columns(kind);
  const locationNames = new Map();
  if (isLocations) {
    for (const { name } of result.rows) {
      const key = name.toLowerCase();
      locationNames.set(key, (locationNames.get(key) || 0) + 1);
    }
  }
  const rows = result.rows.map(record => {
    const upper = kind === "quests" ? record.chapter || record.category : "";
    const status = record.status && !["authored", "authored_recipe", "server-authored"].includes(record.status) ?
      statuses[record.status] || readable(record.status) : "";
    const disambiguate = isLocations && locationNames.get(record.name.toLowerCase()) > 1;
    const sub = [...new Set([upper, status, disambiguate || ["conversations", "ships"].includes(kind) ? record.id : ""]
      .filter(Boolean))].join(" · ");
    const art = kind === "resources" ? resourceIcon(record.icon, "resource-icon") : "";
    return `<tr><td><div class="entry-label">${art}<div>${link(kind, record.id, record.name, "entity-name")}${sub ? `<small class="entity-sub">${h(sub)}</small>` : ""}</div></div></td>
      ${rowCells(kind, record, metadata).map(value => `<td>${value}</td>`).join("")}</tr>`;
  }).join("");
  const table = rows ? `<div class="table-wrap" tabindex="0" role="region" aria-label="${which.title} results">
    <table class="data-table ${kind === "quests" ? "question-table" : ""}">
    <thead><tr>${headers.map(([label, key]) => columnHeading(label, key, filters.sort || "name")).join("")}</tr></thead>
    <tbody>${rows}</tbody></table></div>` : '<p class="empty">No entries match these filters.</p>';
  return `<section class="hero${heroArt ? "" : " hero-simple"}${isLocations ? " locations-hero" : ""}"><div class="hero-content">
    <h1>${which.title}</h1><p>${which.description}</p>
    <form id="browse-search" class="hero-search">
      <label class="search-field"><span class="glass" aria-hidden="true"></span><span class="sr-only">Search ${which.title.toLowerCase()}</span>
      <input type="search" name="q" value="${h(filters.q)}" placeholder="Search ${which.title.toLowerCase()} by name or ID" autocomplete="off"></label>
      <button class="button primary" type="submit">Search</button>
    </form></div>${heroArt}</section>${worldCards}
    <div class="browse-layout">${filtersHtml}<section class="panel">
    <div class="results-heading"><div class="results-title"><h2 id="browse-results-title" tabindex="-1">Browse ${which.title.toLowerCase()}</h2>
      <p>${integer(result.total)} records${result.page > 1 ? " · Page " + integer(result.page) : ""}</p></div>
    </div>${table}${pagination(result, "browse-page")}</section></div>`;
}

function tabsFor(kind, entity, spawnCount) {
  const { related, data } = entity;
  const count = key => related[key]?.count || null;
  if (kind === "quests") return [["overview", "Overview"], ["objectives", "Objectives", data.detail.steps.length],
    ["dialogue", "Dialogue", count("dialogue")], ["chain", "Quest chain"]];
  if (kind === "npcs") return [["overview", "Overview"], ["quests", "Quests", count("series_starts")],
    ["locations", "Locations", Math.max(data.locations.length, entity.spawn_source_count || 0)],
    ...(entity.has_loot ? [["loot", "Loot"]] : []),
    ["dialogue", "Dialogue", count("dialogue")], ["model", "Model"]];
  if (kind === "locations" && data.category === "Building") return [
    ["overview", "Cells and contents", data.cells.length], ["spawns", "Spawns", spawnCount],
    ["locations", "Locations", entity.placement_count],
    ["quests", "Quests", count("quests")], ["map", "World map"], ["model", "Model"],
  ];
  if (kind === "locations") return [["overview", "Overview"], ["npcs", "NPCs", count("npcs")],
    ["quests", "Quests", count("quests")], ["places", "Locations", count("places")],
    ...(count("buildings") ? [["buildings", "Buildings", count("buildings")]] : []),
    ...(count("resources") ? [["resources", "Resources", count("resources")]] : []),
    ...(count("station_resources") ? [["station_resources", "Mining prices", count("station_resources")]] : [])];
  if (kind === "items") return [["overview", "Overview"], ["locations", "Locations", entity.placement_count], ["crafting", "Crafting", count("crafted_by")],
    ["acquisition", "Acquisition"], ["quests", "Quests"],
    ...(count("equipment_npcs") ? [["equipment", "Equipment", count("equipment_npcs")]] : []),
    ["model", "Model"]];
  if (kind === "schematics") return [["overview", "Overview"], ["recipe", "Ingredients", data.slots.length],
    ["experimentation", "Experimentation", data.experimentation.length], ["training", "Training and sources"]];
  if (kind === "ships") return [["overview", "Overview"], ["components", "Components", data.slots.length],
    ["acquisition", "Deeds and recipes"], ["model", "Model"]];
  if (kind === "resources") return [["overview", "Overview"], ["distribution", "Worlds", entity.card.worlds],
    ["schematics", "Recipes", count("schematics")], ["children", "Subclasses", count("children")]];
  return [["overview", "Lines", entity.card.line_count], ["references", "References"]];
}

function detailHeader(kind, entity, active, metadata, tabs) {
  const { card, data } = entity;
  const title = sections[kind].single;
  const category = card.category ? tag(card.category) : "";
  const actualLevel = card.level != null ? tag(({ quests: "Recommended ", ships: "Tier " }[kind] || "Level ") + integer(card.level), "good") : "";
  const origin = card.scene ? tag(locationName(card.scene, metadata)) : "";
  const historical = card.status && !["authored", "authored_recipe", "server-authored"].includes(card.status) ?
    tag(statuses[card.status] || readable(card.status), "warn") : "";
  const minimum = card.min_level != null ? tag("Requires level " + integer(card.min_level)) : "";
  const ancestors = kind === "locations" ? entity.ancestors || [] : [];
  const repeated = ancestors.some(parent => parent.name.toLowerCase() === card.name.toLowerCase());
  const label = repeated ? `${card.name} (${card.category})` : card.name;
  const trail = [link("home", null, "Archive"), link(kind, null, sections[kind].title),
    ...ancestors.map(parent => link("locations", parent.id, parent.name)), `<span aria-current="page">${h(label)}</span>`];
  return `<nav class="breadcrumbs" aria-label="Breadcrumb">${trail.join('<span aria-hidden="true">/</span>')}</nav>
    <header class="detail-title${kind === "locations" ? " location-title" : ""}">${data.journal?.category?.text ? `<p class="detail-kicker">${h(data.journal.category.text)}</p>` : ""}<h1>${h(card.name)}</h1>
    <p class="entry-id">${card.id.endsWith(".iff") ? code(card.id) : h(card.id)}</p><div class="detail-tags">${category + actualLevel + minimum + origin + historical}</div></header>
    <nav class="detail-tabs" aria-label="${title} views">
    ${tabs.map(([id, name, count]) => `<a href="${href(kind, card.id)}${id === "overview" ? "" : "?view=" + id}"
    ${active === id ? ' aria-current="page"' : ""}>${name}${count ? ` <span class="count-label">${integer(count)}</span>` : ""}</a>`).join("")}</nav>`;
}

function requirements(rules) {
  if (!rules.length) return '<p class="muted">No explicit requirement was recovered.</p>';
  return `<ul class="quiet-list">${rules.map(rule => {
    let description = h(rule.description || readable(rule.kind));
    if (rule.kind === "script_route_guard") description = `Route condition ${code(rule.expression)}`;
    if (rule.id) description += " " + link("quests", rule.id, rule.id);
    if (rule.item_id) description += " " + link("items", rule.item_id, rule.item_id);
    if (rule.kind === "minimum_level") description += " " + integer(rule.amount);
    if (rule.faction) description += " · " + h(rule.faction);
    if (rule.threshold != null) description += " · " + integer(rule.threshold);
    if (rule.key) description += " " + code(rule.key);
    return `<li>${description}${sourceText(rule.source)}</li>`;
  }).join("")}</ul>`;
}

function rewards(entries) {
  if (!entries.length) return '<p class="muted">No explicit reward was recovered.</p>';
  return entries.map(reward => {
    const amount = reward.amount == null ? "Variable" : integer(reward.amount);
    let content;
    if (reward.kind === "credits") content = h(amount) + " credits";
    else if (reward.kind === "experience") content = `${integer(reward.base_amount ?? reward.amount)} XP${reward.base_amount != null ? " base" : ""} · ${h(reward.xp_type)}`;
    else if (reward.kind === "item" && reward.item_id) content = link("items", reward.item_id, reward.item?.text || reward.item_id)
      + (reward.count > 1 ? " × " + integer(reward.count) : "");
    else if (reward.kind === "faction") content = `${h(amount)} standing · ${h(reward.faction)}`;
    else content = h(reward.description || readable(reward.kind)) + (reward.item_id ? " " + code(reward.item_id) : "");
    const detail = [readable(reward.timing), reward.selection && readable(reward.selection),
      reward.unit, reward.task != null ? "Task " + reward.task : "", reward.base_amount != null ? "Character caps and modifiers may apply" : ""]
      .filter(Boolean).join(" · ");
    return `<div class="rich-row"><div>${content}</div><p>${h(detail)}</p>${sourceText(reward.source)}</div>`;
  }).join("");
}

function questStarts(entity, metadata) {
  if (!entity.data.starts.length) return '<p class="muted">No quest giver or start was recovered.</p>';
  const sites = entity.related.origin_sites?.rows || [];
  return entity.data.starts.map((start, index) => {
    const display = start.npc?.text || start.npc_id;
    const who = start.npc_id ? link("npcs", start.npc_id, display) :
      start.template && start.kind === "item" ? link("items", start.template, start.template) : h(readable(start.kind));
    const candidates = sites.filter(site => site.detail.start_index === index);
    const site = candidates.find(entry => entry.card?.category === "Building") || candidates[0];
    const place = site ? link("locations", site.id, site.card?.name || site.id) :
      start.scene ? locationLink(start.scene, metadata) :
      start.planet_hint ? `<span class="muted">Hint: ${h(locationName(start.planet_hint, metadata))}</span>` :
        '<span class="hint">Origin unresolved</span>';
    const point = start.placement?.coordinates || site?.detail.coordinates;
    return `<div class="rich-row"><div>${who} · ${place}</div>${point ? worldCoordinates(point) : ""}
      ${start.origin_evidence ? '<p>Origin traced through the authored NPC placement.</p>' + sourceText(start.origin_evidence) : ""}
      ${sourceText(start.grant || start.source)}</div>`;
  }).join("");
}

function issues(quest) {
  if (!quest.issues?.length) return "";
  return section("Source notes", quest.issues.map(issue => `<div class="rich-row">
    <strong>${h(readable(issue.kind))}</strong>
    ${issue.part ? `<p>${h(readable(issue.part))}</p>` : ""}${sourceText(issue.source)}</div>`).join(""));
}

function questOverview(entity, metadata) {
  const data = entity.data;
  const details = data.detail;
  return `<div class="article-grid"><div class="stack">
    ${section("Journal entry", localized(details.description, "The journal description is unresolved."))
      + (details.completion_summary?.text ? section("Completion summary", localized(details.completion_summary)) : "")
      + section("Requirements", requirements(data.requirements))
      + section("Rewards", rewards(data.rewards))
      + issues(data)}
    </div><aside class="stack">${section("Starts at", questStarts(entity, metadata))
      + section("Quest facts", facts([["Family", h(entity.card.category)], ["Recommended level", integer(entity.card.level)],
        ["Minimum level", integer(data.minimum_level)], ["Type", h(data.journal?.type)], ["Tier", integer(data.journal?.tier)],
        ["Repeatable", data.journal ? (data.journal.repeatable ? "Yes" : "No") : null]]))
      + sectionSources(data.sources)}</aside></div>`;
}

function questObjectives(entity, metadata) {
  const data = entity.data;
  const mission = data.detail.authored_mission;
  const objectives = data.detail.steps.map((step, index) => `<details${index === 0 ? " open" : ""}>
      <summary>${step.index + 1}. ${h(step.title.text || readable(step.id))}${!step.visible ? " · Hidden task" : ""}</summary>
      ${localized(step.title && !step.title.text ? step.title : { code: step.title?.code })}
      ${localized(step.description, "The objective text is unresolved.")}
      ${facts([["Task ID", code(step.id)], ["Scene", step.scene && locationLink(step.scene, metadata)],
        ["Handler", step.script && code(step.script)], ["After tasks", h(step.after)],
        ["Excluded tasks", h(step.exclusions)], ["On completion", h(step.on_complete)], ["On failure", h(step.on_failure)]])}
      ${Object.keys(step.attributes).length ? `<details><summary>Authored task fields</summary>${plainObject(step.attributes)}</details>` : ""}
      ${sourceText(step.source)}</details>`).join("");
  return `<div class="stack">${section("Objectives", objectives || '<p class="muted">No task rows were recovered.</p>')}
    ${mission?.fields ? section("Authored mission data", plainObject(mission.fields) + sourceText(mission.source)) : ""}
    ${data.objective_scenes.length ? section("Objective locations", '<ul class="quiet-list">' +
       data.objective_scenes.map(point => `<li>${locationLink(point.scene, metadata)}${sourceText(point.source)}</li>`).join("") + "</ul>") : ""}</div>`;
}

function edgeList(links, entity, group) {
  if (!links.length) return '<p class="muted">No linked quest was recovered.</p>';
  const names = Object.fromEntries((entity.related[group]?.rows || []).filter(entry => entry.card)
    .map(entry => [entry.id, entry.card.name]));
  return links.map(edge => {
    const target = edge.target_defined ? link("quests", edge.id, names[edge.id] || edge.id) : code(edge.id);
    const flags = [readable(edge.kind), edge.trigger_condition && readable(edge.trigger_condition),
      edge.dispatch === "handler_skips_base" ? "Handler skips configured grant" : null,
      !edge.target_defined ? "Definition unresolved" : null, edge.task != null ? "Task " + edge.task : null].filter(Boolean);
    return `<div class="rich-row"><div>${target}</div><p>${h(flags.join(" · "))}</p>${sourceText(edge.source)}</div>`;
  }).join("");
}

function questChain(entity) {
  return `<div class="stack">${section("Preceding quests", edgeList(entity.data.predecessors, entity, "predecessors"))}
    ${section("Follow-up quests", edgeList(entity.data.follow_ups, entity, "follow_ups"))}</div>`;
}

function npcGear(entity) {
  const { data, related } = entity;
  const named = Object.fromEntries((related.weapons?.rows || []).filter(entry => entry.card)
    .map(entry => [entry.id, entry.card.name]));
  if (!data.equipment.length) return '<p class="muted">No authored weapon assignment was recovered.</p>';
  return data.equipment.map(arm => {
    const descriptive = {pool: "Possible equipment", fixed: "Authored weapon", unarmed: "Unarmed",
      unresolved_pool: "Weapon group unresolved"}[arm.status];
    return `<div class="rich-row"><strong>${readable(arm.slot)}</strong><p>${h(descriptive)} · ${code(arm.key)}</p>
      ${arm.items.length ? `<ul class="quiet-list">${arm.items.map(id => `<li>${link("items", id, named[id] || id)}</li>`).join("")}</ul>` : ""}
      ${arm.attack_speed != null ? `<p>Configured attack speed ${integer(arm.attack_speed)}</p>` : ""}
      ${arm.specials ? `<p>Specials ${code(arm.specials)}</p>` : ""}${sourceText(arm.pool_source || arm.source)}</div>`;
  }).join("");
}

function npcStats(data) {
  return section("Authored combat profile", plainObject(data.stats)) +
    (data.balance?.values ? section("Base stat curve", '<p class="muted">Before spawn and equipment modifiers.</p>'
      + plainObject(data.balance.values) + sourceText(data.balance.source)) : "");
}

function mapShell() {
  return `<div id="detail-map" aria-live="polite" aria-busy="true">${loadingIndicator("Loading map…")}</div>`;
}

function npcSpawnerDefinitions(entity, { compact = false } = {}) {
  const definitions = (entity.related.spawners?.rows || []).filter(row => row.detail?.entries?.length);
  return definitions.map(({ id, card, detail }) => {
    const title = link("items", id, card?.name || readable(id.split("/").pop()), "entity-name");
    const config = { ...detail, entries: detail.entries.map(entry =>
      entry.npc === entity.card.id ? { ...entry, name: entity.card.name } : entry) };
    const body = spawnDetails(config, { showType: false });
    return compact ? `<div class="rich-row">${title}<details class="cell-spawn-details">
      <summary>${h(spawnSummary(config))}</summary>${body}</details></div>` :
      `<article class="npc-spawn-card"><h3>${title}</h3>${body}</article>`;
  }).join("");
}

function npcOverview(entity, metadata) {
  const { data, card } = entity;
  const location = data.locations.find(point => point.coordinates) || data.locations[0];
  const left = (data.notes ? section("Profile", `<p class="long-text">${h(data.notes)}</p>`) : "") +
    npcStats(data) + section("Equipped weapons", npcGear(entity));
  const definitions = npcSpawnerDefinitions(entity, { compact: true });
  const placed = entity.placement_count || 0;
  const recorded = entity.spawn_source_count || 0;
  const locationsLink = `<p class="location-world-link"><a href="${href("npcs", card.id)}?view=locations">
    ${placed ? "View spawn locations" : "View spawner sources"}</a></p>`;
  let locationBody = "";
  if (location) locationBody = locationLink(location.scene, metadata) + "<br>" + worldCoordinates(location.coordinates);
  if (placed) locationBody += `<p>${integer(placed)} spawn location${placed === 1 ? "" : "s"}</p>`;
  if (definitions) locationBody += definitions + (placed ? "" :
    '<p class="muted">The game defines this spawner. Its placement was not found in the indexed buildouts.</p>');
  if (recorded || definitions) locationBody += locationsLink;
  if (!locationBody) {
    locationBody = `<p class="muted">World placement unresolved.${data.where_hint ? " Source hint: " + h(data.where_hint) : ""}</p>`;
  }
  return `<div class="article-grid"><div class="stack">${left}</div><aside class="stack">
    ${section("NPC facts", facts([["Base level", integer(card.level)], ["Difficulty", h(data.difficulty)], ["Type", h(card.category)],
      ["Appearance", code(data.template)]]))}
    ${section("Location", locationBody + (location?.coordinates ? mapShell() : ""))}
    ${sectionSources(data.sources)}</aside></div>`;
}

function npcLocations(entity, metadata) {
  const { data } = entity;
  const rows = data.locations.map(point => {
    const cell = point.cell;
    const cellLabel = typeof cell === "string" ? cell : cell?.name || cell?.object_id || cell?.index;
    return `<tr><td>${locationLink(point.scene, metadata)}</td><td>${worldCoordinates(point.coordinates)}
      ${point.local_coordinates && cellLabel ? `<small class="entity-sub">Cell ${h(cellLabel)} · local
        ${copyMarkup(point.local_coordinates.join(", "), coordinateValue(point.local_coordinates), "Copy local coordinates", "coordinate")}</small>` : ""}</td>
      <td>${h(readable(point.status))}${sourceText(point.source)}${cellLabel ? sourceText(cell?.source) : ""}</td></tr>`;
  }).join("");
  const table = rows ? `<div class="table-wrap"><table class="data-table coordinate-table"><thead><tr>
  <th scope="col">Scene</th><th scope="col">World position</th><th scope="col">Evidence</th></tr></thead><tbody>${rows}</tbody></table></div>` :
    '<p class="muted">No authored placement was recovered for this NPC.</p>';
  const scenes = [...new Set(data.locations.filter(point => point.coordinates).map(point => point.scene))];
  const definitions = npcSpawnerDefinitions(entity);
  const placed = entity.placement_count || 0;
  const recorded = entity.spawn_source_count || 0;
  return `<div class="stack">${scenes.length ? section("Game map", (scenes.length > 1 ?
    `<label class="filter-label">Map scene <select id="map-scene">${scenes.map(scene => opt(scene, locationName(scene, metadata))).join("")}</select></label>` : "")
    + mapShell()) : ""}
    ${recorded ? placementSection("npcs", entity) : ""}
    ${definitions ? section("Spawner definitions", (placed ? "" :
      '<p class="spawn-placement-note muted">No placed instance is recorded in the indexed buildouts.</p>') +
      `<div class="npc-spawn-cards">${definitions}</div>`, "npc-spawn-panel") : ""}
    ${rows || !(recorded || definitions) ? section("World coordinates", table) : ""}
    ${entity.related.placed_in?.count ? section("Areas containing placements", relatedRows(
      "npcs", entity.card.id, entity, "placed_in", "")) : ""}</div>`;
}

function npcLoot(entity) {
  const space = entity.card.category === "Space mobile";
  return section("Possible drops", `<p class="muted">Drop chance is the chance of receiving at least one copy
    from an eligible defeat at the authored base level. Open a row for its chance per table roll.
    ${space ? "Space chances use the standard loot settings and base collection rate." : "Encounter scripts can modify loot eligibility."}</p>
    <form id="npc-drop-filter" class="placement-filters" role="search" aria-label="Filter loot items">
      <label class="filter-label">Item name or ID<input class="control-input" name="q" type="search" maxlength="160"></label>
      <button class="button" type="submit">Filter</button>
    </form>
    <div id="npc-drop-results" class="placement-results" aria-live="polite" aria-busy="true" tabindex="-1">
    ${loadingIndicator("Loading loot…")}</div>`);
}

function npcQuests(entity) {
  return `<div class="stack">${section("Series starters", relatedRows("npcs", entity.card.id, entity, "series_starts",
    "No indexed series starts at this NPC."))}
    ${entity.related.continuation_quests?.count ? section("Other offers", '<p class="muted">These quests have an earlier recorded step.</p>' +
      relatedRows("npcs", entity.card.id, entity, "continuation_quests", "")) : ""}</div>`;
}

function locationOverview(entity, metadata) {
  const { card, data, related } = entity;
  const interior = related.interior?.rows[0]?.id;
  const coordinates = data.marker ? worldCoordinates(data.marker.coordinates) : "";
  const limits = data.bounds?.length === 4 ? worldBounds(data.bounds) : "";
  const citations = (data.description?.sources || []).map(citation => {
    if (citation.url?.startsWith("https://")) return `<p><a href="${h(citation.url)}" target="_blank" rel="noreferrer">
      ${h(citation.label)}</a></p>`;
    return sourceText(citation) + (citation.key ? code(citation.key) : "");
  }).join("");
  const narrative = h(data.description?.text).replace(/\bX (-?[\d,]+), Z (-?[\d,]+)/g, (text, x, z) =>
    copyMarkup(text, coordinateValue([x, z].map(value => Number(value.replaceAll(",", ""))), ["X", "Z"]), "Copy map coordinates", "coordinate"));
  const about = narrative ? section("About " + data.name,
    `<p class="long-text">${narrative}</p>
    ${data.lore_parent ? `<p class="location-world-link">${link("locations", data.lore_parent,
      "About " + locationName(data.lore_parent, metadata))}</p>` : ""}
    ${citations ? `<details class="location-about-sources"><summary>Sources</summary>${citations}</details>` : ""}`) : "";
  const children = related.places;
  const preview = children?.rows.slice(0, 6) || [];
  const localPlaces = preview.length ? section("Locations within " + data.name, linkRows(preview) +
    (children.count > preview.length ? `<p class="location-world-link">
      <a href="${href("locations", card.id)}?view=places">Browse all ${integer(children.count)} locations</a></p>` : "")) : "";
  return `<div class="stack location-overview">
    ${section("Game map", mapShell(), "location-map-panel")}
    <div class="article-grid">
      <div class="stack">${about}${localPlaces}${data.attributes ? section("Authored attributes", plainObject(data.attributes)) : ""}</div>
      <aside class="stack">${section("Location facts", facts([
        ["Scene", locationLink(data.scene, metadata)], ["Type", h(data.category)], ["World position", coordinates],
        ["Building", interior && link("locations", interior, "View building interior")],
        ["World bounds", limits], ["Object ID", h(data.object_id)], ["Template", data.template && code(data.template)],
      ])) + sectionSources(data.sources)}</aside></div></div>`;
}

const placementOrigins = {
  server_buildout: "Server buildout", server_buildout_client_parent: "Server buildout",
  client_buildout: "Client buildout", client_snapshot: "Client snapshot", building_spawn: "Script spawn",
};

function buildingPlacementSource(object) {
  const placement = placementOrigins[object.origin] || readable(object.origin);
  const origin = object.spawn_origin;
  if (!origin) {
    return (object.spawner ? `<strong>Dedicated spawner object</strong><small>${h(placement)}</small>` :
      h(placement)) + sourceText(object.source);
  }

  const records = new Map();
  function add(label, proof) {
    if (proof?.path) {
      const key = JSON.stringify([proof.path, proof.line]);
      if (!records.has(key)) records.set(key, [label, proof]);
    }
  }
  add("Datatable row", origin.table_source);
  add("Spawn call", origin.caller_source);
  add("Spawn script", origin.script_source);
  for (const proof of origin.attachments) {
    add(proof.kind === "buildout" ? "Buildout attachment" : origin.template ? "Spawner template" : "Building template", proof);
  }
  add("Placement source", object.source);

  return `<strong>${h(origin.type)}</strong><small>${h(origin.script)}</small>${records.size ?
    `<details class="spawn-sources"><summary>Source records</summary>
      <ul class="spawn-source-list">${[...records.values()].map(([label, proof]) =>
        `<li><span class="spawn-source-label">${h(label)}</span>${sourceText(proof)}</li>`).join("")}</ul></details>` : ""}`;
}

function buildingContents(contents, roomIndex = null) {
  if (!contents.length) return '<p class="muted">No objects are recorded in this cell.</p>';
  return `<div class="table-wrap"><table class="data-table building-contents">
    <thead><tr><th>Object</th><th>World position</th><th>Local position</th><th>Spawn or placement source</th></tr></thead>
    <tbody>${contents.map((object, index) => {
      const name = object.target ? link(...object.target, object.name) : h(object.name);
      const local = object.local_coordinates;
      const row = roomIndex == null ? "" : ` id="building-cell-${roomIndex}-object-${index}" tabindex="-1"`;
      return `<tr${row}><td>${name}${object.template ? `<small>${code(object.template)}</small>` : ""}
        ${object.spawner ? `<details class="cell-spawn-details"><summary>${h(spawnSummary(object.spawner))}</summary>${spawnDetails(object.spawner)}</details>` : ""}</td>
        <td>${worldCoordinates(object.coordinates)}</td>
        <td>${local ? copyMarkup(local.map((value, index) => "XYZ"[index] + " " + integer(value)).join(" · "),
          coordinateValue(local), "Copy local coordinates", "coordinate") : "—"}
          ${object.coordinate_frame === "parent" ? '<small>Relative to parent object</small>' : ""}</td>
        <td>${buildingPlacementSource(object)}
          ${object.required_event ? `<small>Event ${h(readable(object.required_event))}</small>` : ""}</td></tr>`;
    }).join("")}</tbody></table></div>`;
}

function buildingSpawnRooms(data) {
  const rooms = [...data.cells, ...Object.entries(data.unresolved_cells || {}).map(([name, contents]) => ({ name, contents }))];
  return rooms.map(({ name, index, contents }) => ({
    name, index, contents: contents.filter(object => object.origin === "building_spawn" || object.spawner),
  })).filter(room => room.contents.length);
}

function buildingSpawnScripts(rooms) {
  const scripts = new Map();
  for (const { contents } of rooms) {
    for (const { spawn_origin: origin } of contents) {
      if (origin?.type === "Building master spawner" && !scripts.get(origin.script)) {
        scripts.set(origin.script, origin.script_source);
      }
    }
  }
  return scripts.size ? section("Building spawn scripts", `<ul class="quiet-list">${[...scripts].map(([script, proof]) =>
    `<li>${sourceText(proof) || h(script)}</li>`).join("")}</ul>`) : "";
}

function buildingSpawnsView(rooms) {
  if (!rooms.length) return section("Spawns by cell", '<p class="muted">No spawn records were recovered for this building.</p>');
  return `<div class="stack">${buildingSpawnScripts(rooms)}${section("Spawns by cell", rooms.map(room =>
    `<h3>${h(readable(room.name))}${room.index == null ? " · Unresolved cell" : ""}</h3>${buildingContents(room.contents)}`
  ).join(""))}</div>`;
}

function buildingOverview({ data, related }, metadata, spawnRooms, spawnCount) {
  const total = data.cells.reduce((sum, room) => sum + room.contents.length, 0);
  const travel = related.travel?.rows[0]?.id;
  const unresolved = Object.entries(data.unresolved_cells || {});
  const hasPlan = data.extent || data.cells.some(room => room.floor || room.contents.some(item => item.coordinates));
  const plan = hasPlan ? section("Floor plan", `<div id="building-floor-plan" aria-busy="true">
    <div class="building-plan-tools"><label class="filter-label">Cell<select id="building-plan-cell">
      ${opt("", "All cells")}${data.cells.map(room => opt(String(room.index), readable(room.name))).join("")}
    </select></label><button class="button subtle" type="button" data-cell-contents>Browse cell contents</button></div>
    <div class="building-plan"><canvas role="img" aria-label="Top-down floor plan showing cells and placed objects"></canvas>
      <div class="building-plan-markers" aria-label="Objects on the floor plan"></div>
      <div class="building-plan-tooltip" hidden></div>
      <span class="atlas-map-compass" aria-hidden="true">N ↑</span></div>
    <div class="building-plan-status" aria-live="polite">${loadingIndicator("Loading floor plan…")}</div>
    ${data.extent ? `<div class="building-footprint-key">${data.geometry_kind === "Mapped interior footprint" ?
      "Mapped interior outline" : "Exterior footprint"}</div>` : ""}
  </div>`) : "";
  const details = section("Building facts", facts([
    ["World", locationLink(data.scene, metadata)], ["World position", worldCoordinates(data.coordinates)],
    ["Location", `<a href="${href("locations", data.id)}?view=locations">View building location</a>`],
    ["Travel", travel && link("locations", travel, "View travel information")],
    ["World footprint", data.bounds && worldBounds(data.bounds)], ["Cells", integer(data.cells.length)],
    ["Objects in cells", integer(total)], ["Spawns", spawnCount ?
      `<a href="${href("locations", data.id)}?view=spawns">View ${integer(spawnCount)} spawn records</a>` : null],
    ["Template", code(data.template)],
    ["Portal layout", data.layout_asset && code(data.layout_asset)],
    ["Event", h(readable(data.required_event))],
    ["Footprint source", data.geometry_source ? h(data.geometry_kind) + sourceText(data.geometry_source) : "Exterior outline unavailable"],
  ]) + (data.layout_mismatch ? '<p class="muted">The supplied portal layout and the placement have different CRCs. Recorded cell slots are shown.</p>' : ""));
  const openRoom = spawnRooms.find(room => room.index != null)?.index ??
    data.cells.find(room => room.contents.length)?.index ?? data.cells[0]?.index;
  const cells = data.cells.map(room => `<details class="building-room" id="building-cell-${room.index}"${room.index === openRoom ? " open" : ""}>
      <summary><strong>${h(readable(room.name))}</strong><span>Cell ${integer(room.index)} · ${integer(room.contents.length)} objects</span></summary>
      <div class="building-room-meta">
        <span>Origin ${worldCoordinates(room.origin)}</span>
        ${room.portals != null ? `<span>${integer(room.portals)} portals</span>` : ""}
        ${room.floor_asset ? code(room.floor_asset) : ""}${sourceText(room.source)}
      </div>${buildingContents(room.contents, room.index)}</details>`).join("");
  return `<div class="stack">
    <div class="article-grid"><div class="stack">${plan || section("Floor plan", '<p class="muted">Floor geometry is unavailable for this placement.</p>')}</div>
      <aside class="stack">${buildingSpawnScripts(spawnRooms)}${details}${sectionSources(data.sources)}</aside></div>
    <div id="building-cells">${section("Cells and contents", cells || '<p class="muted">Cell placements could not be resolved.</p>')}</div>
    ${unresolved.length ? section("Spawn rows with unresolved cells", unresolved.map(([name, objects]) =>
      "<h3>" + h(readable(name)) + "</h3>" + buildingContents(objects)).join("")) : ""}
  </div>`;
}

const alignment = {
  aligned: ["Aligned", "good"], different: ["Different text", "warn"],
  missing: ["Missing string", "bad"], uncompared: ["No caption", ""], asset_only: ["Asset only", ""],
};

const conversationContent = '<div id="dialogue-npcs"></div><div id="dialogue-lines" aria-live="polite" tabindex="-1"></div>';

export function dialogueNpcs(table, npcs) {
  if (!npcs?.count) return "";
  return `<section class="dialogue-npcs"><h3>Associated NPCs</h3>
    ${relatedRows("conversations", table, { related: { npcs } }, "npcs", "")}</section>`;
}

function conversationGroup(entity) {
  const choices = entity.related.dialogue?.rows || [];
  if (!choices.length) return section("Dialogue", '<p class="muted">No conversation or dialogue reference was recovered for this entry.</p>');
  return section("Dialogue", `<div class="dialog-toolbar">
    <label class="filter-label">String table<select id="linked-dialogue">${choices.map(choice => opt(choice.id, choice.id)).join("")}</select></label>
    <a class="button" id="open-conversation" href="${href("conversations", choices[0].id)}">Open conversation</a>
    </div><div id="conversation-context"></div>${conversationContent}`);
}

function standaloneConversation(entity) {
  const { data } = entity;
  const dynamic = data.dynamic?.length ? section("Runtime string expressions", data.dynamic.map(item =>
    `<div class="rich-row">${code(item.expression)}${sourceText(item.source)}</div>`).join("")) : "";
  return `<div class="stack">${section("Conversation lines",
    (data.asset ? sourceText({path: data.asset}) : '<p class="muted">The English string table is unresolved.</p>')
    + conversationContent)}${dynamic}</div>`;
}

export function dialogueLines(page, query, filter, sort) {
  const rows = page.rows.map(line => {
    const captions = [...new Set(line.usages.map(use => use.caption).filter(Boolean))];
    const usages = line.usages.map(use => `<div class="rich-row">
      <p>${h([use.speaker, readable(use.source_role), alignment[use.alignment]?.[0]].filter(Boolean).join(" · "))}</p>
      ${use.caption ? `<span class="long-text">${h(use.caption)}</span>` : ""}${sourceText(use.source)}</div>`).join("");
    const evidence = usages ? `<details><summary>${line.usages.length} reference${line.usages.length === 1 ? "" : "s"}</summary>${usages}</details>` : "";
    return `<tr><td>${copyMarkup(line.code, line.code, "Copy string code", "copy-code dialog-code")}${line.speakers.length ? `<small class="entity-sub">${h(line.speakers.map(readable).join(", "))}</small>` : ""}</td>
    <td>${line.text != null ? `<p class="long-text">${h(line.text)}</p>` : '<span class="muted">Unresolved</span>'}</td>
    <td>${captions.map(caption => `<p class="long-text">${h(caption)}</p>`).join("") || "—"}</td>
    <td>${tag(...(alignment[line.status] || [readable(line.status)]))}${evidence}</td></tr>`;
  }).join("");
  return `<form id="line-filters" class="dialog-toolbar"><label class="filter-label">Find a code or line
    <input type="search" class="control-input" name="q" value="${h(query)}"></label>
    <label class="filter-label">Alignment<select name="status">${opt("", "All lines", filter)}
    ${Object.entries(alignment).map(([id, [title]]) => opt(id, title, filter)).join("")}</select></label>
    <button class="button" type="submit">Filter lines</button></form>
    <p class="count-label">${integer(page.total)} line${page.total === 1 ? "" : "s"}</p>
    ${rows ? `<div class="table-wrap" tabindex="0" role="region" aria-label="Conversation lines"><table class="data-table dialog-table">
    <thead><tr>${columnHeading("String code", "code", sort)}${columnHeading("English client line", "text", sort)}
    ${columnHeading("Script caption", "caption", sort)}${columnHeading("Alignment", "alignment", sort)}</tr></thead><tbody>${rows}</tbody></table></div>`
    : '<p class="empty">No lines match these filters.</p>'}
    ${pagination(page, "dialogue-page")}`;
}

export function entityPage(kind, entity, requestedView, metadata) {
  if (kind === "items" && requestedView === "loot") requestedView = "acquisition";
  const { card, data } = entity;
  const spawnRooms = kind === "locations" && card.category === "Building" ? buildingSpawnRooms(data) : [];
  const spawnCount = spawnRooms.reduce((total, room) => total + room.contents.length, 0);
  const tabs = tabsFor(kind, entity, spawnCount);
  const view = tabs.some(([id]) => id === requestedView) ? requestedView : "overview";
  let body;
  if (view === "dialogue") {
    body = conversationGroup(entity);
  } else if (kind === "quests") {
    body = view === "objectives" ? questObjectives(entity, metadata) : view === "chain" ? questChain(entity) : questOverview(entity, metadata);
  } else if (kind === "npcs") {
    if (view === "quests") body = npcQuests(entity);
    else if (view === "locations") body = npcLocations(entity, metadata);
    else if (view === "loot") body = npcLoot(entity);
    else if (view === "model") body = `<div class="article-grid"><div class="stack">
      ${entity.data.model_choices?.length > 1 ? `<label class="filter-label model-appearance-choice">Appearance
      <select id="model-choice">${entity.data.model_choices.map(choice => opt(choice, readable(choice.split("/").pop().replace(/^shared_|\.[^.]+$/g, "")))).join("")}</select>
      </label>` : ""}
      ${modelPanel("In-game model")}</div>
      <aside class="stack">${section("Equipped weapons", npcGear(entity)) + npcStats(entity.data)}</aside></div>`;
    else body = npcOverview(entity, metadata);
  } else if (kind === "locations") {
    if (view === "quests") {
      body = section("Quests that start here", relatedRows(kind, card.id, entity, "quests",
        "No quest starts are recorded for this location."));
    } else if (card.category === "Building") {
      if (view === "model") body = modelPanel("In-game building model");
      else if (view === "map") body = locationOverview(entity, metadata);
      else if (view === "locations") body = placementSection(kind, entity);
      else if (view === "spawns") body = buildingSpawnsView(spawnRooms);
      else body = buildingOverview(entity, metadata, spawnRooms, spawnCount);
    }
    else body = view === "overview" ? locationOverview(entity, metadata) :
      section(({ npcs: "NPC placements", places: "Locations within " + card.name,
        resources: "Resource distribution", station_resources: "Mining station prices" })[view] || readable(view),
        relatedRows(kind, card.id, entity, view, view === "places" ? "No child locations are recorded." : "No linked records were recovered."));
  } else if (["items", "schematics", "resources", "ships"].includes(kind)) {
    body = inventoryDetail(kind, entity, view, metadata);
  } else {
    body = view === "references" ? '<div class="stack">' +
      section("Quest references", relatedRows(kind, card.id, entity, "quests", "No linked quests.")) +
      section("NPC references", relatedRows(kind, card.id, entity, "npcs", "No linked NPCs.")) + "</div>" : standaloneConversation(entity);
  }
  return { html: detailHeader(kind, entity, view, metadata, tabs) + body, view };
}
