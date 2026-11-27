export const sections = {
  quests: { title: "Quests", single: "Quest", description: "Find quest givers, follow story chains, and look up objectives and rewards." },
  npcs: { title: "NPCs", single: "NPC", description: "Find characters and creatures, their world placements, and the quest series they start." },
  locations: { title: "Locations", single: "Location", description: "Explore worlds, regions, and landmarks using the game’s authored maps and coordinates." },
  items: { title: "Items", single: "Item", description: "Look up equipment, crafted items, quest rewards, and their sources." },
  schematics: { title: "Schematics", single: "Schematic", description: "Find recipes, ingredient alternatives, crafting skills, and experimental ranges." },
  resources: { title: "Resources", single: "Resource", description: "Browse resource classes, quality ranges, harvesting worlds, and recipe uses." },
  ships: { title: "Ships", single: "Ship", description: "Browse player ship chassis, component slots, certifications, and deeds." },
  conversations: { title: "Dialogue", single: "Conversation", description: "Look up conversation codes, English game text, and source captions." },
};

const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, match => entities[match]);
export const integer = value => value == null ? "—" : Intl.NumberFormat("en-US", { maximumFractionDigits: 3 }).format(value);
export const readable = value => String(value ?? "").replace(/([a-z0-9])([A-Z])/g, "$1 $2").replaceAll("_", " ").replace(/^\w/, c => c.toUpperCase());
export const href = (kind, id) => kind === "home" ? "/" : `/${kind}${id ? "/" + encodeURIComponent(id) : ""}`;

export function loadingIndicator(message) {
  return `<span class="loading-indicator"><span class="loading-spinner" aria-hidden="true"></span>${escapeHTML(message)}</span>`;
}

const fileReference = value => typeof value === "string" &&
  /^(?:@[\w./\\-]+:[\w./-]+|(?:clientdata|client-tools|dsrc|src|data|object|appearance|datatables|shader|texture|string|snapshot|terrain|sound|script)\/\S+|\S+\.(?:iff|tpf|tab|stf|ws|trn|dds|png|script|scriptlib|java|cpp|h|cdf|apt|sat|lod|pob|mgn|msh|ans)(?::\d+)?)$/i.test(value);

export function copyMarkup(text, value = text, label = "Copy value", style = "") {
  return `<button type="button" class="copy-reference ${escapeHTML(style)}" data-copy-text="${escapeHTML(value)}"
    aria-label="${escapeHTML(label)}" title="${escapeHTML(label)}"><span class="copy-value">${escapeHTML(text)}</span>
    <span class="copy-glyph" aria-hidden="true"></span></button>`;
}

export function copyControl(text, value = text, label = "Copy value", style = "") {
  const contents = document.createElement("template");
  contents.innerHTML = copyMarkup(text, value, label, style);
  return contents.content.firstElementChild;
}

export function code(value) {
  if (!value) return "—";
  if (fileReference(value)) {
    return copyMarkup(value, value, value.startsWith("@") ? "Copy string code" : "Copy file reference", "copy-code");
  }
  return `<code>${escapeHTML(value)}</code>`;
}

export function coordinateValue(position, axes = ["X", "Y", "Z"]) {
  return axes.map((axis, i) => axis + " " + (Number.isFinite(position[i]) ? position[i] : "—")).join(" · ");
}

export function boundsValue(bounds) {
  return `X ${bounds[0]} to ${bounds[2]} · Z ${bounds[1]} to ${bounds[3]}`;
}

export function worldBounds(bounds) {
  const display = `X ${integer(bounds[0])} to ${integer(bounds[2])} · Z ${integer(bounds[1])} to ${integer(bounds[3])}`;
  return copyMarkup(display, boundsValue(bounds), "Copy coordinate bounds", "coordinate");
}

export function locationName(scene, metadata) {
  return scene ? metadata.locations[scene]?.name || readable(scene) : "Unresolved";
}

export function locationId(scene, metadata) {
  return metadata.locations[scene]?.alias_for || scene;
}

export function link(kind, id, label, className = "") {
  return `<a href="${href(kind, id)}"${className ? ` class="${className}"` : ""}>${escapeHTML(label || id)}</a>`;
}

