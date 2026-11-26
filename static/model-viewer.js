/* Browser preview for decoded SWG geometry and authored animation tracks. */

import { copyControl, sourceControl } from "./ui.js";

const identity = [0, 0, 0, 1];
const origin = [0, 0, 0];
const initialCamera = {yaw: .35, pitch: -.06, zoom: .92};

function element(tag, label) {
  const node = document.createElement(tag);
  if (label) node.textContent = label;
  return node;
}

function button(label) {
  const node = element("button", label);
  node.type = "button";
  node.className = "model-viewer-button";
  return node;
}

function product(a, b) {
  const [x, y, z, w] = a;
  const [i, j, k, r] = b;
  return [
    w * i + x * r + y * k - z * j, w * j - x * k + y * r + z * i,
    w * k + x * j - y * i + z * r, w * r - x * i - y * j - z * k,
  ];
}

function rotate(q, v) {
  const [x, y, z, w] = q;
  const [a, b, c] = v;
  const tx = 2 * (y * c - z * b);
  const ty = 2 * (z * a - x * c);
  const tz = 2 * (x * b - y * a);
  return [
    a + w * tx + y * tz - z * ty, b + w * ty + z * tx - x * tz, c + w * tz + x * ty - y * tx,
  ];
}

function bracket(keys, frame) {
  let low = 0;
  let high = keys.length - 1;
  if (frame <= keys[0][0]) return [keys[0], keys[0], 0];
  if (frame >= keys[high][0]) return [keys[high], keys[high], 0];
  while (high - low > 1) {
    const center = Math.floor((low + high) / 2);
    if (keys[center][0] > frame) high = center;
    else low = center;
  }
  return [keys[low], keys[high], (frame - keys[low][0]) / (keys[high][0] - keys[low][0])];
}

function sampleRotation(keys, frame) {
  const [before, after, fraction] = bracket(keys, frame);
  const a = before.slice(1);
  if (!fraction) return a;
  let b = after.slice(1);
  let dot = a.reduce((sum, value, index) => sum + value * b[index], 0);
  if (dot < 0) {
    dot = -dot;
    b = b.map(value => -value);
  }
  if (dot > .9995) {
    const result = a.map((value, index) => value * (1 - fraction) + b[index] * fraction);
    const length = Math.hypot(...result);
    return result.map(value => value / length);
  }
  const angle = Math.acos(dot);
  const left = Math.sin((1 - fraction) * angle) / Math.sin(angle);
  const right = Math.sin(fraction * angle) / Math.sin(angle);
  return a.map((value, index) => value * left + b[index] * right);
}

function sampleNumber(keys, frame) {
  const [before, after, fraction] = bracket(keys, frame);
  return before[1] + fraction * (after[1] - before[1]);
}

function absolutePose(rig, tracks = {}, frame = 0) {
  const pose = [];
  rig.forEach((bone, index) => {
    const track = tracks[index];
    const own = track ? sampleRotation(track[0], frame) : identity;
    const delta = track ? track.slice(1).map(keys => sampleNumber(keys, frame)) : origin;
    let rotation = product(bone.post, product(product(own, bone.bind_rotation), bone.pre));
    let offset = bone.bind_translation.map((base, axis) => base + delta[axis]);
    if (bone.parent >= 0) {
      const parent = pose[bone.parent];
      rotation = product(parent.rotation, rotation);
      offset = rotate(parent.rotation, offset).map((coordinate, axis) => coordinate + parent.offset[axis]);
    }
    pose.push({rotation, offset});
  });
  return pose;
}

function transforms(pose, bindPose) {
  return pose.map((bone, i) => {
    const rest = bindPose[i];
    const [a, b, c, d] = rest.rotation;
    const rotation = product(bone.rotation, [-a, -b, -c, d]);
    const [x, y, z, w] = rotation;
    const [dx, dy, dz] = rotate(rotation, rest.offset);
    return [
      1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), bone.offset[0] - dx,
      2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), bone.offset[1] - dy,
      2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), bone.offset[2] - dz,
    ];
  });
}

