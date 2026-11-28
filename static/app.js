import {
  escapeHTML as h, getJSON, href, installClipboard, linkRows, loadingIndicator, locationId, locationName, npcDropResults, placementResults, searchResultGroups,
  sections, sourceText,
} from "./ui.js";
import { browsePage, dialogueLines, dialogueNpcs, entityPage, homePage } from "./views.js";
import { lootNPCResults, otherLootResults } from "./inventory-views.js";

const main = document.querySelector("#main");
const pageLoading = document.querySelector("#page-loading");
const modal = document.querySelector("#archive-search");
const globalQuery = document.querySelector("#global-query");
const globalResults = document.querySelector("#global-results");
const browseKeys = ["q", "scene", "category", "status", "min", "max", "acquisition", "storage", "sound_use", "sort", "page"];
const pageVersion = document.documentElement.dataset.archiveVersion;
let catalog;
let versionRequest;
let activeController;
let mapController;
let releaseModel;
let current;
let dialogState;
let searchController;
let searchTimer;

function route() {
  const segments = location.pathname.split("/").filter(Boolean);
  const kind = Object.hasOwn(sections, segments[0]) ? segments[0] : "home";
  let id = null;
  if (segments.length > 1) {
    try { id = decodeURIComponent(segments.slice(1).join("/")); }
    catch { id = segments.slice(1).join("/"); }
  }
  const search = new URLSearchParams(location.search);
  return { kind, id, search, view: search.get("view") || "overview" };
}

function onPageError(error) {
  main.innerHTML = `<section class="notice" role="alert"><h1>Unable to load this page</h1><p>${h(error.message)}</p>
    <p><a href="/">Open the archive home page</a></p></section>`;
}

function reloadIfUpdated() {
  if (!pageVersion) return;
  if (!versionRequest) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 1500);
    versionRequest = fetch("/api/health", { cache: "no-store", signal: controller.signal })
      .then(response => response.ok ? response.json() : null)
      .then(health => {
        if (health?.version && health.version !== pageVersion) location.reload();
      })
      .catch(() => {})
      .finally(() => {
        clearTimeout(timeout);
        versionRequest = undefined;
      });
  }
  return versionRequest;
}

function visit(target, navigation) {
  if (target.href === location.href) return;
  const previousPath = location.pathname;
  history.pushState(null, "", target);
  if (target.pathname !== previousPath) window.scrollTo({ top: 0, behavior: "instant" });
  if (modal.open) modal.close();
  render(navigation);
}

function filterRoute(changes) {
  const next = new URL(location.href);
  for (const [key, value] of Object.entries(changes)) {
    if (value == null || value === "" || (key === "page" && Number(value) === 1)) next.searchParams.delete(key);
    else next.searchParams.set(key, value);
  }
  return next;
}