export function searchResultGroups(results, query) {
  return Object.entries(results).filter(([kind, rows]) => sections[kind] && rows.length).map(([kind, rows]) => {
    const names = new Map();
    for (const row of rows) names.set(row.name, (names.get(row.name) || 0) + 1);
    const matches = rows.map(row => link(kind, row.id,
      names.get(row.name) > 1 ? row.name + " · " + row.id : row.name, "entity-name")).join("");
    const more = rows.length === 5 ? `<a href="${href(kind)}?q=${encodeURIComponent(query)}">All matching ${sections[kind].title.toLowerCase()}</a>` : "";
    return `<section><h3>${sections[kind].title}</h3>${matches}${more}</section>`;
  }).join("") || '<p class="empty">No entries match your search.</p>';
}

export function locationLink(scene, metadata) {
  return scene ? link("locations", locationId(scene, metadata), locationName(scene, metadata)) : '<span class="hint">Unresolved</span>';
}

export function tag(text, variant = "") {
  return text ? `<span class="tag ${variant}">${escapeHTML(text)}</span>` : "";
}

function archiveSource(path, line) {
  const reference = path.replaceAll("\\", "/");
  const [root, ...parts] = reference.split("/");
  if (!["dsrc", "src", "client-tools", "clientdata"].includes(root) || !parts.length ||
      parts.some(segment => !segment || segment === "." || segment === "..")) return "";
  const params = new URLSearchParams({ path: reference });
  if (Number.isInteger(line) && line > 0) params.set("line", line);
  return "/source?" + params;
}

export function sourceText(proof) {
  if (!proof?.path) return "";
  const suffix = proof.line ? `:${proof.line}` : proof.row ? ` · row ${proof.row}` : "";
  const reference = escapeHTML(proof.path + suffix);
  const root = proof.asset_root ?? (proof.scope === "supplied_clientdata" ? "clientdata" : null);
  const path = root && !proof.path.startsWith(root + "/") ? root + "/" + proof.path : proof.path;
  const url = archiveSource(path, proof.line);
  return url ? `<a class="source-reference source-link" href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${reference}</a>` :
    `<span class="source-reference">${reference}</span>`;
}

export function sourceControl(proof, className) {
  const content = document.createElement("template");
  content.innerHTML = sourceText(proof);
  const source = content.content.firstElementChild;
  if (source && className) source.classList.add(className);
  return source;
}

export function sectionSources(values) {
  return values?.length ? section("Source records", values.map(sourceText).join("")) : "";
}

export function facts(rows) {
  const visible = rows.filter(([, value]) => value !== null && value !== undefined && value !== "");
  return `<dl class="facts">${visible.map(([name, value]) => `<div class="fact-row"><dt>${escapeHTML(name)}</dt><dd>${value}</dd></div>`).join("")}</dl>`;
}

export function section(title, body, extraClass = "") {
  return `<section class="panel section ${extraClass}"><h2>${escapeHTML(title)}</h2>${body}</section>`;
}

export function columnHeading(label, key, sort) {
  if (!key) return `<th scope="col">${escapeHTML(label)}</th>`;
  const descending = sort === key + "-desc";
  const ascending = sort === key;
  const direction = ascending ? "ascending" : descending ? "descending" : "";
  const next = ascending ? key + "-desc" : key;
  return `<th scope="col"${direction ? ` aria-sort="${direction}"` : ""}>
    <button type="button" class="column-sort" data-sort="${escapeHTML(next)}" data-sort-key="${escapeHTML(key)}">
      ${escapeHTML(label)} <span aria-hidden="true">${ascending ? "▲" : descending ? "▼" : "↕"}</span>
    </button></th>`;
}

export function plainObject(fields) {
  const keys = Object.keys(fields || {});
  const labels = { Damagelevelmodifier: "Damage level modifier", Def: "Defense", HP: "Health", XP: "XP",
    damagePerSecond: "Damage per second", ToHit: "To hit", BaseLevel: "Base level" };
  return keys.length ? facts(keys.map(key => {
    const value = fields[key];
    const coordinate = /(?:^|_)[XYZ](?:$|_)|(?:min|max|world|pos|loc|offset)[XYZ]$/i.test(key);
    const numeric = String(value ?? "").trim() && Number.isFinite(Number(value));
    return [labels[key] || readable(key), fileReference(value) ? code(value) :
      coordinate && numeric ? copyMarkup(value, value, "Copy " + readable(key), "coordinate") : escapeHTML(value)];
  })) :
    '<p class="muted">No authored values indexed.</p>';
}

export function localized(value, empty = "") {
  return (value?.text ? `<p class="long-text">${escapeHTML(value.text)}</p>` :
    empty ? `<p class="muted">${escapeHTML(empty)}</p>` : "") +
    (value?.code ? copyMarkup(value.code, value.code, "Copy string code", "source copy-code") : "");
}

