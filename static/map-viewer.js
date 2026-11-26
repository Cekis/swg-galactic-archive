import { boundsValue, coordinateValue, copyControl, getJSON, href, integer, loadingIndicator, readable, sourceControl, spawnDetails, spawnSummary } from "./ui.js";

const MAX_ZOOM = 1024;
const LAYER_KEY = "galactic-archive-map-layers-v4";
const AREA_STYLE = {
  buildout_bounds: { stroke: "#f8d78ca6", fill: "#f8d78c06", dash: [2, 4] },
  region_bounds: { stroke: "#9bf3f1cd", fill: "#72d8de08", dash: [] },
  no_build: { stroke: "#ffb3a2e8", fill: "#ff8e7010", dash: [6, 4] },
  building_filler: { stroke: "#c9d5dfd9", fill: "#53607980", dash: [] },
  building_enterable: { stroke: "#8cf3c4", fill: "#186b50a0", dash: [] },
};
const MAX_BUILDING_METERS_PER_PIXEL = 8;
let mapSequence = 0;

function el(tag, options = {}) {
  return Object.assign(document.createElement(tag), options);
}

function button(label, text = label, className = "") {
  const control = el("button", { type: "button", textContent: text, className, title: label });
  control.setAttribute("aria-label", label);
  return control;
}

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, n));
}

function rect(metadata) {
  return metadata.image_rect || [0, 0, 1, 1];
}

function coordinates(metadata, x, z) {
  const [xmin, zmin, xmax, zmax] = metadata.bounds;
  const [left, top, right, bottom] = rect(metadata);
  return [left + ((x - xmin) / (xmax - xmin)) * (right - left),
    top + ((zmax - z) / (zmax - zmin)) * (bottom - top)];
}

export function projectMapPoint(metadata, point) {
  if (!metadata?.bounds || !point) return null;
  const [xmin, zmin, xmax, zmax] = metadata.bounds;
  const [x, , z] = point;
  if (![xmin, zmin, xmax, zmax, x, z].every(Number.isFinite) ||
      x < xmin || x > xmax || z < zmin || z > zmax || xmin >= xmax || zmin >= zmax) return null;
  const projected = coordinates(metadata, x, z);
  return projected.every(n => n >= 0 && n <= 1) ? projected : null;
}

function metersText(point) {
  const format = n => Number.isFinite(n) ? new Intl.NumberFormat("en", { maximumFractionDigits: 2 }).format(n) : "—";
  return ["X", "Y", "Z"].map((axis, i) => axis + " " + format(point[i])).join(" · ");
}

function layerPrefs(definitions) {
  try {
    const saved = JSON.parse(localStorage.getItem(LAYER_KEY));
    if (Array.isArray(saved)) return new Set(saved.filter(key => definitions[key]));
    const buildings = ["building_enterable", "building_filler"];
    const v3 = JSON.parse(localStorage.getItem("galactic-archive-map-layers-v3"));
    if (Array.isArray(v3)) return new Set([...v3, ...buildings].filter(key => definitions[key]));
    const v2 = JSON.parse(localStorage.getItem("galactic-archive-map-layers-v2"));
    if (Array.isArray(v2)) return new Set([...v2, "region_bounds", "no_build", ...buildings].filter(key => definitions[key]));
    const previous = JSON.parse(localStorage.getItem("galactic-archive-map-layers"));
    if (Array.isArray(previous)) return new Set([...previous, "city", "region_bounds", "no_build", ...buildings].filter(key => definitions[key]));
  } catch {
    // Layer switches still work when storage is unavailable.
  }
  return new Set(Object.keys(definitions).filter(key => definitions[key].default));
}

function savedGrid() {
  try {
    const value = localStorage.getItem("galactic-archive-coordinate-grid");
    return value === null ? null : value === "on";
  } catch {
    return null;
  }
}

function niceSpacing(meters, maximum = false) {
  const step = 10 ** Math.floor(Math.log10(meters));
  return (maximum ? [5, 2, 1].find(n => n * step <= meters) || 1 :
    [1, 2, 5, 10].find(n => n * step >= meters) || 10) * step;
}

function boundaryOf(location) {
  const circle = location?.boundary?.shape === "circle" && location.boundary.values;
  if (circle) {
    const [x, z, radius] = circle;
    return { bounds: [x - radius, z - radius, x + radius, z + radius], circle: true };
  }
  const bounds = location?.boundary?.values || (location?.id !== location?.scene && location?.bounds);
  return bounds?.length === 4 ? { bounds } : null;
}

function markerName(marker) {
  return marker.count ? integer(marker.count) + " map features" : marker.name || marker.label || "Map placement";
}

function markerDescription(marker, definitions) {
  const description = marker.count ? "Select to list individual markers." :
    [marker.context || definitions[marker.layer]?.label, marker.no_build && "No-build zone",
      marker.cell, marker.required_event && "Event " + readable(marker.required_event)]
      .filter(Boolean).join(" · ");
  return el("p", { className: "atlas-map-point-info", textContent: description });
}

function markerPrecision(marker) {
  const label = marker.status === "spawn_area" ? "Spawn center" :
    ["region_center", "authored_extent", "field_center"].includes(marker.status) ? "Mapped center" : "";
  return [label, marker.radius > 0 && "Radius " + integer(marker.radius) + " m"].filter(Boolean).join(" · ");
}

function sourceLine(source) {
  return sourceControl(source, "atlas-map-proof");
}

function copyPoint(point, style = "coordinate") {
  if (point[1] == null) {
    const [x, , z] = point;
    return copyControl("X " + integer(x) + " · Z " + integer(z),
      coordinateValue([x, z], ["X", "Z"]), "Copy world coordinates", style);
  }
  return copyControl(metersText(point), coordinateValue(point), "Copy world coordinates", style);
}

function extentControl(extent) {
  const [xmin, zmin, xmax, zmax] = extent.bounds;
  const display = "X " + integer(xmin) + " to " + integer(xmax) + " · Z " + integer(zmin) + " to " + integer(zmax);
  if (extent.shape === "circle") {
    const [x, z, radius] = extent.values;
    const center = coordinateValue([x, z], ["X", "Z"]);
    return copyControl("Radius " + integer(radius) + " m · " + display,
      "Center " + center + " · Radius " + radius + " m · " + boundsValue(extent.bounds),
      "Copy area extent", "coordinate atlas-map-extent");
  }
  return copyControl(display, boundsValue(extent.bounds), "Copy area extent", "coordinate atlas-map-extent");
}

function extentDistance(extent, x, z) {
  if (extent.shape === "circle") {
    const [centerX, centerZ, radius] = extent.values;
    return Math.hypot(x - centerX, z - centerZ) - radius;
  }
  const [left, bottom, right, top] = extent.bounds;
  return Math.max(left - x, x - right, bottom - z, z - top);
}

function extentSize({ extent: { bounds: [left, bottom, right, top] } }) {
  return (right - left) * (top - bottom);
}

