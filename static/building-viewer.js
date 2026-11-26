import { copyControl, coordinateValue, href, integer, readable, spawnSummary } from "./ui.js";

function tracePolygon(path, points) {
  path.moveTo(...points[0]);
  for (const point of points.slice(1)) path.lineTo(...point);
  path.closePath();
}

function cellOutline(floor, screen) {
  const pixels = floor.points.map(screen);
  const surface = new Path2D();
  const edgeCounts = new Map();
  const outline = new Path2D();
  for (const face of floor.faces) {
    const [a, b, c] = face.map(index => pixels[index]);
    const cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    if (cross === 0) continue;
    tracePolygon(surface, cross > 0 ? [a, b, c] : [a, c, b]);
    for (const [start, end] of [[face[0], face[1]], [face[1], face[2]], [face[2], face[0]]]) {
      const id = Math.min(start, end) + "," + Math.max(start, end);
      const edge = edgeCounts.get(id);
      if (edge) edge.count++;
      else edgeCounts.set(id, { start, end, count: 1 });
    }
  }
  for (const { start, end, count } of edgeCounts.values()) {
    if (count !== 1) continue;
    outline.moveTo(...pixels[start]);
    outline.lineTo(...pixels[end]);
  }
  return { surface, outline };
}

function roomName(room) {
  return readable(room.name);
}

function roomSummary(room) {
  const count = room.contents.length;
  return roomName(room) + " · " + integer(count) + (count === 1 ? " object" : " objects");
}

export function revealBuildingRoom(index, scroll = true, objectIndex = null) {
  const cell = document.getElementById("building-cell-" + index);
  if (!cell) return;
  cell.open = true;
  const item = objectIndex != null && document.getElementById("building-cell-" + index + "-object-" + objectIndex);
  if (item) item.dataset.placementFocus = "";
  if (scroll) {
    const target = item || cell;
    target.scrollIntoView({ block: "start", behavior: "instant" });
    (item || cell.querySelector("summary")).focus({ preventScroll: true });
  }
}