export function linkRows(rows) {
  const titles = new Map();
  for (const entry of rows) {
    if (entry.card) titles.set(entry.card.name, (titles.get(entry.card.name) || 0) + 1);
  }
  return rows.map((entry) => {
    const data = entry.card;
    const labels = { quests: "Recommended level ", ships: "Tier " };
    const recommended = data?.level != null ? (labels[entry.kind] || "Level ") + integer(data.level) : "";
    const required = data?.min_level != null ? "Requires level " + integer(data.min_level) : "";
    const price = entry.detail?.price != null ? integer(entry.detail.price) + " credits per unit" : "";
    const pools = entry.detail?.pool_min != null && entry.detail?.pool_max != null ?
      integer(entry.detail.pool_min) + "–" + integer(entry.detail.pool_max) + " pool size" : "";
    const context = [recommended, required, data?.chapter || data?.category, entry.detail?.reason,
      entry.detail?.slot, price, pools].filter(Boolean).join(" · ");
    const duplicate = data && (titles.get(data.name) > 1 || entry.detail?.shared_name);
    return `<div class="rich-row">${data ? link(entry.kind, entry.id, data.name, "entity-name") : code(entry.id)}
      ${context ? `<p>${escapeHTML(context)}</p>` : ""}${duplicate ? code(entry.id) : ""}</div>`;
  }).join("");
}

export function relatedRows(kind, id, entity, role, empty) {
  const info = entity.related[role];
  if (!info?.count) return `<p class="muted">${escapeHTML(empty)}</p>`;
  return `<div data-related-list="${role}">${linkRows(info.rows)}${info.count > info.rows.length ?
    `<button class="button" type="button" data-related-more="${role}" data-kind="${kind}" data-id="${escapeHTML(id)}" data-size="${info.rows.length}" data-page="2">Show more</button>` : ""}</div>`;
}

export function placementSection(kind, entity) {
  const hasAcquisition = kind === "items" &&
    (entity.has_loot || entity.data.acquisitions?.length || entity.related.reward_items_quests?.count);
  const title = { items: "Where to find this item", locations: "Where this building is located", npcs: "Spawn locations and sources" }[kind];
  return section(title, `
    <form id="placement-filter" class="placement-filters" role="search" aria-label="Filter locations">
      <label class="filter-label">World<select name="scene" aria-label="Filter by world" disabled><option value="">All worlds</option></select></label>
      <label class="filter-label">Location name<input class="control-input" name="q" type="search" maxlength="160" placeholder="City, region, building, or cell"></label>
      ${kind === "npcs" ? '<label class="filter-label">Spawner type<select name="spawn" disabled><option value="">All types</option></select></label>' : ""}
      <button class="button" type="submit">Filter</button>
      <a class="button subtle" id="clear-placement-filter" href="${href(kind, entity.card.id)}?view=locations">Clear filters</a>
    </form>
    <div id="placement-results" class="placement-results" aria-live="polite" aria-busy="true" tabindex="-1">
      ${loadingIndicator("Loading locations…")}</div>
    ${hasAcquisition ? `<p class="placement-acquisition"><a href="${href(kind, entity.card.id)}?view=acquisition">See all acquisition sources</a></p>` : ""}`);
}

function spawnName(entry) {
  return entry.name || readable(entry.ref.split("/").pop().replace(/^shared_/, "").replace(/\.iff$/i, ""));
}

export function spawnSummary(config) {
  const names = config.entries.slice(0, 3).map(spawnName);
  if (config.entries.length > names.length) names.push("+" + (config.entries.length - names.length) + " more");
  const choices = ["random", "conditional"].includes(config.selection) && config.entries.length > 1 ? "Possible spawns " : "Spawns ";
  const targets = names.length ? choices + names.join(", ") : "Spawn configuration unresolved";
  const label = config.count_label;
  const suffix = label?.startsWith("Per ") ? " " + label.toLowerCase() : label === "Initial wave" ? " in the initial wave" : " configured";
  const population = config.count_label === "Maximum active" ? "Up to " + integer(config.count) + " active" :
    integer(config.count) + suffix;
  return [targets, config.count != null && population].filter(Boolean).join(" · ");
}