function deform(mesh, matrices) {
  const {positions, normals, weights, joints, currentPosition, currentNormal} = mesh;
  for (let vertex = 0; vertex < positions.length / 3; vertex += 1) {
    const p = vertex * 3;
    const j = vertex * 4;
    const x = positions[p], y = positions[p + 1], z = positions[p + 2];
    const nx = normals[p], ny = normals[p + 1], nz = normals[p + 2];
    let moved = false;
    currentPosition[p] = currentPosition[p + 1] = currentPosition[p + 2] = 0;
    currentNormal[p] = currentNormal[p + 1] = currentNormal[p + 2] = 0;
    for (let i = 0; i < 4; i += 1) {
      const weight = weights[j + i];
      const matrix = matrices[joints[j + i]];
      if (!weight || !matrix) continue;
      moved = true;
      for (let axis = 0; axis < 3; axis += 1) {
        const row = axis * 4;
        const u = matrix[row], v = matrix[row + 1], w = matrix[row + 2];
        currentPosition[p + axis] += weight * (u * x + v * y + w * z + matrix[row + 3]);
        currentNormal[p + axis] += weight * (u * nx + v * ny + w * nz);
      }
    }
    if (!moved) {
      currentPosition[p] = x;
      currentPosition[p + 1] = y;
      currentPosition[p + 2] = z;
      currentNormal[p] = nx;
      currentNormal[p + 1] = ny;
      currentNormal[p + 2] = nz;
    }
  }
}

function buildGeometry(data) {
  const minimum = [Infinity, Infinity, Infinity];
  const maximum = [-Infinity, -Infinity, -Infinity];
  const meshes = data.meshes.map(group => {
    group.positions.forEach((value, i) => {
      const axis = i % 3;
      minimum[axis] = Math.min(minimum[axis], value);
      maximum[axis] = Math.max(maximum[axis], value);
    });
    const positions = new Float32Array(group.positions);
    const normals = new Float32Array(group.normals);
    return {...group, positions, normals, currentPosition: positions.slice(), currentNormal: normals.slice()};
  });
  const center = minimum.map((value, axis) => (value + maximum[axis]) / 2);
  const radius = Math.max(.05, Math.hypot(...maximum.map((value, axis) => value - minimum[axis])) / 2);
  return {meshes, center, radius};
}

function camera(state, canvas, geometry) {
  const cx = Math.cos(state.pitch), sx = Math.sin(state.pitch);
  // SWG's left-handed coordinates need a reversed screen X when the camera
  // looks toward negative Z. Reverse yaw to keep the orbit controls intuitive.
  const cy = Math.cos(state.yaw), sy = -Math.sin(state.yaw);
  const aspect = canvas.width / canvas.height;
  const scale = 2.2 * Math.min(1, aspect);
  return {
    rotation: [cy, 0, sy, sx * sy, cx, -sx * cy, -cx * sy, sx, cx * cy],
    center: geometry.center, distance: geometry.radius * 3.3 / state.zoom,
    scale: [-scale / aspect, scale], near: geometry.radius * .06, far: geometry.radius * 16,
  };
}

function compile(gl, kind, source) {
  const shader = gl.createShader(kind);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const info = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(info);
  }
  return shader;
}

const vertexSource = [
  "#version 300 es",
  "layout(location=0) in vec3 aPosition;",
  "layout(location=1) in vec3 aNormal;",
  "layout(location=2) in vec2 aUV;",
  "uniform mat3 uRotation;",
  "uniform vec3 uCenter;",
  "uniform vec2 uScale;",
  "uniform float uDistance;",
  "uniform vec2 uDepth;",
  "out vec3 vNormal;",
  "out vec2 vUV;",
  "void main() {",
  "  vec3 p = uRotation * (aPosition - uCenter);",
  "  float depth = uDistance - p.z;",
  "  gl_Position = vec4(uScale * p.xy, uDepth.x * depth - uDepth.y, depth);",
  "  vNormal = uRotation * aNormal;",
  "  vUV = aUV;",
  "}",
].join("\n");