function readFilters(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function focusResults(selector) {
  const results = main.querySelector(selector);
  if (!results) return;
  results.focus({ preventScroll: true });
  results.scrollIntoView({ block: "start", behavior: "instant" });
}

async function render({ page: pageNumber, sortKey } = {}) {
  const pager = pageNumber ? { page: pageNumber } : undefined;
  const firstRender = !activeController;
  if (activeController) activeController.abort();
  mapController?.abort();
  if (releaseModel) releaseModel();
  releaseModel = undefined;
  dialogState = undefined;
  const controller = new AbortController();
  activeController = controller;
  const { signal } = controller;
  const { kind, id, search, view } = route();
  const samePage = current?.kind === kind && current.id === id;
  const retainContent = samePage && !main.querySelector(":scope > .loading-panel, :scope > .notice[role=alert]");
  let message = "Loading the archive…";
  if (sortKey) message = "Sorting results…";
  else if (pager) message = `Loading page ${pager.page}…`;
  else if (id) message = `Loading ${sections[kind].single.toLowerCase()} details…`;
  else if (kind !== "home") message = `Loading ${kind === "npcs" ? "NPCs" : sections[kind].title.toLowerCase()}…`;
  else if (search.has("q")) message = "Searching the archive…";
  main.setAttribute("aria-busy", "true");
  pageLoading.innerHTML = retainContent ? loadingIndicator(message) : `<span class="sr-only">${h(message)}</span>`;
  if (!retainContent) main.innerHTML = `<section class="loading-panel" aria-hidden="true">${loadingIndicator(message)}</section>`;
  for (const anchor of document.querySelectorAll(".site-nav a")) {
    if (anchor.getAttribute("href") === href(kind)) anchor.setAttribute("aria-current", "page");
    else anchor.removeAttribute("aria-current");
  }
  let resultsFocus;
  try {
    catalog ??= getJSON("/api/meta").catch(error => {
      catalog = undefined;
      throw error;
    });
    let request, query, filters;
    if (kind === "home") {
      query = (search.get("q") || "").trim().slice(0, 160);
      request = query ? getJSON("/api/search", { q: query }, signal) : null;
    } else if (!id) {
      filters = Object.fromEntries(browseKeys.map(key => [key, search.get(key) || ""]));
      request = getJSON("/api/list", { type: kind, ...filters }, signal);
    } else {
      request = samePage && current.entity ? current.entity : getJSON("/api/entity", { type: kind, id, sound_groups: 1 }, signal);
    }
    const [metadata, result] = await Promise.all([catalog, request]);
    if (signal.aborted) return;
    document.querySelector("#snapshot").textContent = "Game source " + metadata.revision.slice(0, 9);
    if (kind === "home") {
      const submitted = result && current?.kind === "home" && current.query !== query;
      current = { kind, id, metadata, query };
      document.title = metadata.title;
      main.innerHTML = homePage(metadata, query, result);
      if (submitted) resultsFocus = "#archive-results-title";
    } else if (!id) {
      current = { kind, id, metadata };
      document.title = sections[kind].title + " · " + metadata.title;
      main.innerHTML = browsePage(kind, filters, result, metadata);
      if (pager) resultsFocus = "#browse-results-title";
    } else {
      const entity = result;
      const page = entityPage(kind, entity, view, metadata);
      if (kind === "items" && view === "loot") {
        const url = new URL(location.href);
        url.searchParams.set("view", page.view);
        history.replaceState(history.state, "", url);
      }
      current = { kind, id, metadata, entity, view: page.view };
      document.title = entity.card.name + " · " + metadata.title;
      main.innerHTML = page.html;
      const detailLoad = initDetail(current, search, signal, pager);
      if (sortKey) await detailLoad;
    }
  } catch (error) {
    if (!signal.aborted) onPageError(error);
  } finally {
    if (!signal.aborted) {
      main.removeAttribute("aria-busy");
      pageLoading.textContent = "";
      if (resultsFocus) focusResults(resultsFocus);
      if (sortKey) focusSortColumn(sortKey);
      if (!firstRender) reloadIfUpdated();
    }
  }
}

function focusSortColumn(key) {
  main.querySelector(`[data-sort-key="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
}

function initDetail(state, search, signal, pager) {
  if (main.querySelector("#detail-map, #building-floor-plan") ||
      (state.kind === "locations" && search.has("placement"))) mountDetailMaps(state, search, signal);
  if (main.querySelector("#placement-results")) return loadPlacements(state, search, signal, pager);
  if (main.querySelector("#npc-drop-results")) return loadNPCDrops(state, search, signal, pager);
  if (main.querySelector("#loot-npc-results")) return loadLootNPCs(state, search, signal, pager);
  if (main.querySelector("#entity-model")) showModel(state, signal, search.get("variant") || "");
  if (!main.querySelector("#dialogue-lines")) return;
  const choices = state.entity.related.dialogue?.rows || [];
  const table = state.kind === "conversations" ? state.id :
    choices.find(choice => choice.id === search.get("table"))?.id || choices[0]?.id;
  if (!table) return;
  const select = main.querySelector("#linked-dialogue");
  if (select) select.value = table;
  const npc = state.kind === "npcs" ? state.id : search.get("npc") || "";
  dialogState = { state, table, npc, query: search.get("line") || "", filter: "", sort: "", page: 1, signal, nonce: 0 };
  loadLines(dialogState);
}

async function loadPlacements(state, search, signal, pager) {
  const host = main.querySelector("#placement-results");
  const form = main.querySelector("#placement-filter");
  const scene = search.get("scene") || "";
  const spawn = search.get("spawn") || "";
  const query = search.get("q") || "";
  const sort = search.get("sort") || "";
  form.elements.q.value = query;
  form.querySelector("#clear-placement-filter").hidden = !scene && !spawn && !query;
  if (pager) host.innerHTML = loadingIndicator(`Loading locations page ${pager.page}…`);
  try {
    const result = await getJSON("/api/placements", {
      type: state.kind, id: state.id, scene, spawn, q: query, sort, page: search.get("page") || 1, size: 30,
    }, signal);
    if (signal.aborted) return;
    const worlds = result.scenes.map(id => [id, locationName(id, state.metadata)]).sort((a, b) => a[1].localeCompare(b[1]));
    if (scene && !result.scenes.includes(scene)) worlds.push([scene, locationName(scene, state.metadata)]);
    const selector = form.elements.scene;
    selector.replaceChildren(new Option("All worlds", ""), ...worlds.map(([id, name]) => new Option(name, id)));
    selector.value = scene;
    selector.disabled = !worlds.length;
    if (form.elements.spawn) {
      const select = form.elements.spawn;
      select.replaceChildren(new Option("All types", ""), ...(result.types || []).map(type => new Option(type, type)));
      select.value = spawn;
      select.disabled = !result.types?.length;
    }
    host.innerHTML = placementResults(result, state.metadata, Boolean(scene || query || spawn), sort);
  } catch (error) {
    if (!signal.aborted) host.textContent = "Location records could not load. " + error.message;
  } finally {
    if (!signal.aborted) {
      host.removeAttribute("aria-busy");
      if (pager) focusResults("#placement-results");
    }
  }
}

async function loadNPCDrops(state, search, signal, pager) {
  const host = main.querySelector("#npc-drop-results");
  const query = search.get("q") || "";
  const sort = search.get("sort") || "chance-desc";
  main.querySelector("#npc-drop-filter").elements.q.value = query;
  try {
    const result = await getJSON("/api/npc-drops", {
      type: "npcs", id: state.id, q: query, sort, page: search.get("page") || 1,
    }, signal);
    if (!signal.aborted) host.innerHTML = npcDropResults(result, sort);
  } catch (error) {
    if (!signal.aborted) host.textContent = "Loot could not load. " + error.message;
  } finally {
    if (!signal.aborted) {
      host.removeAttribute("aria-busy");
      if (pager) focusResults("#npc-drop-results");
    }
  }
}

async function loadLootNPCs(state, search, signal, pager) {
  const target = main.querySelector("#loot-npc-results");
  const other = main.querySelector("#other-loot-results");
  const query = search.get("q") || "";
  const sort = search.get("sort") || "chance-desc";
  main.querySelector("#item-loot-filter").elements.q.value = query;
  if (pager) target.innerHTML = loadingIndicator(`Loading loot sources page ${pager.page}…`);
  try {
    const result = await getJSON("/api/item-loot-npcs", {
      type: "items", id: state.id, q: query, sort, page: search.get("page") || 1, size: 30,
    }, signal);
    if (signal.aborted) return;
    target.innerHTML = lootNPCResults(result, Boolean(query), sort);
    other.innerHTML = otherLootResults(result.other_sources || []);
    other.hidden = !other.innerHTML;
  } catch (error) {
    if (!signal.aborted) target.textContent = "Loot sources could not load. " + error.message;
  } finally {
    if (!signal.aborted) {
      target.removeAttribute("aria-busy");
      if (pager) focusResults("#loot-npc-results");
    }
  }
}

async function mountDetailMaps(state, search, signal) {
  const map = main.querySelector("#detail-map");
  const floorPlan = main.querySelector("#building-floor-plan");
  const placementId = state.kind === "locations" ? search.get("placement") : null;
  let selection;
  if (placementId) {
    try {
      const entry = await getJSON("/api/placement", { id: placementId }, signal);
      if (locationId(entry.map_location, state.metadata) === locationId(state.id, state.metadata)) selection = entry;
      else throw new Error("This placement belongs to a different location.");
    } catch (error) {
      if (signal.aborted) return;
      const notice = document.createElement("p");
      notice.className = "notice";
      notice.textContent = "The requested placement is unavailable. " + error.message;
      (map || floorPlan || main.querySelector(".detail-tabs")).before(notice);
    }
  }
  if (signal.aborted) return;
  if (map) {
    await showMap(state, undefined, selection?.marker, search.get("chart"));
    if (!signal.aborted && selection?.marker) map.scrollIntoView({ block: "start", behavior: "instant" });
  }
  if (floorPlan || selection?.cell_index != null) {
    try {
      const { mountBuildingPlan, revealBuildingRoom } = await import("./building-viewer.js");
      if (signal.aborted) return;
      if (floorPlan) mountBuildingPlan(floorPlan, state.entity.data, signal, selection);
      else revealBuildingRoom(selection.cell_index);
    } catch {
      if (!signal.aborted && floorPlan) floorPlan.textContent = "The floor plan could not load. Cell contents are listed below.";
    } finally {
      if (!signal.aborted) floorPlan?.removeAttribute("aria-busy");
    }
  }
}

async function showMap(state, selectedScene, focusMarker, requestedChart) {
  const host = main.querySelector("#detail-map");
  if (!host) return;
  mapController?.abort();
  const { signal } = mapController = new AbortController();
  const { data, card } = state.entity;
  const charts = state.metadata.maps;
  let options;
  if (state.kind === "npcs") {
    const points = data.locations.filter(point => point.coordinates);
    const scene = selectedScene || points[0]?.scene;
    const markers = points.filter(point => point.scene === scene).map(point => ({
      ...point, name: card.name, context: data.category,
      cell: point.cell?.name || (point.cell?.index ? "Cell " + point.cell.index : undefined),
    }));
    const areas = new Set(markers.map(point => point.map_id).filter(id => id && id !== scene));
    options = { scene, preferred: areas.size === 1 ? [...areas][0] : scene, markers };
  } else {
    const scene = data.map?.scene || data.scene;
    const preferred = charts[scene]?.regions?.includes(requestedChart) && !focusMarker ? requestedChart : data.map?.id;
    const markers = data.category === "Building" && !data.extent ? [{
      id: card.id, name: card.name, layer: "building_enterable", coordinates: data.coordinates,
      enterable: true, source: data.source, template: data.template, context: data.context,
    }] : [];
    options = { scene, preferred, chartLabel: data.map_scene ? state.metadata.locations[scene]?.name : data.name,
      location: data, definitions: state.metadata.map_layers, markers, focusMarker };
  }
  host.setAttribute("aria-busy", "true");
  host.innerHTML = loadingIndicator("Loading map…");
  try {
    const { mountMap } = await import("./map-viewer.js");
    if (!signal.aborted) await mountMap(host, { ...options, charts, signal });
  } catch (error) {
    if (!signal.aborted) host.textContent = "Map unavailable. " + error.message;
  } finally {
    if (!signal.aborted) host.removeAttribute("aria-busy");
  }
}

async function showModel(state, signal, variant) {
  const container = main.querySelector("#entity-model");
  const field = { npcs: "npc", ships: "ship", items: "item", locations: "building" }[state.kind];
  try {
    const [profile, { mountModelViewer }] = await Promise.all([
      getJSON("/api/model", { [field]: state.id, variant }, signal),
      import("./model-viewer.js"),
    ]);
    if (!signal.aborted) {
      const choice = main.querySelector("#model-choice");
      if (choice && profile.chosen) choice.value = profile.chosen;
      releaseModel = mountModelViewer(container, profile);
    }
  } catch (error) {
    if (!signal.aborted) container.textContent = "Model unavailable: " + error.message;
  } finally {
    if (!signal.aborted) container.removeAttribute("aria-busy");
  }
}

async function loadLines(state, paging = false, sortKey) {
  const { table, npc, query, filter, sort, page, signal } = state;
  const target = main.querySelector("#dialogue-lines");
  const contacts = main.querySelector("#dialogue-npcs");
  if (contacts.dataset.table !== table) contacts.replaceChildren();
  const nonce = ++state.nonce;
  target.setAttribute("aria-busy", "true");
  target.innerHTML = loadingIndicator(paging ? `Loading dialogue page ${page}…` : "Loading dialogue…");
  const context = main.querySelector("#conversation-context");
  if (context) {
    const match = state.state.entity.related.dialogue.rows.find(item => item.id === table);
    context.innerHTML = `<p class="muted">${h(match?.detail?.reason)}</p>${sourceText(match?.detail?.source)}`;
    main.querySelector("#open-conversation").href = href("conversations", table);
  }
  try {
    const result = await getJSON("/api/dialogue", { table, q: query, status: filter, sort, page, size: 35, npc }, signal);
    if (signal.aborted || nonce !== state.nonce) return;
    if (contacts.dataset.table !== table) {
      contacts.innerHTML = dialogueNpcs(table, result.npcs);
      contacts.dataset.table = table;
    }
    target.innerHTML = dialogueLines(result, query, filter, sort);
  } catch (error) {
    if (signal.aborted || nonce !== state.nonce) return;
    target.textContent = error.message;
  }
  target.removeAttribute("aria-busy");
  if (paging) focusResults("#dialogue-lines");
  if (sortKey) focusSortColumn(sortKey);
}

async function loadRelated(button) {
  const { signal } = activeController;
  const label = button.textContent;
  button.disabled = true;
  button.innerHTML = loadingIndicator("Loading…");
  try {
    const result = await getJSON("/api/related", {
      type: button.dataset.kind, id: button.dataset.id, role: button.dataset.relatedMore,
      page: button.dataset.page, size: button.dataset.size, sound_use: button.dataset.soundUse,
    }, signal);
    if (signal.aborted || !button.isConnected) return;
    button.insertAdjacentHTML("beforebegin", linkRows(result.rows, button.dataset.soundUse));
    if (result.page * result.size >= result.total) button.remove();
    else button.dataset.page = result.page + 1;
  } catch (error) {
    if (!signal.aborted && button.isConnected) {
      const notice = document.createElement("p");
      notice.textContent = error.message;
      notice.setAttribute("role", "alert");
      button.before(notice);
    }
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}

function openSearch() {
  if (!modal.open) modal.showModal();
  globalQuery.focus();
  if (!globalQuery.value) globalResults.textContent = "";
}

async function globalSearch() {
  const q = globalQuery.value.trim();
  if (searchController) searchController.abort();
  if (!q) {
    globalResults.textContent = "";
    globalResults.removeAttribute("aria-busy");
    return;
  }
  searchController = new AbortController();
  const { signal } = searchController;
  globalResults.setAttribute("aria-busy", "true");
  globalResults.innerHTML = loadingIndicator("Searching…");
  try {
    const results = await getJSON("/api/search", { q }, signal);
    if (signal.aborted) return;
    globalResults.innerHTML = searchResultGroups(results, q);
  } catch (error) {
    if (!signal.aborted) globalResults.textContent = error.message;
  } finally {
    if (!signal.aborted) globalResults.removeAttribute("aria-busy");
  }
}

document.querySelector(".global-search").addEventListener("click", openSearch);
modal.querySelector(".icon-button").addEventListener("click", () => modal.close());
globalQuery.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(globalSearch, 170);
});
document.querySelector("#global-search-form").addEventListener("submit", event => {
  event.preventDefault();
  clearTimeout(searchTimer);
  globalSearch();
});
document.addEventListener("keydown", event => {
  if (event.key === "/" && !event.target.matches?.("input, textarea, select, [contenteditable]") && !modal.open) {
    event.preventDefault();
    openSearch();
  }
});

window.addEventListener("popstate", () => render());
window.addEventListener("pageshow", event => { if (event.persisted) reloadIfUpdated(); });
window.addEventListener("focus", reloadIfUpdated);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") reloadIfUpdated();
});

document.addEventListener("click", event => {
  if (event.button !== 0 || event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  const anchor = event.target.closest("a[href]");
  if (anchor && !anchor.target && !anchor.getAttribute("href").startsWith("#")) {
    const url = new URL(anchor.href);
    const kind = url.pathname.split("/")[1];
    if (url.origin === location.origin && (sections[kind] || url.pathname === "/")) {
      event.preventDefault();
      visit(url);
    }
    return;
  }
  const button = event.target.closest("button");
  if (!button) return;
  const { sort, sortKey } = button.dataset;
  if (sort && sortKey) {
    if (dialogState && button.closest("#dialogue-lines")) {
      Object.assign(dialogState, { sort, page: 1 });
      loadLines(dialogState, false, sortKey);
    } else {
      visit(filterRoute({ sort, page: null }), { sortKey });
    }
    return;
  }
  const page = button.dataset.browsePage || button.dataset.placementPage || button.dataset.lootPage || button.dataset.npcDropPage;
  if (page) visit(filterRoute({ page }), { page });
  if (button.dataset.dialoguePage && dialogState) {
    dialogState.page = Number(button.dataset.dialoguePage);
    loadLines(dialogState, true);
  }
  if (button.dataset.relatedMore) loadRelated(button);
});

main.addEventListener("submit", event => {
  const form = event.target;
  const values = readFilters(form);
  event.preventDefault();
  if (form.id === "home-search") visit(filterRoute({ q: values.q.trim() }));
  if (form.id === "browse-search" || form.id === "browse-filters") visit(filterRoute({ ...values, page: null }));
  if (form.id === "placement-filter") visit(filterRoute({ scene: values.scene, spawn: values.spawn, q: values.q.trim(), page: null }));
  if (form.id === "npc-drop-filter" || form.id === "item-loot-filter") visit(filterRoute({ q: values.q.trim(), page: null }));
  if (form.id === "line-filters" && dialogState) {
    Object.assign(dialogState, { query: values.q, filter: values.status, page: 1 });
    loadLines(dialogState);
  }
});

main.addEventListener("change", event => {
  const selection = event.target;
  if (selection.id === "map-scene") showMap(current, selection.value);
  if (["scene", "spawn"].includes(selection.name) && selection.form?.id === "placement-filter") selection.form.requestSubmit();
  if (selection.id === "model-choice") visit(filterRoute({ variant: selection.value }));
  if (selection.id === "linked-dialogue" && dialogState) {
    Object.assign(dialogState, { table: selection.value, query: "", filter: "", page: 1 });
    loadLines(dialogState);
  }
});

installClipboard();
render();