export function spawnDetails(config, { showType = true } = {}) {
  const { delay_min: minimum, delay_max: maximum } = config;
  const delay = minimum == null && maximum == null ? null :
    [minimum, maximum !== minimum ? maximum : null].filter(value => value != null).map(integer).join("–") + " s";
  const metrics = [
    [config.count_label || "Count", config.count != null ? integer(config.count) : null],
    ["Respawn delay", delay],
    ["Radius", config.radius != null ? escapeHTML(config.radius) + " m" : null],
    ["Patrol route", config.patrol_type && escapeHTML(readable(config.patrol_type))],
    ["Trigger", config.trigger && escapeHTML(readable(config.trigger))],
    ["Spawn limit", config.maximum != null ? integer(config.maximum) : null],
    ["Weight", config.weight != null ? integer(config.weight) : null],
  ].filter(([, value]) => value).map(([label, value]) =>
    `<div class="spawn-metric"><dt>${escapeHTML(label)}</dt><dd>${value}</dd></div>`).join("");
  const sources = new Map();
  function addSource(proof, label) {
    if (!proof?.path) return;
    const key = JSON.stringify([proof.path, proof.line, proof.row]);
    if (!sources.has(key)) sources.set(key, { proof, label });
  }
  addSource(config.source, config.source?.path?.endsWith(".tpf") ? "Spawner template" : "Configuration");
  addSource(config.script_source, "Spawn script");
  addSource(config.delay_source, "Respawn script");
  addSource(config.caller_source, "Script caller");
  const entries = config.entries.map(entry => {
    const name = spawnName(entry);
    addSource(entry.source, name);
    addSource(entry.selector_source, "Spawn selector");
    const title = entry.npc ? link("npcs", entry.npc, name) : entry.item ? link("items", entry.item, name) :
      entry.ref.startsWith("object/") ? code(entry.ref) : escapeHTML(name);
    const showCount = entry.count != null && (config.entries.length > 1 || entry.count !== config.count);
    return `<li>${showCount ? `<span class="spawn-target-count">${integer(entry.count)} ×</span>` : ""}
      <span class="spawn-target-name">${title}</span>${entry.maximum != null ? `<small>Limit ${integer(entry.maximum)}</small>` : ""}
      ${entry.role ? `<small>${escapeHTML(readable(entry.role))}</small>` : ""}${config.entries.length > 1 && entry.rows && config.total_rows ?
        `<small>${integer(entry.rows)} of ${integer(config.total_rows)} table rows</small>` : ""}</li>`;
  }).join("");
  const status = config.configuration_status === "missing_row" ? "The placement has no matching spawn-table row." :
    config.configuration_status === "scripted" ? "Spawn choices are set by the controlling script." : "Creature choices are unresolved.";
  const heading = ["random", "conditional"].includes(config.selection) && config.entries.length > 1 ? "Possible spawns" : "Spawn targets";
  return `<div class="spawn-details">
    ${showType ? `<strong>${escapeHTML(config.type)}</strong>` : ""}
    ${metrics ? `<dl class="spawn-metrics">${metrics}</dl>` : ""}
    ${entries ? `<div class="spawn-targets"><p>${heading}</p><ul class="spawn-target-list">${entries}</ul></div>` :
      `<p class="muted">${escapeHTML(status)}</p>`}
    ${sources.size ? `<details class="spawn-sources">
      <summary>Source records <span>(${integer(sources.size)})</span></summary>
      <ul class="spawn-source-list">${[...sources.values()].map(({ proof, label }) =>
        `<li><span class="spawn-source-label">${escapeHTML(label)}</span>${sourceText(proof)}</li>`).join("")}</ul>
    </details>` : ""}
  </div>`;
}