const fragmentSource = [
  "#version 300 es",
  "precision mediump float;",
  "in vec3 vNormal;",
  "in vec2 vUV;",
  "uniform vec3 uColor;",
  "uniform sampler2D uTexture;",
  "uniform bool uTextureReady;",
  "uniform int uAlphaComparison;",
  "uniform float uAlphaReference;",
  "uniform bool uBlendAlpha;",
  "out vec4 color;",
  "bool passesAlpha(float alpha) {",
  "  int a = int(alpha * 255.0 + 0.5);",
  "  int r = int(uAlphaReference * 255.0 + 0.5);",
  "  if (uAlphaComparison == 0) return false;",
  "  if (uAlphaComparison == 1) return a < r;",
  "  if (uAlphaComparison == 2) return a == r;",
  "  if (uAlphaComparison == 3) return a <= r;",
  "  if (uAlphaComparison == 4) return a > r;",
  "  if (uAlphaComparison == 5) return a >= r;",
  "  if (uAlphaComparison == 6) return a != r;",
  "  return true;",
  "}",
  "void main() {",
  "  vec4 base = uTextureReady ? texture(uTexture, vUV) : vec4(uColor, 1.0);",
  "  if (uTextureReady && !passesAlpha(base.a)) discard;",
  "  vec3 normal = normalize(vNormal);",
  "  float key = max(0.0, dot(normal, normalize(vec3(0.55, 0.8, 0.8))));",
  "  float fill = max(0.0, dot(normal, normalize(vec3(-0.85, 0.2, 0.1))));",
  "  color = vec4(base.rgb * (0.62 + 0.42 * key + 0.16 * fill), uBlendAlpha ? base.a : 1.0);",
  "}",
].join("\n");