export function mountBuildingPlan(host, building, signal, placement) {
  const selector = host.querySelector("select");
  const plan = host.querySelector(".building-plan");
  const canvas = host.querySelector("canvas");
  const markerLayer = host.querySelector(".building-plan-markers");
  const tooltip = host.querySelector(".building-plan-tooltip");
  const status = host.querySelector(".building-plan-status");
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const exterior = building.extent;
  const selectedRoom = building.cells.find(room => room.index === placement?.cell_index);
  const selectedItem = selectedRoom?.contents[placement?.object_index];
  let positionAt;
  let cells = [];
  let geometry = [];
  let markers = new Map();
  let footprint = null;
  let hovering = null;
  let density = 1;
  let focused = null;
  let outsidePlan = 0;

  if (selectedRoom) {
    selector.value = String(selectedRoom.index);
    revealBuildingRoom(selectedRoom.index, !selectedItem?.coordinates, placement.object_index);
  }

  function instruction() {
    const count = markers.size;
    status.textContent = integer(cells.length) + (cells.length === 1 ? " cell" : " cells") +
      " · " + integer(count) + (count === 1 ? " mapped object." : " mapped objects.") +
      " Select a cell to open its contents.";
    if (outsidePlan) status.textContent += " " + integer(outsidePlan) + (outsidePlan === 1 ?
      " object is outside the floor plan." : " objects are outside the floor plan.");
    if (focused) status.textContent = "Selected " + focused.item.name + " in " + roomName(focused.room) + ".";
    if (selectedItem?.coordinates && selectedRoom && selector.value === String(selectedRoom.index) && !focused) {
      status.textContent = "The recorded position of " + selectedItem.name + " is outside this floor plan.";
      const onMap = document.createElement("a");
      onMap.href = href("locations", building.id) + "?view=map&placement=" + encodeURIComponent(placement.id);
      onMap.textContent = "View recorded point on world map";
      status.append(" ", onMap);
    }
  }

  function paint() {
    const width = canvas.clientWidth, height = canvas.clientHeight;
    ctx.clearRect(0, 0, width, height);
    if (!positionAt) {
      ctx.fillStyle = "#b3c5d0";
      ctx.font = "14px system-ui";
      ctx.textAlign = "center";
      ctx.fillText("No mapped geometry or objects for this cell", width / 2, height / 2);
      return;
    }
    const active = hovering || focused?.room;
    const front = geometry.find(entry => entry.room === active);
    for (const { room, surface, outline } of front ? [...geometry.filter(entry => entry !== front), front] : geometry) {
      const highlight = room === active;
      ctx.fillStyle = highlight ? "#4baab7bf" : "#27565f98";
      ctx.strokeStyle = highlight ? "#cefff5" : "#8cd3d5b8";
      ctx.lineWidth = highlight ? 2.6 : 1.2;
      ctx.fill(surface);
      ctx.stroke(outline);
    }
    if (footprint) {
      ctx.strokeStyle = "#ffdb94";
      ctx.lineWidth = 1.7;
      ctx.setLineDash([5, 4]);
      ctx.stroke(footprint);
      ctx.setLineDash([]);
    }
  }

  function objectMarker(room, item, itemIndex, point) {
    const node = document.createElement(item.target ? "a" : "button");
    if (item.target) node.href = href(...item.target);
    else {
      node.type = "button";
      node.addEventListener("click", () => revealBuildingRoom(room.index, true, itemIndex));
    }
    node.className = "building-plan-marker";
    node.setAttribute("aria-label", item.name + " in " + roomName(room));
    node.style.left = point[0] + "px";
    node.style.top = point[1] + "px";
    const marker = { node, point, item, room };
    markers.set(node, marker);
    if (item === selectedItem) {
      focused = marker;
      node.dataset.placementFocus = "";
      node.setAttribute("aria-current", "location");
    }
    node.addEventListener("focus", () => highlight(room, marker, point));
    node.addEventListener("blur", () => highlight(null));
    return node;
  }

  function draw() {
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    density = Math.min(devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * density);
    canvas.height = Math.round(height * density);
    ctx.setTransform(density, 0, 0, density, 0, 0);
    cells = selector.value ? building.cells.filter(cell => cell.index === Number(selector.value)) : building.cells;
    const objects = cells.flatMap(room => room.contents.map((item, index) => ({ room, item, index }))).filter(({ item }) => item.coordinates);
    const positions = cells.flatMap(cell => cell.floor?.points || []);
    const perimeter = exterior?.edges?.length ? exterior.edges : exterior?.paths;
    if ((!selector.value || !positions.length) && exterior) {
      const [xmin, zmin, xmax, zmax] = exterior.bounds;
      positions.push([xmin, zmin], [xmax, zmax]);
    }
    // Frame recovered geometry first. A distant placement keeps its exact
    // coordinates in the cell table without shrinking usable geometry.
    if (!positions.length) positions.push(...objects.map(({ item }) => [item.coordinates[0], item.coordinates[2]]));
    geometry = [];
    markers = new Map();
    markerLayer.replaceChildren();
    footprint = null;
    focused = null;
    outsidePlan = 0;
    hovering = null;
    plan.classList.remove("building-plan-cell-hover");
    positionAt = null;
    if (positions.length) {
      const [xmin, zmin, xmax, zmax] = positions.reduce(([minX, minZ, maxX, maxZ], [x, z]) =>
        [Math.min(minX, x), Math.min(minZ, z), Math.max(maxX, x), Math.max(maxZ, z)], [Infinity, Infinity, -Infinity, -Infinity]);
      const scale = Math.min((width - 46) / Math.max(xmax - xmin, 8), (height - 46) / Math.max(zmax - zmin, 8));
      const centerX = (xmin + xmax) / 2, centerZ = (zmin + zmax) / 2;
      const screen = ([x, z]) => [width / 2 + (x - centerX) * scale, height / 2 - (z - centerZ) * scale];
      positionAt = ([x, y]) => [centerX + (x - width / 2) / scale, centerZ - (y - height / 2) / scale]
        .map(value => Math.round(value * 100) / 100);
      geometry = cells.filter(cell => cell.floor).map(room => ({ room, ...cellOutline(room.floor, screen) }));
      if (perimeter?.length) {
        footprint = new Path2D();
        for (const path of perimeter) {
          const pixels = path.map(screen);
          if (path.length > 2) tracePolygon(footprint, pixels);
          else {
            footprint.moveTo(...pixels[0]);
            footprint.lineTo(...pixels[1]);
          }
        }
      }
      const batch = document.createDocumentFragment();
      for (const { room, item, index } of objects) {
        const point = screen([item.coordinates[0], item.coordinates[2]]);
        if (point[0] < 7 || point[0] > width - 7 || point[1] < 7 || point[1] > height - 7) {
          outsidePlan++;
          continue;
        }
        batch.append(objectMarker(room, item, index, point));
      }
      markerLayer.append(batch);
    }
    paint();
    highlight(null);
    if (!status.querySelector("button")) instruction();
  }

  function localPosition(event) {
    const box = canvas.getBoundingClientRect();
    return [event.clientX - box.left, event.clientY - box.top];
  }

  function cellAt([x, y]) {
    return geometry.findLast(cell => ctx.isPointInPath(cell.surface, x * density, y * density))?.room;
  }

  function highlight(room, marker = null, point = null) {
    if (hovering !== room) {
      hovering = room;
      plan.classList.toggle("building-plan-cell-hover", Boolean(room));
      paint();
    }
    marker ||= !room && focused;
    room ||= marker?.room;
    point ||= marker?.point;
    tooltip.hidden = !room;
    if (!room) return;
    tooltip.textContent = (marker ? marker.item.name + " · " : "") + roomSummary(room);
    if (marker?.item.spawner) {
      const detail = document.createElement("small");
      detail.textContent = spawnSummary(marker.item.spawner);
      tooltip.append(detail);
    }
    tooltip.style.left = Math.max(6, Math.min(point[0] + 13, canvas.clientWidth - tooltip.offsetWidth - 6)) + "px";
    tooltip.style.top = Math.max(6, Math.min(point[1] + 12, canvas.clientHeight - tooltip.offsetHeight - 6)) + "px";
  }

  const observer = new ResizeObserver(draw);
  observer.observe(canvas);
  signal.addEventListener("abort", () => observer.disconnect(), { once: true });
  selector.addEventListener("change", () => {
    if (selector.value) revealBuildingRoom(Number(selector.value), false);
    draw();
    instruction();
  });
  host.querySelector("[data-cell-contents]").addEventListener("click", () => {
    if (selector.value) revealBuildingRoom(Number(selector.value));
    else document.getElementById("building-cells").scrollIntoView({ block: "start" });
  });
  plan.addEventListener("pointermove", event => {
    const point = localPosition(event);
    const marker = markers.get(event.target);
    highlight(marker?.room || cellAt(point) || null, marker, point);
  });
  plan.addEventListener("pointerleave", () => highlight(null));
  canvas.addEventListener("click", event => {
    const room = cellAt(localPosition(event));
    if (room) revealBuildingRoom(room.index);
  });
  plan.addEventListener("contextmenu", event => {
    if (!positionAt || event.target.closest("a,button")) return;
    event.preventDefault();
    const point = positionAt(localPosition(event));
    const text = coordinateValue(point, ["X", "Z"]);
    const copy = copyControl("Copy coordinates · " + text, text, "Copy floor plan coordinates", "coordinate");
    copy.addEventListener("keydown", event => {
      if (event.key === "Escape") instruction();
    });
    status.replaceChildren(copy);
    copy.focus({ preventScroll: true });
  });
  draw();
  if (selectedItem?.coordinates) host.scrollIntoView({ block: "start", behavior: "instant" });
  else if (selectedRoom) revealBuildingRoom(selectedRoom.index, true, placement.object_index);
}