export function placementResults(result, metadata, filtered, sort) {
  if (!result.rows.length) return '<p class="muted">' + (filtered ?
    "No recorded locations match these filters." : "No mapped locations are recorded for this entry.") + "</p>";
  const npc = result.rows[0]?.subject;
  const scope = { placed: "Placed in game data", scripted: "Scripted spawn", region: "Possible within this region",
    unregistered: "Unregistered buildout", runtime: "Location chosen at runtime", definition: "Definition only",
    development: "Development or unbound helper" };
  return `<p class="count-label">${integer(result.total)} ${npc ? (result.total === 1 ? "spawn record" : "spawn records") : (result.total === 1 ? "location" : "locations")}</p>
    <div class="table-wrap" tabindex="0" role="region" aria-label="Recorded locations">
    <table class="data-table placement-table"><thead><tr>
      ${columnHeading("Location", "location", sort)}${columnHeading("World", "world", sort)}
      ${columnHeading(npc ? "Spawner type" : "Found as", "method", sort)}${columnHeading("World position", "position", sort)}
      ${columnHeading("Source", "source", sort)}
    </tr></thead><tbody>${result.rows.map(row => {
      const available = row.map_location && (row.coordinates || row.cell_index != null);
      const label = available ? `<a href="${href("locations", row.map_location)}?placement=${escapeHTML(row.id)}">${escapeHTML(row.location)}</a>` :
        row.location_id ? link("locations", row.location_id, row.location) : escapeHTML(row.location);
      const details = [!npc && row.place !== row.location && row.place, row.cell && "Cell " + readable(row.cell)].filter(Boolean);
      const source = row.related ? link(...row.related) : "";
      const spawner = row.spawner_template ? `<small>${link("items", row.spawner_template, readable(row.spawner_template.split("/").pop().replace(/\.iff$/, "")))}</small>` : "";
      const config = npc && row.spawner ? { ...row.spawner, entries: row.spawner.entries.filter(entry => entry.npc === npc) } : row.spawner;
      const origin = row.spawn_origin?.type;
      const local = !row.coordinates && row.local_coordinates && row.cell ?
        `<small>Cell coordinates ${copyMarkup(row.local_coordinates.join(", "), coordinateValue(row.local_coordinates), "Copy cell coordinates", "coordinate")}</small>` : "";
      return `<tr><td>${label}${details.length ? `<small>${escapeHTML(details.join(" · "))}</small>` : ""}</td>
        <td>${row.scene ? escapeHTML(locationName(row.scene, metadata)) : '<span class="muted">Unspecified</span>'}</td>
        <td>${escapeHTML(row.spawn_type || row.method)}${row.scope ? `<small>${escapeHTML(scope[row.scope])}</small>` : ""}
          ${origin && origin !== (row.spawn_type || row.method) ? `<small>${escapeHTML(origin)}</small>` : ""}
          ${spawner}${config ? `<details class="cell-spawn-details"><summary>${escapeHTML(spawnSummary(config))}</summary>${spawnDetails(config, { showType: !npc })}</details>` : ""}
          ${row.event ? `<small>Event ${escapeHTML(readable(row.event))}</small>` : ""}</td>
        <td>${row.coordinates ? worldCoordinates(row.coordinates) : row.scope === "region" ? "Region-wide" :
          row.scope === "runtime" ? "Runtime position" : worldCoordinates(null)}${local}
          ${row.status === "spawner_anchor" && row.coordinates ? '<small>Spawner anchor</small>' : ""}</td>
        <td>${source}${sourceText(row.source)}${sourceText(row.region_source)}${sourceText(row.anchor_source)}</td></tr>`;
    }).join("")}</tbody></table></div>${pagination(result, "placement-page")}`;
}

function percent(probability) {
  if (probability == null) return "Unresolved";
  return new Intl.NumberFormat(undefined, { style: "percent", maximumSignificantDigits: 5 }).format(probability);
}

export function dropChance(entry) {
  const conditional = (entry.details || []).filter(detail => detail.condition);
  const derived = conditional.length === 1 ? conditional[0].conditional_probability : null;
  const range = entry.minimum_chance != null && entry.minimum_chance < entry.chance ?
    percent(entry.minimum_chance) + "–" : "";
  const label = entry.chance != null ? range + percent(entry.chance) : derived != null ?
    percent(derived) + "<small>When eligible</small>" : conditional.length ? "Conditional" : "Unresolved";
  return label + (entry.qualified && entry.name && entry.chance != null ? "<small>At base level</small>" : "");
}

export function dropDetails(entry) {
  const details = entry.details || [];
  if (!details.length) return "";
  const table = details.map(detail => {
    const chance = detail.condition ?
      `${detail.conditional_probability == null ? "Chance unresolved" : percent(detail.conditional_probability)} · ${escapeHTML(detail.condition)}` :
      `${percent(detail.table_chance)} per table roll`;
    const rollInfo = detail.condition || (detail.rolls === 1 && detail.gate === 1) ? "" : detail.table_selection ?
      `${detail.gate === 1 ? "" : " · " + percent(detail.gate) + " table selection"} · ${integer(detail.rolls)} item roll${detail.rolls === 1 ? "" : "s"}` :
      ` · ${integer(detail.rolls)} ${detail.rolls === 1 ? "attempt" : "attempts"} at ${percent(detail.gate)}`;
    return `<div class="drop-contribution"><strong>${escapeHTML(detail.channel)}</strong>
      <small>${escapeHTML(detail.selector)} · ${chance}${rollInfo}
      ${detail.minimum_level ? ` · Level ${integer(detail.minimum_level)}+` : ""}</small>
      ${sourceText(detail.rule)}${sourceText(detail.profile)}${detail.sources.map(proof => sourceText(proof)).join("")}</div>`;
  }).join("");
  const channels = [...new Set(details.map(detail => detail.channel))].join(", ");
  return `<details class="cell-spawn-details"><summary>${escapeHTML(channels)}</summary>${table}</details>`;
}