function webRenderer(canvas, meshes, assets, redraw) {
  const gl = canvas.getContext("webgl2", {antialias: true, alpha: true});
  if (!gl) return null;
  const vertex = compile(gl, gl.VERTEX_SHADER, vertexSource);
  const fragment = compile(gl, gl.FRAGMENT_SHADER, fragmentSource);
  const program = gl.createProgram();
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  gl.deleteShader(vertex);
  gl.deleteShader(fragment);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const info = gl.getProgramInfoLog(program);
    gl.deleteProgram(program);
    throw new Error(info);
  }
  gl.useProgram(program);
  gl.enable(gl.DEPTH_TEST);
  gl.frontFace(gl.CW);
  const uniforms = {};
  for (const name of ["Rotation", "Center", "Scale", "Distance", "Depth", "Color", "Texture", "TextureReady",
    "AlphaComparison", "AlphaReference", "BlendAlpha"]) {
    uniforms[name] = gl.getUniformLocation(program, "u" + name);
  }
  gl.uniform1i(uniforms.Texture, 0);
  const fallback = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, fallback);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE,
    new Uint8Array([255, 255, 255, 255]));
  const buffers = meshes.map(mesh => {
    const array = gl.createVertexArray();
    gl.bindVertexArray(array);
    const attrs = [mesh.currentPosition, mesh.currentNormal, new Float32Array(mesh.uvs)].map((values, i) => {
      const buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, values, i === 2 ? gl.STATIC_DRAW : gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(i);
      gl.vertexAttribPointer(i, i === 2 ? 2 : 3, gl.FLOAT, false, 0, 0);
      return buffer;
    });
    const indices = gl.createBuffer();
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indices);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(mesh.indices), gl.STATIC_DRAW);
    return {array, attrs, indices};
  });
  const opaque = [], transparent = [];
  meshes.forEach((mesh, index) => (mesh.material?.blend ? transparent : opaque).push(index));
  const factors = [gl.ZERO, gl.ONE, gl.SRC_COLOR, gl.ONE_MINUS_SRC_COLOR, gl.SRC_ALPHA,
    gl.ONE_MINUS_SRC_ALPHA, gl.DST_ALPHA, gl.ONE_MINUS_DST_ALPHA, gl.DST_COLOR,
    gl.ONE_MINUS_DST_COLOR, gl.SRC_ALPHA_SATURATE];
  const equations = [gl.FUNC_ADD, gl.FUNC_SUBTRACT, gl.FUNC_REVERSE_SUBTRACT, gl.MIN, gl.MAX];
  const textures = new Map();
  let disposed = false;
  if (typeof createImageBitmap !== "undefined") {
    for (const [name, uri] of Object.entries(assets)) {
      const prefix = "data:image/png;base64,";
      if (!uri.startsWith(prefix)) continue;
      const bytes = Uint8Array.from(atob(uri.slice(prefix.length)), char => char.charCodeAt(0));
      createImageBitmap(new Blob([bytes], {type: "image/png"}), {premultiplyAlpha: "none"}).then(image => {
        if (!disposed) {
          const texture = gl.createTexture();
          gl.bindTexture(gl.TEXTURE_2D, texture);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
          gl.generateMipmap(gl.TEXTURE_2D);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
          textures.set(name, texture);
          redraw();
        }
        image.close();
      }).catch(() => {});
    }
  }
  return {
    name: "WebGL",
    render(view, changed) {
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      const r = view.rotation;
      gl.uniformMatrix3fv(uniforms.Rotation, false, [r[0], r[3], r[6], r[1], r[4], r[7], r[2], r[5], r[8]]);
      gl.uniform3fv(uniforms.Center, view.center);
      gl.uniform2fv(uniforms.Scale, view.scale);
      gl.uniform1f(uniforms.Distance, view.distance);
      gl.uniform2f(uniforms.Depth, (view.far + view.near) / (view.far - view.near),
        (2 * view.far * view.near) / (view.far - view.near));
      const sorted = transparent.map(index => {
        const vertices = meshes[index].currentPosition;
        let depth = 0;
        for (let i = 0; i < vertices.length; i += 3) {
          depth += r[6] * vertices[i] + r[7] * vertices[i + 1] + r[8] * vertices[i + 2];
        }
        return {index, depth: depth * 3 / vertices.length};
      }).sort((a, b) => a.depth - b.depth);
      for (const index of [...opaque, ...sorted.map(item => item.index)]) {
        const mesh = meshes[index];
        const {array, attrs} = buffers[index];
        if (changed) {
          gl.bindBuffer(gl.ARRAY_BUFFER, attrs[0]);
          gl.bufferSubData(gl.ARRAY_BUFFER, 0, mesh.currentPosition);
          gl.bindBuffer(gl.ARRAY_BUFFER, attrs[1]);
          gl.bufferSubData(gl.ARRAY_BUFFER, 0, mesh.currentNormal);
        }
        const texture = textures.get(mesh.texture_key ?? mesh.texture);
        const test = mesh.material?.alpha_test;
        const blend = texture ? mesh.material?.blend : undefined;
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, texture || fallback);
        gl.uniform1i(uniforms.TextureReady, texture ? 1 : 0);
        gl.uniform1i(uniforms.AlphaComparison, test?.comparison ?? 7);
        gl.uniform1f(uniforms.AlphaReference, test?.reference ?? 0);
        gl.uniform1i(uniforms.BlendAlpha, blend ? 1 : 0);
        if (blend) {
          gl.enable(gl.BLEND);
          gl.depthMask(Boolean(mesh.material.depth_write));
          gl.blendEquationSeparate(equations[blend[0]] ?? gl.FUNC_ADD, gl.FUNC_ADD);
          gl.blendFuncSeparate(factors[blend[1]] ?? gl.SRC_ALPHA,
            factors[blend[2]] ?? gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
        } else {
          gl.disable(gl.BLEND);
          gl.depthMask(true);
        }
        // Authored meshes have clockwise fronts after the projection above.
        // Proxy floors and collision geometry remain visible from either side.
        if (mesh.shader) gl.enable(gl.CULL_FACE);
        else gl.disable(gl.CULL_FACE);
        gl.uniform3fv(uniforms.Color, mesh.color);
        gl.bindVertexArray(array);
        gl.drawElements(gl.TRIANGLES, mesh.indices.length, gl.UNSIGNED_SHORT, 0);
      }
      gl.depthMask(true);
    },
    dispose() {
      disposed = true;
      for (const texture of textures.values()) gl.deleteTexture(texture);
      gl.deleteTexture(fallback);
      for (const {array, attrs, indices} of buffers) {
        for (const attr of attrs) gl.deleteBuffer(attr);
        gl.deleteBuffer(indices);
        gl.deleteVertexArray(array);
      }
      gl.deleteProgram(program);
    },
  };
}