function polygonContains(paths, x, z) {
  return paths.some(path => {
    let inside = false;
    for (let i = 0, j = path.length - 1; i < path.length; j = i++) {
      const [ax, az] = path[j], [bx, bz] = path[i];
      if ((az > z) !== (bz > z) && x < ax + (z - az) * (bx - ax) / (bz - az)) inside = !inside;
    }
    return inside;
  });
}

export async function mountMap(container, { scene, charts, preferred, markers = [], location, chartLabel, definitions, focusMarker, signal }) {
  const sceneChart = charts[scene];
  if (!sceneChart?.bounds) {
    container.textContent = "Map coordinates are unavailable for this location.";
    return;
  }
  const interactiveLayers = Boolean(definitions);
  const prefix = "atlas-map-" + (++mapSequence);
  let chart = charts[preferred] || sceneChart;
  if (focusMarker && !projectMapPoint(chart, focusMarker.coordinates) && !chart.grid_bounds) chart = sceneChart;
  const homeChart = chart.id;
  const home = boundaryOf(location);
  const focus = focusMarker?.coordinates || location?.marker?.coordinates || markers[0]?.coordinates;
  let fullExtent = Boolean(chart.grid_bounds && (!chart.image || (focus && !projectMapPoint(chart, focus))));
  let gridPreference = savedGrid();
  let zoom = 1;
  let fitted = false;
  let center = [0.5, 0.5];
  let frame;
  let pinned = null;
  let hovering = null;
  let fetchController;
  let detailController;
  let pending;
  let hoverTimer;
  let hoverOpenTimer;
  let raf;
  let pins = [];
  let areas = [];
  let areaPress = null;
  let filter = "";
  const activeLayers = interactiveLayers ? layerPrefs(definitions) : new Set();
  const pointers = new Map();

  const root = el("section", { className: interactiveLayers ? "atlas-map" : "atlas-map atlas-map-compact" });
  const toolbar = el("div", { className: "atlas-map-toolbar" });
  const title = el("span", { className: "atlas-map-toolbar-title", textContent: chartLabel || readable(scene) });
  const regionPicker = el("select");
  regionPicker.setAttribute("aria-label", "Map area");
  regionPicker.append(new Option("Scene overview", sceneChart.id));
  for (const id of sceneChart.regions || []) {
    const region = charts[id];
    if (region) regionPicker.append(new Option(readable(region.region || id), id));
  }
  regionPicker.value = chart.id;
  const cityPicker = el("select");
  cityPicker.setAttribute("aria-label", "Go to city");
  cityPicker.append(new Option("Go to city", ""));
  const cities = interactiveLayers ? sceneChart.cities || [] : [];
  for (const city of cities) cityPicker.append(new Option(city.name, String(city.id)));
  const extentPicker = el("select");
  extentPicker.setAttribute("aria-label", "Map extent");
  extentPicker.append(new Option("Chart extent", "chart"), new Option("Full coordinate extent", "full"));
  toolbar.append(title);
  if (cities.length) toolbar.append(cityPicker);
  if ((sceneChart.regions || []).length) toolbar.append(regionPicker);
  toolbar.append(extentPicker);

  const view = el("div", { className: "atlas-map-viewport", tabIndex: 0 });
  view.setAttribute("role", "region");
  view.setAttribute("aria-label", "Interactive game map");
  const help = el("span", { id: prefix + "-help", className: "sr-only",
    textContent: "Drag or use arrow keys to pan. Double-click, pinch, or use plus and minus to zoom. Right-click or press Shift+F10 to copy coordinates or inspect map features." });
  view.setAttribute("aria-describedby", help.id);
  const plane = el("div", { className: "atlas-map-plane" });
  const areaCanvas = el("canvas", { className: "atlas-map-areas", hidden: true });
  areaCanvas.setAttribute("aria-hidden", "true");
  const gridCanvas = el("canvas", { className: "atlas-map-coordinate-grid", hidden: true });
  gridCanvas.setAttribute("aria-hidden", "true");
  const markerLayer = el("div", { className: "atlas-map-marker-layer" });
  const boundary = el("div", { className: "atlas-map-boundary", hidden: true });
  const radius = el("div", { className: "atlas-map-boundary atlas-map-radius", hidden: true });
  const tooltip = el("div", { className: "atlas-map-tooltip", hidden: true });
  const tooltipText = el("div", { id: prefix + "-tooltip" });
  tooltipText.setAttribute("role", "tooltip");
  const popup = el("div", { className: "atlas-map-popup", hidden: true });
  popup.setAttribute("role", "dialog");

  const mapTools = el("div", { className: "atlas-map-camera" });
  const plus = button("Zoom in", "+");
  const minus = button("Zoom out", "−");
  const reset = button("Reset map view", "⌖");
  const fit = button("Fit entire chart", "▣");
  const expand = button("Fullscreen map", "⛶", "atlas-map-expand");
  const compass = el("span", { className: "atlas-map-compass", textContent: "N ↑" });
  compass.setAttribute("aria-hidden", "true");
  mapTools.append(plus, minus, reset, fit);
  const positionReadout = copyControl("X / Z", "", "Copy cursor coordinates", "atlas-map-position");
  positionReadout.disabled = true;
  const scale = el("span", { className: "atlas-map-scale" });
  const status = el("div", { className: "atlas-map-status", role: "status" });
  const imageLoading = el("div", { className: "atlas-map-image-loading", role: "status", hidden: true });
  imageLoading.innerHTML = loadingIndicator("Loading game map…");
  const summary = el("p", { className: "atlas-map-caption" });
  const filters = el("div", { className: "atlas-map-layers" });
  const layerButton = button("Map layers", "Layers");
  const layerPanel = el("div", { className: "atlas-map-layer-panel", id: prefix + "-layers", hidden: true });
  layerButton.setAttribute("aria-expanded", "false");
  layerButton.setAttribute("aria-controls", layerPanel.id);
  const search = el("input", { type: "search", placeholder: "Filter by name", autocomplete: "off" });
  search.setAttribute("aria-label", "Filter map features");
  const all = button("Show all map layers", "Show all");
  const clear = button("Clear map layers", "Clear");
  const layerActions = el("div", { className: "atlas-map-layer-actions" });
  layerActions.append(all, clear);
  const layerChoices = el("fieldset");
  const buildingHint = el("p", { className: "atlas-map-building-hint", textContent: "Zoom in to see building outlines.", hidden: true });
  const gridToggle = el("input", { type: "checkbox" });
  const gridRow = el("label", { className: "atlas-map-layer-row atlas-map-grid-choice" });
  const gridDot = el("span", { className: "atlas-map-layer-dot", textContent: "#" });
  gridDot.dataset.layer = "grid";
  gridDot.setAttribute("aria-hidden", "true");
  gridRow.append(gridToggle, gridDot, el("span", { textContent: "Coordinate grid" }));
  layerPanel.append(search, layerActions, gridRow, layerChoices, buildingHint);
  filters.append(layerButton, layerPanel);

  plane.append(boundary, radius);
  view.append(plane, areaCanvas, gridCanvas, markerLayer, tooltip, popup, compass, mapTools, expand, scale, positionReadout, status, imageLoading, help);
  if (interactiveLayers) view.append(filters);
  root.append(toolbar, view, summary);
  container.replaceChildren(root);

  function projection() {
    return fullExtent && chart.grid_bounds ? { ...chart, bounds: chart.grid_bounds, image_rect: [0, 0, 1, 1] } : chart;
  }

  function gridShown() {
    return gridPreference ?? !chart.image;
  }

  function updateLayerCaption() {
    layerButton.textContent = "Layers · " + (shownLayers().length + Number(gridShown()));
    buildingHint.hidden = frame?.metersPerPixel <= MAX_BUILDING_METERS_PER_PIXEL ||
      !shownLayers().some(layer => definitions[layer].close_zoom);
  }

  function layerCounts() {
    return fullExtent && chart.grid_layers ? { ...chart.grid_layers, ...chart.grid_area_layers } :
      { ...chart.layers, ...chart.area_layers };
  }

  function shownLayers() {
    const counts = layerCounts();
    return Object.keys(definitions || {}).filter(layer => counts[layer] && activeLayers.has(layer));
  }

  function drawLayers() {
    if (!interactiveLayers) return;
    const counts = layerCounts();
    layerChoices.replaceChildren(el("legend", { className: "sr-only", textContent: "Map feature categories" }));
    for (const [key, layer] of Object.entries(definitions)) {
      if (!counts?.[key]) continue;
      const checkbox = el("input", { type: "checkbox", value: key, checked: activeLayers.has(key) });
      const row = el("label", { className: "atlas-map-layer-row" });
      const dot = el("span", { className: "atlas-map-layer-dot", textContent: layer.symbol });
      dot.dataset.layer = key;
      dot.setAttribute("aria-hidden", "true");
      row.append(checkbox, dot, el("span", { textContent: layer.label }), el("small", { textContent: integer(counts[key]) }));
      layerChoices.append(row);
    }
    gridToggle.checked = gridShown();
    updateLayerCaption();
  }

  function setGrid(show) {
    gridPreference = show;
    gridToggle.checked = show;
    try {
      localStorage.setItem("galactic-archive-coordinate-grid", show ? "on" : "off");
    } catch {
      // The current toggle continues to work without storage.
    }
    updateLayerCaption();
    paintSoon();
  }

  function updateFilters() {
    try {
      localStorage.setItem(LAYER_KEY, JSON.stringify([...activeLayers]));
    } catch {
      // The active choice can remain in memory for this map.
    }
    closeSelection(false);
    updateLayerCaption();
    loadSoon(0);
  }

  function showChart() {
    areas = [];
    plane.querySelector("img")?.remove();
    imageLoading.hidden = true;
    extentPicker.hidden = !chart.grid_bounds;
    extentPicker.value = fullExtent ? "full" : "chart";
    if (chart.image) {
      const image = el("img", { className: "atlas-map-art", alt: "", src: chart.image, draggable: false });
      imageLoading.hidden = Boolean(image.complete && image.naturalWidth);
      image.addEventListener("load", () => {
        if (image.parentNode === plane && !signal?.aborted) imageLoading.hidden = true;
      }, { once: true });
      if (fullExtent && chart.grid_bounds) {
        const [xmin, zmin, xmax, zmax] = chart.bounds;
        const [left, top, right, bottom] = rect(chart);
        const width = (xmax - xmin) / (right - left);
        const height = (zmax - zmin) / (bottom - top);
        const [x, y] = coordinates(projection(), xmin - left * width, zmax + top * height);
        const [r, b] = coordinates(projection(), xmin + (1 - left) * width, zmax - (1 - top) * height);
        Object.assign(image.style, { left: (x * 100) + "%", top: (y * 100) + "%",
          width: ((r - x) * 100) + "%", height: ((b - y) * 100) + "%" });
      }
      image.addEventListener("error", () => {
        if (image.parentNode !== plane || signal?.aborted) return;
        chart = { ...chart, image: null };
        fullExtent = Boolean(chart.grid_bounds);
        showChart();
      }, { once: true });
      plane.prepend(image);
    }
    drawLayers();
    const [xmin, zmin, xmax, zmax] = projection().bounds;
    const display = "X " + integer(xmin) + " to " + integer(xmax) + " m · Z " + integer(zmin) + " to " + integer(zmax) + " m";
    summary.replaceChildren(document.createTextNode((chart.image ? "Original game map" : "World coordinate chart") + " · "),
      copyControl(display, boundsValue([xmin, zmin, xmax, zmax]), "Copy coordinate bounds", "coordinate"),
      el("span", { className: "atlas-map-instruction", textContent:
        (Object.keys(layerCounts()).some(layer => definitions?.[layer]?.close_zoom) ? "Zoom in for buildings. " : "") +
        "Right-click to copy coordinates or inspect a point." }));
    resetCamera();
  }

  function baseSize() {
    const [xmin, zmin, xmax, zmax] = projection().bounds;
    const [left, top, right, bottom] = rect(projection());
    const ratio = (xmax - xmin) / (zmax - zmin) * (bottom - top) / (right - left);
    const width = Math.max(view.clientWidth, view.clientHeight * ratio);
    return [width, width / ratio];
  }

  function fitZoom() {
    const [width, height] = baseSize();
    return width && height ? Math.min(view.clientWidth / width, view.clientHeight / height, 1) : 1;
  }

  function screenPoint(point) {
    if (!frame) return null;
    const relative = projectMapPoint(projection(), point);
    return relative ? [frame.x + relative[0] * frame.w, frame.y + relative[1] * frame.h] : null;
  }

  function unboundedScreenPoint(x, z) {
    const [left, top] = coordinates(projection(), x, z);
    return [frame.x + left * frame.w, frame.y + top * frame.h];
  }

  function worldAt(x, y) {
    const info = projection();
    const [xmin, zmin, xmax, zmax] = info.bounds;
    const [left, top, right, bottom] = rect(info);
    const u = ((x - frame.x) / frame.w - left) / (right - left);
    const v = ((y - frame.y) / frame.h - top) / (bottom - top);
    return [xmin + u * (xmax - xmin), zmax - v * (zmax - zmin)];
  }

  function visibleBounds() {
    const [xmin, zmin, xmax, zmax] = projection().bounds;
    const minimum = worldAt(Math.max(0, frame.x), Math.min(view.clientHeight, frame.y + frame.h));
    const maximum = worldAt(Math.min(view.clientWidth, frame.x + frame.w), Math.max(0, frame.y));
    return [Math.max(xmin, minimum[0]), Math.max(zmin, minimum[1]),
      Math.min(xmax, maximum[0]), Math.min(zmax, maximum[1])];
  }

  function highlight(node, shape) {
    node.hidden = !shape;
    if (!shape) return;
    const [xmin, zmin, xmax, zmax] = shape.bounds;
    const [left, top] = coordinates(projection(), xmin, zmax);
    const [right, bottom] = coordinates(projection(), xmax, zmin);
    Object.assign(node.style, { left: (left * 100) + "%", top: (top * 100) + "%",
      width: ((right - left) * 100) + "%", height: ((bottom - top) * 100) + "%" });
    node.classList.toggle("atlas-map-circle", Boolean(shape.circle));
  }

  function overlayContext(canvas) {
    const width = view.clientWidth, height = view.clientHeight;
    const density = Math.min(devicePixelRatio || 1, 2);
    const pixelWidth = Math.round(width * density), pixelHeight = Math.round(height * density);
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    const context = canvas.getContext("2d");
    context?.setTransform(density, 0, 0, density, 0, 0);
    context?.clearRect(0, 0, width, height);
    return context;
  }

  function drawAreas() {
    const outline = pinned?.feature?.extent?.shape === "polygon" ? pinned.feature : null;
    areaCanvas.hidden = !areas.length && !outline;
    if (areaCanvas.hidden) return;
    const ctx = overlayContext(areaCanvas);
    if (!ctx) return;
    const [xmin, zmin, xmax, zmax] = projection().bounds;
    const [left, top] = unboundedScreenPoint(xmin, zmax);
    const [right, bottom] = unboundedScreenPoint(xmax, zmin);
    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, right - left, bottom - top);
    ctx.clip();
    ctx.lineWidth = 1.4;
    for (const feature of outline ? [...areas, outline] : areas) {
      const { extent, layer } = feature;
      const selected = feature === outline;
      if (!selected && definitions[layer].close_zoom && frame.metersPerPixel > MAX_BUILDING_METERS_PER_PIXEL) continue;
      const [west, south, east, north] = extent.bounds;
      const [x, y] = unboundedScreenPoint(west, north);
      const [r, b] = unboundedScreenPoint(east, south);
      const { stroke, fill, dash } = selected ?
        { stroke: "#ffe0a0", fill: "#ffd5824b", dash: [] } : AREA_STYLE[layer];
      ctx.lineWidth = selected ? 2.5 : 1.4;
      ctx.beginPath();
      if (extent.shape === "polygon") {
        for (const path of extent.paths) {
          ctx.moveTo(...unboundedScreenPoint(...path[0]));
          for (const point of path.slice(1)) ctx.lineTo(...unboundedScreenPoint(...point));
          ctx.closePath();
        }
      } else if (extent.shape === "circle") ctx.ellipse((x + r) / 2, (y + b) / 2, (r - x) / 2, (b - y) / 2, 0, 0, 2 * Math.PI);
      else ctx.rect(x, y, r - x, b - y);
      ctx.setLineDash(dash);
      ctx.strokeStyle = stroke;
      ctx.fillStyle = fill;
      ctx.fill();
      if (extent.edges) {
        ctx.beginPath();
        for (const [start, end] of extent.edges) {
          ctx.moveTo(...unboundedScreenPoint(...start));
          ctx.lineTo(...unboundedScreenPoint(...end));
        }
      }
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawGrid(metersPerPixel) {
    gridCanvas.hidden = !gridShown();
    if (gridCanvas.hidden) return;
    const ctx = overlayContext(gridCanvas);
    if (!ctx) return;
    const width = view.clientWidth, height = view.clientHeight;
    const [xmin, zmin, xmax, zmax] = visibleBounds();
    const areaLeft = Math.max(0, frame.x), areaRight = Math.min(width, frame.x + frame.w);
    const areaTop = Math.max(0, frame.y), areaBottom = Math.min(height, frame.y + frame.h);
    const step = niceSpacing(metersPerPixel * 86);
    const labels = [];
    ctx.beginPath();
    for (let x = Math.ceil(xmin / step) * step; x <= xmax; x += step) {
      const u = coordinates(projection(), x, zmax)[0] * frame.w + frame.x;
      ctx.moveTo(u, areaTop);
      ctx.lineTo(u, areaBottom);
      if (u < areaRight - 51) labels.push(["X " + integer(x), u + 4, areaTop + 14]);
    }
    for (let z = Math.ceil(zmin / step) * step; z <= zmax; z += step) {
      const v = coordinates(projection(), xmin, z)[1] * frame.h + frame.y;
      ctx.moveTo(areaLeft, v);
      ctx.lineTo(areaRight, v);
      if (v > areaTop + 27) labels.push(["Z " + integer(z), areaLeft + 5, v - 4]);
    }
    ctx.strokeStyle = "#b6d9ea8c";
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.font = "600 10px system-ui, sans-serif";
    ctx.lineJoin = "round";
    ctx.strokeStyle = "#0b1825";
    ctx.lineWidth = 3;
    ctx.fillStyle = "#d6edf5";
    for (const [label, x, y] of labels) {
      ctx.strokeText(label, x, y);
      ctx.fillText(label, x, y);
    }
  }

  function layout() {
    const [baseWidth, baseHeight] = baseSize();
    if (!baseWidth || !baseHeight) return;
    const w = baseWidth * zoom, h = baseHeight * zoom;
    const halfX = Math.min(.5, view.clientWidth / (2 * w));
    const halfY = Math.min(.5, view.clientHeight / (2 * h));
    center = [clamp(center[0], halfX, 1 - halfX), clamp(center[1], halfY, 1 - halfY)];
    frame = { x: view.clientWidth / 2 - center[0] * w, y: view.clientHeight / 2 - center[1] * h, w, h };
    Object.assign(plane.style, { width: w + "px", height: h + "px",
      transform: `translate(${frame.x}px, ${frame.y}px)` });
    plus.disabled = zoom >= MAX_ZOOM;
    minus.disabled = zoom <= fitZoom() + 1e-8;
    const [xmin, , xmax] = projection().bounds;
    const [left, , right] = rect(projection());
    const metersPerPixel = (xmax - xmin) / (w * (right - left));
    frame.metersPerPixel = metersPerPixel;
    const length = niceSpacing(metersPerPixel * 80, true);
    scale.style.width = (length / metersPerPixel) + "px";
    scale.textContent = length >= 1000 ? (length / 1000) + " km" : integer(length) + " m";
    for (const { node, marker } of pins) {
      const point = screenPoint(marker.coordinates);
      node.hidden = !point || point[0] < -4 || point[1] < -4 || point[0] > view.clientWidth + 4 || point[1] > view.clientHeight + 4;
      if (!node.hidden) {
        node.style.transform = `translate(${point[0]}px, ${point[1]}px) translate(-50%, -50%)`;
        if (marker.layer === "city") node.classList.toggle("atlas-map-city-left", point[0] > view.clientWidth - 128);
      }
    }
    drawAreas();
    drawGrid(metersPerPixel);
    updateLayerCaption();
    if (hovering) placeOverlay(tooltip, hovering.anchor);
    if (pinned) placeOverlay(popup, pinned.anchor);
  }

  function paintSoon() {
    if (!raf) raf = requestAnimationFrame(() => { raf = 0; layout(); });
  }

  function centerOn(bounds, fit = .77) {
    const [x1, z1, x2, z2] = bounds;
    const [u1, v1] = coordinates(projection(), x1, z2);
    const [u2, v2] = coordinates(projection(), x2, z1);
    const [baseWidth, baseHeight] = baseSize();
    center = [(u1 + u2) / 2, (v1 + v2) / 2];
    zoom = clamp(Math.min(view.clientWidth / (baseWidth * (u2 - u1)),
      view.clientHeight / (baseHeight * (v2 - v1))) * fit, 1, MAX_ZOOM);
    fitted = false;
  }

  function resetCamera() {
    closeSelection(false);
    center = [.5, .5];
    zoom = 1;
    fitted = false;
    const selected = chart.id === homeChart && focusMarker && projectMapPoint(projection(), focus);
    const shape = chart.id === homeChart && !selected && home;
    if (selected && focusMarker.extent) {
      const [west, south, east, north] = focusMarker.extent.bounds;
      const [x, , z] = focus;
      centerOn([Math.min(west, x), Math.min(south, z), Math.max(east, x), Math.max(north, z)], .62);
    }
    else if (shape) centerOn(shape.bounds);
    else if (chart.id === homeChart && focus && projectMapPoint(projection(), focus)) {
      const [x, , z] = focus;
      const span = scene.startsWith("space_") ? 500 : selected ? Math.max(85, focusMarker.radius || 0) : 225;
      centerOn([x - span, z - span, x + span, z + span]);
    }
    highlight(boundary, shape);
    layout();
    if (selected) {
      renderPins(markers);
      openPopup(focusMarker, view, false);
    }
    loadSoon(0);
  }

  function fitChart() {
    closeSelection(false);
    fitted = true;
    zoom = fitZoom();
    center = [.5, .5];
    layout();
    loadSoon(0);
  }

  function moveCamera(dx, dy) {
    if (!frame) return;
    center = [center[0] - dx / frame.w, center[1] - dy / frame.h];
    paintSoon();
    loadSoon();
  }

  function zoomAt(factor, x = view.clientWidth / 2, y = view.clientHeight / 2) {
    if (!frame) return;
    const former = zoom;
    zoom = clamp(zoom * factor, fitZoom(), MAX_ZOOM);
    fitted = zoom <= fitZoom() + 1e-8;
    const difference = 1 - former / zoom;
    center = [center[0] + ((x - view.clientWidth / 2) / frame.w) * difference,
      center[1] + ((y - view.clientHeight / 2) / frame.h) * difference];
    paintSoon();
    loadSoon();
  }

  function placeOverlay(node, point) {
    const anchor = screenPoint(point);
    if (!anchor || anchor[0] < 0 || anchor[1] < 0 || anchor[0] > view.clientWidth || anchor[1] > view.clientHeight) {
      node.classList.add("atlas-map-offscreen");
      return;
    }
    node.classList.remove("atlas-map-offscreen");
    const width = node.offsetWidth;
    const height = node.offsetHeight;
    const left = clamp(anchor[0] - width / 2, 8, Math.max(8, view.clientWidth - width - 8));
    const top = anchor[1] - height - 18 >= 8 ? anchor[1] - height - 18 :
      clamp(anchor[1] + 17, 8, Math.max(8, view.clientHeight - height - 8));
    node.style.transform = `translate(${left}px, ${top}px)`;
  }

  function showTooltip(marker, node, anchor = marker.coordinates) {
    if (pinned) return;
    hideTooltip();
    hovering = { marker, node, anchor };
    node?.setAttribute("aria-describedby", tooltipText.id);
    tooltipText.replaceChildren(el("strong", { textContent: markerName(marker) }), markerDescription(marker, definitions || {}));
    if (marker.spawner) tooltipText.append(el("p", { textContent: spawnSummary(marker.spawner) }));
    if (marker.description) tooltipText.append(el("p", { className: "atlas-map-feature-description", textContent: marker.description }));
    tooltip.replaceChildren(tooltipText);
    if (marker.extent) tooltip.append(extentControl(marker.extent));
    else if (!marker.count) tooltip.append(copyPoint(marker.coordinates));
    if (marker.enterable && marker.target) {
      tooltip.append(el("a", { href: href(...marker.target), textContent: "View building details" }));
    }
    tooltip.hidden = false;
    placeOverlay(tooltip, anchor);
  }

  function hideTooltip() {
    clearTimeout(hoverTimer);
    clearTimeout(hoverOpenTimer);
    hoverTimer = null;
    hovering?.node?.removeAttribute("aria-describedby");
    hovering = null;
    tooltip.hidden = true;
  }

  function queueTooltipClose() {
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(hideTooltip, 320);
  }

  function hoverMarker(marker, node) {
    clearTimeout(hoverOpenTimer);
    const focus = document.activeElement;
    if (tooltip.contains(focus) || (hovering?.node === focus && focus !== node)) return;
    if (hovering && hovering.node !== node) hoverOpenTimer = setTimeout(() => showTooltip(marker, node), 150);
    else showTooltip(marker, node);
  }

  function closeSelection(refocus = true) {
    const original = pinned?.node;
    if (pinned?.feature?.extent?.shape === "polygon") paintSoon();
    detailController?.abort();
    pinned = null;
    popup.hidden = true;
    highlight(radius, null);
    if (refocus && original) (original.isConnected ? original : view).focus({ preventScroll: true });
  }

  function beginPopup(label, anchor, node) {
    hideTooltip();
    closeSelection(false);
    pinned = { anchor, node };
    const caption = el("h3", { textContent: label });
    const close = button("Close map details", "×", "atlas-map-dismiss");
    const head = el("div", { className: "atlas-map-popup-heading" });
    head.append(caption, close);
    popup.replaceChildren(head);
    popup.setAttribute("aria-label", label);
    popup.hidden = false;
    close.addEventListener("click", () => closeSelection());
    return close;
  }

  function openPopup(marker, node, openByKeyboard, anchor = marker.coordinates, back) {
    const close = beginPopup(markerName(marker), anchor, node);
    pinned.feature = marker;
    if (back) {
      const previous = button("Back to map point", "← Map point", "atlas-map-back");
      previous.addEventListener("click", back);
      popup.append(previous);
    }
    if (marker.count) {
      const direction = button("Zoom to these markers", "Zoom to these markers", "atlas-map-zoom-group");
      direction.addEventListener("click", () => {
        const [x1, z1, x2, z2] = marker.bounds;
        if (Math.max(x2 - x1, z2 - z1) < 2) return;
        closeSelection(false);
        centerOn(marker.bounds);
        layout();
        loadSoon(0);
        view.focus({ preventScroll: true });
      });
      direction.disabled = Math.max(marker.bounds[2] - marker.bounds[0], marker.bounds[3] - marker.bounds[1]) < 2;
      const list = el("div", { className: "atlas-map-cluster-list" });
      popup.append(direction, list);
      loadCluster(marker, node, list, 1);
    } else {
      const description = markerDescription(marker, definitions || {});
      popup.append(description, copyPoint(marker.coordinates, "coordinate atlas-map-coordinate"));
      if (marker.extent) {
        const extent = el("div", { className: "atlas-map-extent-row" });
        extent.append(el("small", { textContent: "Extent" }), extentControl(marker.extent));
        popup.append(extent);
        if (marker.extent.shape === "polygon") drawAreas();
        else highlight(radius, { bounds: marker.extent.bounds, circle: marker.extent.shape === "circle" });
      }
      if (markerPrecision(marker)) popup.append(el("p", { textContent: markerPrecision(marker) }));
      const featureDescription = marker.description || marker.summary;
      if (featureDescription) popup.append(el("p", { className: "atlas-map-feature-description", textContent: featureDescription }));
      if (marker.spawner) {
        const content = document.createElement("template");
        content.innerHTML = spawnDetails(marker.spawner);
        popup.append(content.content);
      }
      if (marker.object_id) popup.append(el("small", { textContent: "Object ID " + marker.object_id }));
      if (marker.template) popup.append(copyControl(marker.template, marker.template, "Copy file reference", "atlas-map-proof copy-code"));
      if (marker.route?.length) {
        const waypoints = el("details");
        waypoints.append(el("summary", { textContent: "Field path coordinates" }));
        const positions = el("ol");
        for (const point of marker.route) {
          const row = el("li");
          row.append(copyPoint(point));
          positions.append(row);
        }
        waypoints.append(positions);
        popup.append(waypoints);
      }
      if (marker.target) {
        const [kind, ident] = marker.target;
        popup.append(el("a", { href: href(kind, ident), textContent: marker.enterable ? "View building details" :
          "Open " + ({ quests: "quest", npcs: "NPC", items: "item", locations: "location" }[kind] || "entry") }));
      }
      if (marker.geometry_source) popup.append(el("small", { textContent: marker.geometry_kind }), sourceLine(marker.geometry_source));
      const proof = sourceLine(marker.source);
      if (proof) popup.append(proof);
      const transform = sourceLine(marker.parent_source);
      if (transform) popup.append(el("small", { textContent: "Room transform" }), transform);
      if (marker.radius > 0) {
        const [x, , z] = marker.coordinates;
        const r = marker.radius;
        highlight(radius, { bounds: [x - r, z - r, x + r, z + r], circle: true });
      }
    }
    placeOverlay(popup, anchor);
    if (openByKeyboard) close.focus({ preventScroll: true });
  }

  function areasAt(point, edgeOnly = false) {
    const [x, z] = worldAt(...point);
    const [xmin, , xmax] = projection().bounds;
    const [left, , right] = rect(projection());
    const tolerance = 6 * (xmax - xmin) / (frame.w * (right - left));
    return areas.filter(area => {
      if (area.extent.shape === "polygon") {
        return frame.metersPerPixel <= MAX_BUILDING_METERS_PER_PIXEL &&
          extentDistance(area.extent, x, z) <= 0 && polygonContains(area.extent.paths, x, z);
      }
      const distance = extentDistance(area.extent, x, z);
      return (edgeOnly ? Math.abs(distance) : distance) <= tolerance;
    }).sort((first, second) => extentSize(first) - extentSize(second));
  }

  function openPointMenu(point, picked, node = view) {
    const world = worldAt(...point).map(value => Math.round(value * 100) / 100);
    const anchor = [world[0], null, world[1]];
    if (!screenPoint(anchor)) return false;
    beginPopup("Map point", anchor, node);
    const copy = copyControl("Copy coordinates · X " + integer(world[0]) + " · Z " + integer(world[1]),
      coordinateValue(world, ["X", "Z"]), "Copy coordinates", "coordinate atlas-map-context-copy");
    popup.append(copy);
    const entries = [];
    const seen = new Set();
    for (const item of [...picked, ...areasAt(point)]) {
      const key = item.extent && item.target?.[0] === "locations" ? item.target[1] : item.id;
      if (seen.has(key)) continue;
      seen.add(key);
      entries.push(item);
    }
    if (entries.length) {
      const list = el("ul", { className: "atlas-map-context-list" });
      list.setAttribute("aria-label", "Features at this point");
      const names = new Map();
      for (const item of entries) {
        const label = markerName(item);
        names.set(label, (names.get(label) || 0) + 1);
      }
      for (const item of entries) {
        const entry = el("li");
        const name = markerName(item);
        const description = item.spawner ? spawnSummary(item.spawner) :
          item.no_build ? "No-build zone" : item.context || definitions?.[item.layer]?.label;
        const record = names.get(name) > 1 && (item.source?.line || item.source?.row);
        const action = button(record ? name + " · Source row " + record : name, name);
        if (description && !item.count) action.append(el("small", { textContent: description }));
        if (record) action.append(el("small", { textContent: "Row " + record }));
        action.addEventListener("click", event => openPopup(item, node, event.detail === 0, anchor,
          () => openPointMenu(screenPoint(anchor) || point, picked, node)));
        entry.append(action);
        list.append(entry);
      }
      popup.append(list);
    }
    placeOverlay(popup, anchor);
    copy.focus({ preventScroll: true });
    return true;
  }

  async function loadCluster(marker, anchor, host, page) {
    detailController?.abort();
    const request = detailController = new AbortController();
    host.textContent = "Loading markers…";
    try {
      const result = await queryMarkers(request.signal, marker.bounds, { list: 1, page });
      if (request.signal.aborted || !host.isConnected || signal?.aborted) return;
      const list = el("ul");
      for (const entry of result.rows) {
        const row = el("li");
        const pin = button(markerName(entry) + ", " + metersText(entry.coordinates), markerName(entry));
        pin.addEventListener("click", event => openPopup(entry, anchor, event.detail === 0));
        row.append(pin, copyPoint(entry.coordinates));
        list.append(row);
      }
      host.replaceChildren(list);
      if (result.total > 20) {
        const paging = el("div", { className: "atlas-map-cluster-paging" });
        const prev = button("Previous markers", "Previous");
        const next = button("Next markers", "Next");
        prev.disabled = page === 1;
        next.disabled = page * 20 >= result.total;
        prev.addEventListener("click", () => loadCluster(marker, anchor, host, page - 1));
        next.addEventListener("click", () => loadCluster(marker, anchor, host, page + 1));
        paging.append(prev, el("small", { textContent: "Page " + page + " of " + Math.ceil(result.total / 20) }), next);
        host.append(paging);
      }
      if (pinned) placeOverlay(popup, pinned.anchor);
    } catch (error) {
      if (!request.signal.aborted) host.textContent = error.message;
    }
  }

  function renderPins(next) {
    hideTooltip();
    const batch = document.createDocumentFragment();
    const featured = focusMarker ? [...next, focusMarker] : next;
    pins = featured.map(marker => {
      const label = markerName(marker);
      const symbol = marker.count ? integer(marker.count) : definitions?.[marker.layer]?.symbol || "●";
      const node = button(marker.count ? label : label + ", " + metersText(marker.coordinates), symbol,
        "atlas-map-pin" + (marker.count ? " atlas-map-cluster" : ""));
      node.removeAttribute("title");
      node.dataset.layer = marker.layer || "mixed";
      if (marker === focusMarker) node.classList.add("atlas-map-focus-pin");
      if (marker.layer === "city") {
        const city = el("span", { className: "atlas-map-city-label", textContent: label });
        city.setAttribute("aria-hidden", "true");
        node.append(city);
      }
      node.addEventListener("focus", () => showTooltip(marker, node));
      node.addEventListener("blur", queueTooltipClose);
      node.addEventListener("pointerenter", event => {
        if (event.pointerType !== "touch") hoverMarker(marker, node);
      });
      node.addEventListener("pointerleave", () => {
        clearTimeout(hoverOpenTimer);
        if (hovering?.node === node && document.activeElement !== node) queueTooltipClose();
      });
      node.addEventListener("click", event => openPopup(marker, node, event.detail === 0));
      batch.append(node);
      return { node, marker };
    });
    markerLayer.replaceChildren(batch);
    layout();
  }

  function queryMarkers(requestSignal, bounds, extra = {}) {
    const layers = shownLayers();
    const geometry = layers.filter(layer => definitions[layer].area &&
      (!definitions[layer].close_zoom || frame?.metersPerPixel <= MAX_BUILDING_METERS_PER_PIXEL));
    return getJSON("/api/map/" + encodeURIComponent(chart.id) + "/markers", {
      extent: fullExtent ? "full" : "chart", layers: layers.filter(layer => !definitions[layer].area).join(","),
      areas: geometry.join(","), bounds: bounds.join(","),
      q: filter, ...extra,
    }, requestSignal);
  }

  function loadSoon(delay = 160) {
    clearTimeout(pending);
    // An in-flight request describes the old viewport.
    fetchController?.abort();
    pending = setTimeout(loadPins, delay);
  }

  async function loadPins() {
    if (signal?.aborted || !frame) return;
    if (!interactiveLayers) {
      renderPins(markers);
      return;
    }
    const layers = shownLayers();
    if (!layers.length) {
      areas = [];
      renderPins(markers);
      status.textContent = "No layers selected";
      return;
    }
    const request = fetchController = new AbortController();
    try {
      const result = await queryMarkers(request.signal, visibleBounds(), {
        columns: clamp(Math.floor(view.clientWidth / 42), 1, 28),
        rows: clamp(Math.floor(view.clientHeight / 42), 1, 24),
      });
      if (request.signal.aborted || signal?.aborted) return;
      const order = Object.keys(AREA_STYLE);
      areas = (result.areas || []).sort((a, b) => order.indexOf(a.layer) - order.indexOf(b.layer) || extentSize(b) - extentSize(a));
      renderPins([...result.groups, ...markers]);
      const count = [result.total && integer(result.total) + " markers", areas.length && integer(areas.length) + " areas"]
        .filter(Boolean).join(" · ");
      status.textContent = count ? count + " in view" : "No matching features in view";
    } catch (error) {
      if (!request.signal.aborted) status.textContent = "Couldn't load map features";
    }
  }

  function localPoint(event) {
    const box = view.getBoundingClientRect();
    return [event.clientX - box.left, event.clientY - box.top];
  }

  function resetInteractions() {
    pointers.clear();
    areaPress = null;
    view.classList.remove("atlas-map-dragging");
  }

  function showCursorPosition(point) {
    if (!frame) return;
    const coordinates = worldAt(...point).map(value => Math.round(value * 100) / 100);
    positionReadout.querySelector(".copy-value").textContent = "X " + integer(coordinates[0]) + " · Z " + integer(coordinates[1]);
    positionReadout.dataset.copyText = coordinateValue(coordinates, ["X", "Z"]);
    positionReadout.disabled = false;
  }

  tooltip.addEventListener("pointerenter", () => {
    clearTimeout(hoverTimer);
    clearTimeout(hoverOpenTimer);
    hoverTimer = null;
  });
  tooltip.addEventListener("pointerleave", queueTooltipClose);
  tooltip.addEventListener("focusin", () => {
    clearTimeout(hoverTimer);
    hoverTimer = null;
  });
  tooltip.addEventListener("focusout", queueTooltipClose);
  view.addEventListener("pointerleave", () => {
    if (hovering?.node === null && !tooltip.contains(document.activeElement)) queueTooltipClose();
  });
  view.addEventListener("contextmenu", event => {
    if (!frame || (event.target.closest("button, input, select, .atlas-map-popup, .atlas-map-tooltip, .atlas-map-layer-panel") &&
                   !event.target.closest(".atlas-map-pin"))) return;
    const clicked = document.elementsFromPoint(event.clientX, event.clientY);
    const picked = clicked.map(element => pins.find(pin => pin.node === element)).filter(Boolean);
    if (openPointMenu(localPoint(event), picked.map(pin => pin.marker), picked[0]?.node || view)) event.preventDefault();
  });
  view.addEventListener("pointerdown", event => {
    if ((event.pointerType === "mouse" && event.button !== 0) ||
        event.target.closest("button, input, select, .atlas-map-popup, .atlas-map-tooltip, .atlas-map-layer-panel")) return;
    closeSelection(false);
    if (event.pointerType !== "touch") view.focus({ preventScroll: true });
    const point = localPoint(event);
    showCursorPosition(point);
    const hit = frame && !pointers.size ? areasAt(point, true)[0] : null;
    areaPress = hit ? { id: event.pointerId, point, hit } : null;
    pointers.set(event.pointerId, point);
    view.setPointerCapture(event.pointerId);
    view.classList.add("atlas-map-dragging");
  });
  view.addEventListener("pointermove", event => {
    if (!frame) return;
    const point = localPoint(event);
    const artwork = !event.target.closest("button, input, select, .atlas-map-popup, .atlas-map-tooltip, .atlas-map-layer-panel");
    if (artwork) {
      showCursorPosition(point);
    }
    if (!pointers.has(event.pointerId)) {
      if (artwork && event.pointerType !== "touch" && !pinned && !tooltip.contains(document.activeElement) &&
          (!hovering?.node || hovering.node !== document.activeElement)) {
        const area = areasAt(point, true)[0];
        if (area && hovering?.marker !== area) {
          const [x, z] = worldAt(...point);
          showTooltip(area, null, [x, null, z]);
        } else if (area || hovering?.node === null) {
          if (area) {
            clearTimeout(hoverTimer);
            hoverTimer = null;
          }
          else if (!hoverTimer) queueTooltipClose();
        }
      }
      return;
    }
    if (areaPress?.id === event.pointerId) {
      if (Math.hypot(point[0] - areaPress.point[0], point[1] - areaPress.point[1]) <= 6) return;
      areaPress = null;
    }
    const previous = pointers.get(event.pointerId);
    pointers.set(event.pointerId, point);
    const other = [...pointers.entries()].find(([key]) => key !== event.pointerId)?.[1];
    const dx = point[0] - previous[0], dy = point[1] - previous[1];
    if (!other) moveCamera(dx, dy);
    else {
      const before = Math.hypot(previous[0] - other[0], previous[1] - other[1]);
      const after = Math.hypot(point[0] - other[0], point[1] - other[1]);
      if (before > 5) zoomAt(after / before, (point[0] + other[0]) / 2, (point[1] + other[1]) / 2);
      moveCamera(dx / 2, dy / 2);
    }
  });
  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
    view.addEventListener(type, event => {
      if (type === "pointerup" && areaPress?.id === event.pointerId) {
        const [x, z] = worldAt(...areaPress.point);
        if (screenPoint([x, null, z])) openPopup(areaPress.hit, view, false, [x, null, z]);
      }
      pointers.delete(event.pointerId);
      if (!pointers.size) resetInteractions();
    });
  }
  view.addEventListener("dblclick", event => {
    if (!event.target.closest("button, .atlas-map-layer-panel, .atlas-map-tooltip, .atlas-map-popup")) zoomAt(1.75, ...localPoint(event));
  });
  view.addEventListener("wheel", event => {
    if ((!event.ctrlKey && document.activeElement !== view) || event.target.closest(".atlas-map-layer-panel, .atlas-map-popup")) return;
    event.preventDefault();
    zoomAt(event.deltaY < 0 ? 1.35 : 1 / 1.35, ...localPoint(event));
  }, { passive: false });
  view.addEventListener("keydown", event => {
    if (event.key === "Escape" && pinned) {
      event.preventDefault();
      closeSelection();
      return;
    }
    if (event.key === "Escape" && !layerPanel.hidden) {
      event.preventDefault();
      layerButton.click();
      layerButton.focus();
      return;
    }
    if (frame && (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10"))) {
      const pin = pins.find(item => item.node === event.target);
      const point = pin ? screenPoint(pin.marker.coordinates) :
        event.target === view ? [view.clientWidth / 2, view.clientHeight / 2] : null;
      if (point && openPointMenu(point, pin ? [pin.marker] : [], event.target)) event.preventDefault();
      return;
    }
    if (event.target !== view) return;
    const offsets = { ArrowLeft: [64, 0], ArrowRight: [-64, 0], ArrowUp: [0, 64], ArrowDown: [0, -64] };
    if (offsets[event.key]) moveCamera(...offsets[event.key]);
    else if (["+", "=", "-"].includes(event.key)) zoomAt(event.key === "-" ? 1 / 1.6 : 1.6);
    else if (event.key === "Home") resetCamera();
    else return;
    event.preventDefault();
  });
  plus.addEventListener("click", () => zoomAt(1.6));
  minus.addEventListener("click", () => zoomAt(1 / 1.6));
  reset.addEventListener("click", resetCamera);
  fit.addEventListener("click", fitChart);
  function setFullscreenLabel() {
    const active = document.fullscreenElement === root || root.classList.contains("atlas-map-expanded");
    expand.title = active ? "Exit fullscreen map" : "Fullscreen map";
    expand.setAttribute("aria-label", expand.title);
  }
  document.addEventListener("fullscreenchange", setFullscreenLabel, { signal });
  expand.addEventListener("click", async () => {
    if (document.fullscreenElement === root) await document.exitFullscreen();
    else if (root.classList.contains("atlas-map-expanded")) root.classList.remove("atlas-map-expanded");
    else {
      try {
        await root.requestFullscreen();
      } catch {
        root.classList.add("atlas-map-expanded");
      }
    }
    setFullscreenLabel();
  });
  regionPicker.addEventListener("change", () => {
    chart = charts[regionPicker.value] || sceneChart;
    fullExtent = Boolean(!chart.image && chart.grid_bounds);
    showChart();
  });
  cityPicker.addEventListener("change", () => {
    const city = cities.find(item => String(item.id) === cityPicker.value);
    if (!city) return;
    if (!projectMapPoint(projection(), city.coordinates)) {
      chart = sceneChart;
      regionPicker.value = chart.id;
      fullExtent = Boolean(chart.grid_bounds && !projectMapPoint(chart, city.coordinates));
      showChart();
    }
    if (!activeLayers.has("city")) {
      activeLayers.add("city");
      drawLayers();
      updateFilters();
    }
    if (filter) {
      filter = "";
      search.value = "";
    }
    const [x, , z] = city.coordinates;
    const [xmin, zmin, xmax, zmax] = projection().bounds;
    const span = clamp(Math.min(xmax - xmin, zmax - zmin) / 32, 170, 600);
    centerOn([x - span, z - span, x + span, z + span]);
    layout();
    loadSoon(0);
    openPopup(city, cityPicker, false);
  });
  extentPicker.addEventListener("change", () => {
    fullExtent = extentPicker.value === "full";
    showChart();
  });
  layerButton.addEventListener("click", () => {
    layerPanel.hidden = !layerPanel.hidden;
    layerButton.setAttribute("aria-expanded", String(!layerPanel.hidden));
  });
  layerChoices.addEventListener("change", event => {
    if (event.target.checked) activeLayers.add(event.target.value);
    else activeLayers.delete(event.target.value);
    updateFilters();
  });
  all.addEventListener("click", () => {
    for (const key of Object.keys(definitions)) activeLayers.add(key);
    setGrid(true);
    drawLayers();
    updateFilters();
  });
  clear.addEventListener("click", () => {
    activeLayers.clear();
    setGrid(false);
    drawLayers();
    updateFilters();
  });
  search.addEventListener("input", () => {
    filter = search.value.trim().slice(0, 100);
    closeSelection(false);
    loadSoon(190);
  });
  gridToggle.addEventListener("change", () => setGrid(gridToggle.checked));

  const observer = new ResizeObserver(() => {
    if (fitted) zoom = fitZoom();
    paintSoon();
    loadSoon();
  });
  observer.observe(view);
  signal?.addEventListener("abort", () => {
    observer.disconnect();
    clearTimeout(pending);
    clearTimeout(hoverTimer);
    clearTimeout(hoverOpenTimer);
    cancelAnimationFrame(raf);
    fetchController?.abort();
    detailController?.abort();
  }, { once: true });
  showChart();
}