export function npcDropResults(result, sort = "chance-desc") {
  const rows = result.rows.map(entry => {
    const id = copyMarkup(entry.id, entry.id, `Copy item ID ${entry.id}`, "copy-code");
    const item = entry.name ? `${link("items", entry.id, entry.name)}<small class="entity-sub">${id}</small>` : id;
    return `<tr><td>${item}</td><td class="drop-chance">${dropChance(entry)}</td><td>${dropDetails(entry)}</td></tr>`;
  }).join("");
  if (!rows) return '<p class="muted">No loot items match this search.</p>';
  return `<p class="count-label">${integer(result.total)} possible item${result.total === 1 ? "" : "s"}</p>
    <div class="table-wrap" tabindex="0" role="region" aria-label="Creature loot">
    <table class="data-table"><thead><tr>${columnHeading("Item", "name", sort)}${columnHeading("Drop chance", "chance", sort)}
    <th scope="col">Rolls and source records</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${pagination(result, "npc-drop-page")}`;
}

export function resourceIcon(ident, className, alt = "") {
  return ident ? `<img class="${className}" src="/api/resource-image?class=${encodeURIComponent(ident)}" alt="${escapeHTML(alt)}" decoding="async">` : "";
}

export function pagination(result, action) {
  const pages = result.pages ?? Math.max(1, Math.ceil(result.total / result.size));
  if (pages <= 1) return "";
  const prev = result.page === 1 ? " disabled" : "";
  const next = result.page >= pages ? " disabled" : "";
  return `<div class="pagination"><span>Page ${integer(result.page)} of ${integer(pages)}</span>
    <button class="button" type="button" data-${action}="${result.page - 1}"${prev}>Previous</button>
    <button class="button" type="button" data-${action}="${result.page + 1}"${next}>Next</button></div>`;
}

export function opt(value, title, selected = "") {
  return `<option value="${escapeHTML(value)}"${selected === value ? " selected" : ""}>${escapeHTML(title)}</option>`;
}

export function worldCoordinates(position) {
  if (!position) return '<span class="muted">World coordinates unresolved</span>';
  const display = `X ${integer(position[0])} · Y ${integer(position[1])} · Z ${integer(position[2])}`;
  return copyMarkup(display, coordinateValue(position), "Copy world coordinates", "coordinate");
}

export function installClipboard() {
  const notice = document.createElement("div");
  notice.className = "copy-notice";
  notice.setAttribute("role", "status");
  let hide;
  document.addEventListener("click", async event => {
    const target = event.target.closest?.("button[data-copy-text]");
    if (!target || target.disabled) return;
    const value = target.dataset.copyText;
    let copied = false;
    try {
      await navigator.clipboard.writeText(value);
      copied = true;
    } catch {
      const field = document.createElement("textarea");
      field.className = "copy-buffer";
      field.value = value;
      field.readOnly = true;
      (document.fullscreenElement || document.body).append(field);
      field.select();
      try {
        copied = document.execCommand("copy");
      } catch {
        copied = false;
      } finally {
        field.remove();
        target.focus({ preventScroll: true });
      }
    }
    clearTimeout(hide);
    (document.fullscreenElement || document.body).append(notice);
    notice.textContent = copied ? "Copied to clipboard" : "Clipboard access is unavailable";
    notice.hidden = false;
    if (copied) {
      target.dataset.copied = "true";
      setTimeout(() => { delete target.dataset.copied; }, 1600);
    }
    hide = setTimeout(() => { notice.hidden = true; }, 1800);
  });
}

export async function getJSON(path, params = {}, signal) {
  const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value != null && value !== ""));
  const url = query.size ? `${path}?${query}` : path;
  const response = await fetch(url, { signal });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `Request failed (${response.status})`);
  }
  return response.json();
}