function softwareRenderer(canvas, meshes) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  return {
    name: "software rendering",
    render(view) {
      const triangles = [];
      const r = view.rotation;
      const [cX, cY, cZ] = view.center;
      for (const mesh of meshes) {
        const vertices = [];
        for (let i = 0; i < mesh.currentPosition.length; i += 3) {
          const x = mesh.currentPosition[i] - cX;
          const y = mesh.currentPosition[i + 1] - cY;
          const z = mesh.currentPosition[i + 2] - cZ;
          const depth = view.distance - (r[6] * x + r[7] * y + r[8] * z);
          vertices.push([
            (1 + view.scale[0] * (r[0] * x + r[1] * y + r[2] * z) / depth) * canvas.width / 2,
            (1 - view.scale[1] * (r[3] * x + r[4] * y + r[5] * z) / depth) * canvas.height / 2,
            depth,
          ]);
        }
        for (let i = 0; i < mesh.indices.length; i += 3) {
          const a = mesh.indices[i], b = mesh.indices[i + 1], c = mesh.indices[i + 2];
          const points = [vertices[a], vertices[b], vertices[c]];
          if (points.some(point => point[2] <= view.near)) continue;
          if (mesh.shader) {
            const [first, second, third] = points;
            const area = (second[0] - first[0]) * (third[1] - first[1]) -
              (second[1] - first[1]) * (third[0] - first[0]);
            if (area <= 0) continue;
          }
          const nx = mesh.currentNormal[a * 3], ny = mesh.currentNormal[a * 3 + 1], nz = mesh.currentNormal[a * 3 + 2];
          const light = Math.max(0, .34 * (r[0] * nx + r[1] * ny + r[2] * nz) +
            .53 * (r[3] * nx + r[4] * ny + r[5] * nz) + .76 * (r[6] * nx + r[7] * ny + r[8] * nz));
          const rgb = mesh.color.map(value => Math.min(255, Math.round(value * (.62 + .54 * light) * 255)));
          const depth = points.reduce((sum, point) => sum + point[2], 0);
          triangles.push({points, depth, rgb});
        }
      }
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      triangles.sort((a, b) => b.depth - a.depth);
      for (const {points, rgb} of triangles) {
        ctx.fillStyle = "rgb(" + rgb.join(",") + ")";
        ctx.strokeStyle = ctx.fillStyle;
        ctx.lineWidth = .6;
        ctx.beginPath();
        ctx.moveTo(points[0][0], points[0][1]);
        ctx.lineTo(points[1][0], points[1][1]);
        ctx.lineTo(points[2][0], points[2][1]);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
      }
    },
    dispose() {},
  };
}

/** Attach an interactive client model preview and return its cleanup callback. */
export function mountModelViewer(container, data) {
  container.replaceChildren();
  container.dataset.modelStatus = data?.status || "missing";
  if (data?.geometry_kind === "sprite" && data.sprite_image) {
    const figure = element("figure");
    figure.className = "model-sprite";
    const image = element("img");
    image.alt = "In-game sprite";
    image.src = data.sprite_image;
    const caption = element("figcaption", "In-game sprite ");
    const asset = data.sprite.texture;
    caption.append(copyControl(asset, asset, "Copy file reference", "copy-code"));
    figure.append(image, caption);
    container.append(figure);
    return () => {};
  }
  if (data?.status !== "available" || !data.meshes?.length) {
    container.append(element("p", data?.message || "No model data is available for this entry."));
    if (data?.appearance) container.append(copyControl(data.appearance, data.appearance, "Copy file reference", "copy-code"));
    if (data?.attempted?.length) {
      const details = element("details");
      details.append(element("summary", "Asset references checked"));
      const assets = element("ul");
      for (const name of data.attempted) {
        const entry = element("li");
        entry.append(copyControl(name, name, "Copy file reference", "copy-code"));
        assets.append(entry);
      }
      details.append(assets);
      container.append(details);
    }
    return () => {};
  }

  const geometry = buildGeometry(data);
  const clips = data.animations || [];
  const rig = data.rig || [];
  const bindPose = absolutePose(rig);
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const defaultCamera = rig.length ? initialCamera : {yaw: .55, pitch: -.5, zoom: 1.46};
  const state = {...defaultCamera, playing: clips.length > 0 && !reduceMotion};
  let activeClip = state.playing ? clips[0] : undefined;
  let elapsed = 0, scheduled = 0, lastTime = 0;
  let changed = true, dirty = true, disposed = false;
  let canvas = element("canvas");
  const schedule = () => {
    dirty = true;
    if (!disposed && !scheduled) scheduled = requestAnimationFrame(draw);
  };
  let renderer = null;
  try {
    renderer = webRenderer(canvas, geometry.meshes, data.textures || {}, schedule);
  } catch {
    canvas = element("canvas");
  }
  renderer ||= softwareRenderer(canvas, geometry.meshes);
  if (!renderer) {
    container.append(element("p", "The browser could not open a canvas for the model preview."));
    return () => {};
  }
  canvas.className = "model-viewer-canvas";
  canvas.tabIndex = 0;
  const preview = { bounds: "appearance bounds", collision: "collision mesh", floors: "interior floor meshes",
    display: "in-game display model" }[data.geometry_kind] || "game model";
  canvas.setAttribute("aria-label", `Interactive ${preview}. Use arrow keys to rotate and plus or minus to zoom.`);
  Object.assign(canvas.style, {
    display: "block", width: "100%", height: "min(52vh, 420px)", minHeight: "250px",
    background: "radial-gradient(ellipse at 50% 45%, #263848, #101822 78%)",
    borderRadius: "8px", cursor: "grab", touchAction: "none",
  });
  if (data.model_selection) container.append(element("p", data.model_selection +
    (data.missing_hull_model ? " · Hull geometry unavailable" : "")));
  container.append(canvas);

  const toolbar = element("div");
  toolbar.className = "model-viewer-toolbar";
  Object.assign(toolbar.style, {display: "flex", flexWrap: "wrap", alignItems: "center", gap: ".5rem", margin: ".65rem 0"});
  const select = element("select");
  select.setAttribute("aria-label", "Animation");
  const rest = element("option", "Rest pose");
  rest.value = "";
  select.append(rest);
  clips.forEach((clip, index) => {
    const option = element("option", clip.name);
    option.value = String(index);
    select.append(option);
  });
  select.value = activeClip ? "0" : "";
  const play = button(state.playing ? "Pause" : "Play");
  play.disabled = !activeClip;
  const zoomOut = button("−"), zoomIn = button("+"), reset = button("Reset view");
  zoomOut.setAttribute("aria-label", "Zoom out");
  zoomIn.setAttribute("aria-label", "Zoom in");
  if (clips.length) toolbar.append(select, play);
  toolbar.append(zoomOut, zoomIn, reset);
  container.append(toolbar);
  const metadata = element("p", [data.vertices.toLocaleString() + " vertices",
    data.triangles.toLocaleString() + " triangles", renderer.name].join(" · "));
  metadata.className = "model-viewer-metadata";
  const source = element("p");
  source.className = "model-viewer-source";
  container.append(metadata, source);

  function showSource() {
    const reference = activeClip ? activeClip.asset : data.appearance;
    source.replaceChildren();
    if (reference) source.append(sourceControl({ path: reference }));
    if (!clips.length && rig.length) source.append(document.createTextNode(" · No compatible authored clip is available."));
  }
  showSource();
  const details = element("details");
  details.className = "model-viewer-details";
  details.append(element("summary", "Preview assets"));
  const assets = element("ul");
  for (const name of new Set([data.client_template, data.appearance, data.client_data, data.missing_hull_model,
    ...geometry.meshes.map(mesh => mesh.asset), ...clips.map(clip => clip.asset)].filter(Boolean))) {
    const entry = element("li");
    entry.append(copyControl(name, name, "Copy file reference", "copy-code"));
    assets.append(entry);
  }
  if (data.model_source) {
    const entry = element("li");
    entry.append(sourceControl(data.model_source));
    assets.append(entry);
  }
  for (const issue of data.issues || []) assets.append(element("li", issue));
  const display = renderer.name === "WebGL" && Object.keys(data.textures || {}).length
    ? "Base diffuse textures are used where available."
    : "Geometry uses neutral materials in this preview.";
  details.append(element("p", display), assets);
  container.append(details);

  function draw(now) {
    scheduled = 0;
    if (disposed) return;
    const sinceDraw = lastTime ? now - lastTime : 0;
    if (state.playing && activeClip && sinceDraw >= 32) {
      elapsed += sinceDraw / 1000;
      changed = true;
    }
    if (dirty || changed) {
      lastTime = now;
      const scale = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.max(1, Math.round(canvas.clientWidth * scale));
      const height = Math.max(1, Math.round(canvas.clientHeight * scale));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      if (changed) {
        if (activeClip && rig.length) {
          const frame = (elapsed * activeClip.fps) % activeClip.frame_count;
          const motion = transforms(absolutePose(rig, activeClip.tracks, frame), bindPose);
          for (const mesh of geometry.meshes) deform(mesh, motion);
        } else {
          for (const mesh of geometry.meshes) {
            mesh.currentPosition.set(mesh.positions);
            mesh.currentNormal.set(mesh.normals);
          }
        }
      }
      renderer.render(camera(state, canvas, geometry), changed);
      dirty = changed = false;
    }
    if (state.playing) scheduled = requestAnimationFrame(draw);
  }

  function zoom(factor) {
    state.zoom = Math.max(.55, Math.min(2, state.zoom * factor));
    schedule();
  }

  zoomIn.addEventListener("click", () => zoom(1.15));
  zoomOut.addEventListener("click", () => zoom(1 / 1.15));
  reset.addEventListener("click", () => {
    Object.assign(state, defaultCamera);
    schedule();
  });
  select.addEventListener("change", () => {
    activeClip = select.value === "" ? undefined : clips[Number(select.value)];
    state.playing = Boolean(activeClip);
    elapsed = lastTime = 0;
    changed = true;
    play.disabled = !activeClip;
    play.textContent = state.playing ? "Pause" : "Play";
    showSource();
    schedule();
  });
  play.addEventListener("click", () => {
    state.playing = !state.playing;
    lastTime = 0;
    play.textContent = state.playing ? "Pause" : "Play";
    schedule();
  });
  canvas.addEventListener("wheel", event => {
    if (document.activeElement !== canvas) return;
    event.preventDefault();
    zoom(Math.exp(-event.deltaY * .001));
  }, {passive: false});

  let pointer = null, previousX = 0, previousY = 0;
  canvas.addEventListener("pointerdown", event => {
    if (event.button !== 0 || pointer !== null) return;
    canvas.focus({preventScroll: true});
    canvas.setPointerCapture(event.pointerId);
    pointer = event.pointerId;
    previousX = event.clientX;
    previousY = event.clientY;
    canvas.style.cursor = "grabbing";
  });
  canvas.addEventListener("pointermove", event => {
    if (event.pointerId !== pointer) return;
    state.yaw += (event.clientX - previousX) / 105;
    state.pitch = Math.max(-1.45, Math.min(1.45, state.pitch + (event.clientY - previousY) / 105));
    previousX = event.clientX;
    previousY = event.clientY;
    schedule();
  });
  canvas.addEventListener("lostpointercapture", () => {
    pointer = null;
    canvas.style.cursor = "grab";
  });
  canvas.addEventListener("keydown", event => {
    const direction = {ArrowLeft: -.12, ArrowRight: .12, ArrowUp: -.12, ArrowDown: .12};
    if (event.key === "+" || event.key === "=") zoom(1.15);
    else if (event.key === "-") zoom(1 / 1.15);
    else if (Object.hasOwn(direction, event.key)) {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") state.yaw += direction[event.key];
      else state.pitch = Math.max(-1.45, Math.min(1.45, state.pitch + direction[event.key]));
    } else return;
    event.preventDefault();
    schedule();
  });
  const observer = new ResizeObserver(schedule);
  observer.observe(canvas);
  schedule();
  return () => {
    disposed = true;
    cancelAnimationFrame(scheduled);
    observer.disconnect();
    renderer.dispose();
  };
}
