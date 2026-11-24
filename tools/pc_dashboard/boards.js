// PCS motherboard renderer (2026-11-22, owner: "sexy, predator like", one system for many boards, generic fallback).
// A board is DATA (window.PCS_BOARDS[id]): outline size, parts, sensor bindings. One renderer draws every board in the
// same style, so a new board is a definition file, not new drawing code.
//
// Coordinates: board units (u). Apex: PDF points of its manual / photo trace (origin = top-left of the PCB in those
// units); generic ATX: 254 x 317 u, same proportions as 244 x 305 mm. Drawn at S px per unit.
//
// Part types:
//   sink   {pts | rect, heat, chamfer, light: "top"|"left"|"right"|"bottom", fins: true, logo:{text, at:[x,y], v, stack, size}}
//   socket {rect, ihs:[x0,y0,x1,y1], heat}
//   slot   {rect, kind: "dimm"|"pcie"|"m2"|"conn", heat, label, vertical}
//   block  {rect, rx}                    small parts (EPS, 24-pin, power stages)        add heat to shade it
//   ring   {c:[x,y], r}                  round parts (buttons)
//   slashes{at:[x,y], n, step:[dx,dy], vec:[dx,dy]}   RGB light slashes (accent)
//   text   {at:[x,y], text, size, v, spacing}         silk-screen / shroud lettering
//   eye    {pts}                         decorative shroud cut (accent line)
//   cuts   {at, n, step, vec, w}         black cut-in stripes in a heatsink (the white Apex's slashes)
//   caps   {pts:[[x,y]..], r}            black capacitors
//   finpoly{pts}                         a shape filled with short slanted fins (the ROG eye on the chipset cover)
//   stencil{at, text, size, v}           segmented stencil lettering
//   rog    {at:[cx,cy], h, rot}          the ROG wordmark as on the Apex VRM heatsink: angular glyphs cut from slanted slots
//   any slot / block / socket may carry black: true (black plastic: DIMM, PCIe, 24-pin, stages, socket frame)
// Live tags: {at:[x,y], k: label, v: path, f: format, sub: [path, format], heat: [warm, hot, crit] | true, align, size}
//   paths read the PC detail tree (cpu.pkg_t, dimm.2.t, ...); "dimm.@0.t" = the first DIMM present; "_pch", "_eps"
//   are derived (hottest chipset sensor, estimated 8-pin W).
// Headers: {hw: detail fan key, name, short, rect, side: "t"|"r"} wired to rotors outside the board. A board without
// headers (generic) gets a rail of every fan the sensors report.
(function () {
  let S = 2;                                   // px per board unit, per board (def.s), set by draw / update
  const M = { l: 14, t: 64, r: 170, b: 16 };
  const NS = "http://www.w3.org/2000/svg";
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function geom(def) {
    const [ox, oy] = def.origin || [0, 0];
    const X = x => M.l + (x - ox) * S, Y = y => M.t + (y - oy) * S;
    return { X, Y, W: M.l + def.w * S + M.r, H: M.t + def.h * S + M.b, ox, oy };
  }
  const rectPts = ([x0, y0, x1, y1], c = 0) => c ? [[x0 + c, y0], [x1 - c, y0], [x1, y0 + c], [x1, y1 - c], [x1 - c, y1], [x0 + c, y1], [x0, y1 - c], [x0, y0 + c]]
                                                  : [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
  const poly = (g, pts) => "M" + pts.map(([x, y]) => `${g.X(x).toFixed(1)} ${g.Y(y).toFixed(1)}`).join("L") + "Z";
  const bbox = pts => { const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]); return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]; };

  function get(d, path) {
    if (!path) return null;
    if (path === "_pch") { const p = d.pch || {}; const v = ["t", "t2", "t3", "t4"].map(k => p[k]).filter(v => v != null); return v.length ? Math.max(...v) : null; }
    if (path === "_eps") { const w = (d.oc || {}).vr_w ?? (d.vrm || {}).w; return w == null ? null : w / 0.9; }
    let o = d;
    for (const k of path.split(".")) {
      if (o == null) return null;
      if (k[0] === "@") { const keys = Object.keys(o).sort(); o = o[keys[+k.slice(1)]]; }
      else o = o[k];
    }
    return typeof o === "number" ? o : null;
  }
  const FMT = {
    deg0: v => Math.round(v) + "°", deg1: v => v.toFixed(1) + "°", w0: v => Math.round(v) + " W", approxW: v => "≈ " + Math.round(v) + " W",
    v3: v => v.toFixed(3) + " V", c0: v => Math.round(v) + " °C", rpm: v => Math.round(v) + " rpm", pct: v => Math.round(v) + " %",
  };
  const fmt = (v, f) => v == null ? "--" : (FMT[f] || FMT.deg0)(v);
  function heatColor(t, th) {
    if (t == null) return null;
    const [a, b, c] = Array.isArray(th) ? th : [55, 75, 88];
    return t < a ? "var(--green)" : t < b ? "var(--yellow)" : t < c ? "var(--orange)" : "var(--red)";
  }

  // ---------------------------------------------------------------- static drawing
  function draw(svg, def, opts = {}) {
    S = def.s || 2;
    svg.classList.toggle("bd-isphoto", !!(def.image || def.art));
    const g = geom(def), o = [], uid = "b" + Math.random().toString(36).slice(2, 7);
    const thumb = !!opts.thumb;
    svg.setAttribute("viewBox", thumb ? `${M.l - 6} ${M.t - 6} ${def.w * S + 12} ${def.h * S + 12}` : `0 0 ${g.W} ${g.H}`);
    o.push(`<defs>
      <pattern id="${uid}dots" width="8" height="8" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".7" fill="currentColor"/></pattern>
      <pattern id="${uid}fins" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)"><line x1="0" y1="0" x2="0" y2="5" stroke="currentColor" stroke-width="1"/></pattern>
      <pattern id="${uid}eye" width="9" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(-32)"><rect x="0" y="1.5" width="6.5" height="2.4" rx=".8" fill="currentColor"/></pattern>
      <pattern id="${uid}slot" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(-38)"><rect x="0" y="0" width="7" height="3.6" fill="currentColor"/></pattern>
      <pattern id="${uid}slot2" width="6.5" height="6.5" patternUnits="userSpaceOnUse" patternTransform="rotate(-52)"><rect x="0" y="0" width="6.5" height="3.2" rx=".6" fill="currentColor"/></pattern>
      <linearGradient id="${uid}sink" x1="0" y1="0" x2="1" y2="1"><stop offset="0" class="bd-s0"/><stop offset="1" class="bd-s1"/></linearGradient>
      <filter id="${uid}glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
    </defs>`);
    // PCB: chamfered outline, inner line, faint dot texture
    const edge = rectPts([g.ox, g.oy, g.ox + def.w, g.oy + def.h], def.chamfer ?? 8);
    if (def.art) {                     // vector drawing per theme (owner's SVGs), both on the same coordinates
      const at = `x="${g.X(g.ox)}" y="${g.Y(g.oy)}" width="${def.w * S}" height="${def.h * S}" preserveAspectRatio="none"`;
      o.push(`<image href="${def.art.light}" ${at} class="bd-art bd-art-l"/><image href="${def.art.dark}" ${at} class="bd-art bd-art-d"/>`);
      o.push(`<g class="bd-heat" data-heat></g>`);
    } else if (def.image) {
      o.push(`<image href="${def.image}" x="${g.X(g.ox)}" y="${g.Y(g.oy)}" width="${def.w * S}" height="${def.h * S}" class="bd-photo" preserveAspectRatio="none"/>`);
      o.push(`<g class="bd-heat" data-heat></g>`);
    } else {
    o.push(`<path d="${poly(g, edge)}" class="bd-pcb"/>`);
    o.push(`<path d="${poly(g, edge)}" fill="url(#${uid}dots)" class="bd-tex"/>`);
    o.push(`<path d="${poly(g, rectPts([g.ox + 3, g.oy + 3, g.ox + def.w - 3, g.oy + def.h - 3], (def.chamfer ?? 8) - 2))}" class="bd-inner"/>`);
    o.push(`<g class="bd-heat" data-heat></g>`);
    }
    for (const [i, p] of (def.image || def.art ? [] : def.parts).entries()) {
      const id = `${uid}p${i}`;
      if (p.t === "sink") {
        const pts = p.pts || rectPts(p.rect, p.chamfer ?? 4), d = poly(g, pts);
        o.push(`<path d="${d}" class="bd-sink" fill="url(#${uid}sink)" id="${id}"/>`);
        if (p.fins !== false) o.push(`<path d="${d}" fill="url(#${uid}fins)" class="bd-fins"/>`);
        if (p.light) {
          const [x0, y0, x1, y1] = bbox(pts), inset = 4;
          const L = { top: [x0 + inset, y0 + 2.5, x1 - inset, y0 + 2.5], bottom: [x0 + inset, y1 - 2.5, x1 - inset, y1 - 2.5],
                      left: [x0 + 2.5, y0 + inset, x0 + 2.5, y1 - inset], right: [x1 - 2.5, y0 + inset, x1 - 2.5, y1 - inset] }[p.light];
          o.push(`<line x1="${g.X(L[0])}" y1="${g.Y(L[1])}" x2="${g.X(L[2])}" y2="${g.Y(L[3])}" class="bd-light" filter="url(#${uid}glow)"/>`);
        }
        if (p.logo) {
          const L = p.logo, size = (L.size || 14) * S;
          if (L.stack) L.text.split("").forEach((ch, k) =>
            o.push(`<text x="${g.X(L.at[0])}" y="${g.Y(L.at[1]) + k * size * 1.05}" class="bd-logo" style="font-size:${size}px" text-anchor="middle">${esc(ch)}</text>`));
          else o.push(`<text transform="translate(${g.X(L.at[0])} ${g.Y(L.at[1])})${L.v ? " rotate(90)" : ""}" class="bd-logo" style="font-size:${size}px;letter-spacing:${L.spacing ?? .18}em" text-anchor="middle">${esc(L.text)}</text>`);
        }
      } else if (p.t === "socket") {
        const [x0, y0, x1, y1] = p.rect;
        o.push(`<path d="${poly(g, rectPts(p.rect, 3))}" class="bd-socket${p.black ? " bd-black" : ""}"/>`);
        o.push(`<path d="${poly(g, rectPts(p.ihs, 2))}" class="bd-ihs" id="${uid}ihs"/>`);
        const c = 7, br = [[x0 - 2, y0 - 2, 1, 1], [x1 + 2, y0 - 2, -1, 1], [x0 - 2, y1 + 2, 1, -1], [x1 + 2, y1 + 2, -1, -1]];   // reticle corners
        br.forEach(([x, y, sx, sy]) => o.push(`<path d="M${g.X(x + sx * c)} ${g.Y(y)} H${g.X(x)} V${g.Y(y + sy * c)}" class="bd-reticle"/>`));
        if (!thumb) o.push(`<text x="${g.X((p.ihs[0] + p.ihs[2]) / 2)}" y="${g.Y(p.ihs[1]) - 5}" class="bd-silk" text-anchor="middle">${esc(p.label || "CPU")}</text>`);
      } else if (p.t === "slot") {
        const r = p.kind === "m2" ? 3 : 1.5;
        o.push(`<path d="${poly(g, rectPts(p.rect, r))}" class="bd-slot bd-${p.kind}${p.black ? " bd-black" : ""}" id="${id}"/>`);
        if (p.kind === "dimm") { const [x0, y0, x1, y1] = p.rect; o.push(`<line x1="${g.X((x0 + x1) / 2)}" y1="${g.Y(y0 + 4)}" x2="${g.X((x0 + x1) / 2)}" y2="${g.Y(y1 - 4)}" class="bd-key"/>`); }
        if (p.kind === "pcie") { const [x0, y0, x1, y1] = p.rect; o.push(`<line x1="${g.X(x0 + 3)}" y1="${g.Y((y0 + y1) / 2)}" x2="${g.X(x1 - 3)}" y2="${g.Y((y0 + y1) / 2)}" class="bd-key"/>`); }
      } else if (p.t === "block") {
        o.push(`<path d="${poly(g, rectPts(p.rect, p.rx ?? 1))}" class="bd-block${p.black ? " bd-black" : ""}" id="${id}"/>`);
      } else if (p.t === "ring") {
        o.push(`<circle cx="${g.X(p.c[0])}" cy="${g.Y(p.c[1])}" r="${p.r * S}" class="bd-block"/>`);
      } else if (p.t === "slashes") {
        for (let k = 0; k < p.n; k++) {
          const x = p.at[0] + k * p.step[0], y = p.at[1] + k * p.step[1];
          o.push(`<line x1="${g.X(x)}" y1="${g.Y(y)}" x2="${g.X(x + p.vec[0])}" y2="${g.Y(y + p.vec[1])}" class="bd-slash" filter="url(#${uid}glow)"/>`);
        }
      } else if (p.t === "text") {
        o.push(`<text transform="translate(${g.X(p.at[0])} ${g.Y(p.at[1])})${p.v ? " rotate(90)" : ""}" class="bd-letter" style="font-size:${(p.size || 10) * S}px;letter-spacing:${p.spacing ?? .3}em" text-anchor="middle">${esc(p.text)}</text>`);
      } else if (p.t === "cuts") {
        for (let k = 0; k < p.n; k++) {
          const x = p.at[0] + k * p.step[0], y = p.at[1] + k * p.step[1];
          o.push(`<line x1="${g.X(x)}" y1="${g.Y(y)}" x2="${g.X(x + p.vec[0])}" y2="${g.Y(y + p.vec[1])}" class="bd-cut" style="stroke-width:${(p.w || 2.2) * S}px"/>`);
        }
      } else if (p.t === "caps") {
        p.pts.forEach(([x, y]) => o.push(`<circle cx="${g.X(x)}" cy="${g.Y(y)}" r="${p.r * S}" class="bd-cap"/><circle cx="${g.X(x)}" cy="${g.Y(y)}" r="${p.r * S * .45}" class="bd-capin"/>`));
      } else if (p.t === "finpoly") {
        const dd = poly(g, p.pts) + (p.holes || []).map(h => poly(g, h)).join("");
        o.push(`<path d="${dd}" fill-rule="evenodd" fill="url(#${uid}${p.slots ? "slot2" : "eye"})" class="bd-eyefins"/><path d="${dd}" fill-rule="evenodd" class="bd-eyeline"/>`);
      } else if (p.t === "stencil") {
        o.push(`<text transform="translate(${g.X(p.at[0])} ${g.Y(p.at[1])})${p.v ? ` rotate(${p.v === true ? 90 : p.v})` : ""}" class="bd-stencil" style="font-size:${(p.size || 20) * S}px;letter-spacing:${p.spacing ?? .08}em" text-anchor="middle">${esc(p.text)}</text>`);
      } else if (p.t === "rog") {
        // glyphs in a 0..90 x 0..36 box (R, O, G: 26 wide, 6 apart), even-odd for the counters
        const k = (p.h || 32) / 36;
        const G = ["M0 0H22L26 4V16L22 20H17L26 36H17L10 20H8V36H0Z M8 7H18V13H8Z",
                   "M4 0H22L26 4V32L22 36H4L0 32V4Z M8 8H18V28H8Z",
                   "M4 0H26V8H8V28H18V23H13V16H26V32L22 36H4L0 32V4Z"];
        // stack: upright letters one under the other (the Apex VRM heatsink); else one line, rotated by rot
        const place = p.stack ? G.map((d, i) => [d, `translate(${g.X(p.at[0])} ${g.Y(p.at[1])}) scale(${S * k}) translate(-13 ${-63 + i * 44})`])
                              : [[G.map((d, i) => d.replace(/([MHL])(-?[\d.]+)/g, (m, c, n) => c === "H" || c === "L" || c === "M" ? c + (+n + i * 32) : m)).join(" "),
                                  `translate(${g.X(p.at[0])} ${g.Y(p.at[1])}) rotate(${p.rot ?? 0}) scale(${S * k}) translate(-45 -18)`]];
        place.forEach(([d, tr]) => {
          o.push(`<path d="${d}" transform="${tr}" fill-rule="evenodd" fill="url(#${uid}slot)" class="bd-rogfill"/>`);
          o.push(`<path d="${d}" transform="${tr}" fill-rule="evenodd" class="bd-rogline"/>`);
        });
      } else if (p.t === "seam") {
        o.push(`<line x1="${g.X(p.from[0])}" y1="${g.Y(p.from[1])}" x2="${g.X(p.to[0])}" y2="${g.Y(p.to[1])}" class="bd-seam"/>`);
      } else if (p.t === "plate") {
        o.push(`<path d="${poly(g, p.pts)}" class="bd-plate"/>`);
      } else if (p.t === "eye") {
        o.push(`<path d="${poly(g, p.pts)}" class="bd-eye" filter="url(#${uid}glow)"/>`);
      }
    }
    // fan headers + rotor rail
    const fans = [];
    if (!thumb) {
      if (def.headers && def.headers.length) {
        const top = def.headers.filter(h => h.side === "t"), right = def.headers.filter(h => h.side !== "t");
        top.forEach((h, k) => {
          const [x0, y0, x1] = h.rect, rx = g.X(h.railX ?? (x0 + x1) / 2), ry = 26;
          o.push(`<path d="${poly(g, rectPts(h.rect, .8))}" class="bd-hdr" data-hdr="${h.name}"/>`);
          o.push(`<path d="M${g.X((x0 + x1) / 2)} ${g.Y(y0)} V${ry + 22} H${rx} V${ry + 13}" class="bd-lead" data-lead="${h.name}"/>`);
          fans.push({ h, cx: rx, cy: ry, side: "t" });
        });
        right.forEach(h => {
          const [, y0, x1, y1] = h.rect, cy = g.Y((y0 + y1) / 2), cx = M.l + def.w * S + 26;
          o.push(`<path d="${poly(g, rectPts(h.rect, .8))}" class="bd-hdr" data-hdr="${h.name}"/>`);
          o.push(`<path d="M${g.X(x1)} ${cy} H${cx - 12}" class="bd-lead" data-lead="${h.name}"/>`);
          fans.push({ h, cx, cy, side: "r" });
        });
      }
    }
    fans.forEach(f => o.push(rotor(f.h.name, f.cx, f.cy)));
    o.push(`<g data-rail></g><g data-tags></g><g data-hit></g>`);
    svg.innerHTML = o.join("");
    svg.__board = { def, g, uid, fans, thumb, rpm: {} };
    if (!thumb) drawTags(svg);
  }

  function rotor(name, cx, cy, r = 9) {
    let b = "";
    for (let k = 0; k < 5; k++) b += `<path d="M0 0 C ${r * .25} ${-r * .35} ${r * .55} ${-r * .95} 0 ${-r} C ${-r * .35} ${-r * .8} ${-r * .3} ${-r * .3} 0 0Z" transform="rotate(${k * 72})"/>`;
    return `<g transform="translate(${cx} ${cy})" class="bd-rotor" data-rotor="${name}"><circle r="${r + 3}" class="bd-rring"/><g class="bd-spin">${b}</g><circle r="${r * .28}" class="bd-hub"/></g>`;
  }

  const Y_K = { l: -26, m: -12, s: -6 }, Y_V = { l: 8, m: 7, s: 8 }, Y_S = { l: 26, m: 21, s: 21 };
  // tags: label + value on their own backing, placed by the definition, sized after the text is set
  function drawTags(svg) {
    const B = svg.__board, { def, g } = B, o = [];
    (def.tags || []).forEach((t, i) => {
      const x = g.X(t.at[0]), y = g.Y(t.at[1]), al = t.align || "middle", sz = t.size || "m";
      o.push(`<g class="bd-tag bd-tag-${sz}" data-tag="${i}" transform="translate(${x} ${y})${t.rot ? ` rotate(${t.rot})` : ""}">
        <rect class="bd-tagbg" rx="6"/>
        ${t.k ? `<text class="bd-k" text-anchor="${al}" y="${Y_K[sz]}">${esc(t.k)}</text>` : ""}
        <text class="bd-v" text-anchor="${al}" y="${t.k ? Y_V[sz] : Y_V[sz] - 6}">--</text>
        ${t.sub ? `<text class="bd-sub" text-anchor="${al}" y="${Y_S[sz]}"></text>` : ""}</g>`);
    });
    svg.querySelector("[data-tags]").innerHTML = o.join("");
  }

  // ---------------------------------------------------------------- live update
  function update(svg, d, opts = {}) {
    const B = svg.__board;
    if (!B || B.thumb) return;
    const { def, g } = B;
    S = def.s || 2;
    d = d || {};
    // heat shading on the parts that carry a sensor
    let h = "";
    def.parts.forEach(p => {
      if (!p.heat) return;
      const t = get(d, p.heat), c = heatColor(t, p.th);
      if (!c || c === "var(--green)") return;                 // cool metal stays dark; shading starts when it warms up
      const pts = p.t === "socket" ? rectPts(p.ihs, 2) : p.pts || rectPts(p.rect, p.t === "sink" ? (p.chamfer ?? 4) : p.t === "zone" ? 6 : 1.2);
      h += p.t === "zone" ? `<path d="${poly(g, pts)}" fill="${c}" fill-opacity=".22" stroke="${c}" stroke-opacity=".85" stroke-width="1.6" class="bd-zone"/>`
                          : `<path d="${poly(g, pts)}" fill="${c}" fill-opacity="${p.t === "sink" ? .14 : .28}" stroke="${c}" stroke-opacity=".7" stroke-width="1.4"/>`;
    });
    svg.querySelector("[data-heat]").innerHTML = h;
    // tags
    (def.tags || []).forEach((t, i) => {
      const el = svg.querySelector(`[data-tag="${i}"]`);
      if (!el) return;
      const v = get(d, t.v), vt = el.querySelector(".bd-v");
      vt.textContent = fmt(v, t.f);
      vt.style.fill = t.heat ? (heatColor(v, t.heat) || "") : "";
      const st = el.querySelector(".bd-sub");
      if (st && t.sub) st.textContent = (t.sub[2] || "") + fmt(get(d, t.sub[0]), t.sub[1]);
      const bb = el.getBBox ? (() => { const r = el.querySelector("rect"); r.setAttribute("width", 0); return el.getBBox(); })() : null;
      if (bb && bb.width) {
        const r = el.querySelector("rect");
        r.setAttribute("x", bb.x - 7); r.setAttribute("y", bb.y - 4); r.setAttribute("width", bb.width + 14); r.setAttribute("height", bb.height + 8);
      }
    });
    // fans: board headers, or a rail of every fan the sensors report (generic boards)
    const fan = d.fan || {}, roles = opts.roles || {};
    let fans = B.fans;
    if (!def.headers || !def.headers.length) {
      const keys = Object.keys(fan).filter(k => fan[k] != null);
      const sig = keys.join("|");
      if (sig !== B.railSig) {
        B.railSig = sig;
        const x = M.l + def.w * S + 26, y0 = M.t + 20, step = Math.min(46, (def.h * S - 40) / Math.max(1, keys.length));
        B.fans = fans = keys.map((k, n) => ({ h: { hw: k, name: k, short: k.replace("Chassis", "CHA").replace("W_PUMP+", "PUMP+") }, cx: x, cy: y0 + n * step, side: "r" }));
        svg.querySelector("[data-rail]").innerHTML = fans.map(f => rotor(f.h.name, f.cx, f.cy)).join("");
      }
    }
    let lab = "";
    fans.forEach(f => {
      const rpm = f.h.hw ? fan[f.h.hw] : null, on = rpm != null && rpm > 0, role = roles[f.h.name] || f.h.role || "";
      const ro = svg.querySelector(`[data-rotor="${CSS.escape(f.h.name)}"]`);
      if (ro) {
        ro.classList.toggle("on", on);
        const sp = ro.querySelector(".bd-spin"), prev = B.rpm[f.h.name];
        if (on && (prev == null || Math.abs(rpm - prev) / prev > .08)) { sp.style.animationDuration = Math.max(.12, Math.min(3, 420 / rpm)).toFixed(2) + "s"; B.rpm[f.h.name] = rpm; }
        sp.style.animationPlayState = on ? "running" : "paused";
      }
      svg.querySelectorAll(`[data-hdr="${CSS.escape(f.h.name)}"],[data-lead="${CSS.escape(f.h.name)}"]`).forEach(e => e.classList.toggle("on", on));
      const val = `<tspan class="bd-rpm">${rpm == null ? "--" : Math.round(rpm)}</tspan><tspan class="bd-unit"> rpm</tspan>`;
      if (f.side === "t") lab += `<text x="${f.cx}" y="${f.cy - 18}" class="bd-fan${on ? " on" : ""}" text-anchor="middle">${esc(f.h.short)} ${val}</text>`;
      else lab += `<text x="${f.cx + 18}" y="${f.cy - 1}" class="bd-fan${on ? " on" : ""}">${esc(f.h.short)} ${val}</text>` +
                  (role ? `<text x="${f.cx + 18}" y="${f.cy + 12}" class="bd-role">${esc(role)}</text>` : "");
      if (f.side === "t" && role) lab += `<text x="${f.cx}" y="${f.cy + 28}" class="bd-role" text-anchor="middle"></text>`;
    });
    let rail = svg.querySelector("[data-rail-lab]");
    if (!rail) { rail = document.createElementNS(NS, "g"); rail.setAttribute("data-rail-lab", ""); svg.appendChild(rail); }
    rail.innerHTML = lab;
    // tooltips
    if (opts.tips) svg.querySelector("[data-hit]").innerHTML = opts.tips(g, d, poly, rectPts);
  }

  // ---------------------------------------------------------------- boards
  const R = (x0, y0, x1, y1) => [x0, y0, x1, y1];

  // ROG Maximus Z890 Apex (owner 2026-11-23: his vector drawings, one per theme, boards/asus-rog-maximus-z890-apex-light.svg
  // / -dark.svg; cleaned by boards/svgclean.py, the dark one moved onto the light one's coordinates by edge matching).
  // Units = the drawing's px, board at x 12..776, y 27..987. Zones mark where heat shows; headers from the manual
  // (page 18). To add a board the same way: draw it (light + dark), svgclean.py both onto one box, place zones / tags /
  // headers in those units.
  const apexArt = {
    id: "asus-rog-maximus-z890-apex", name: "ROG Maximus Z890 Apex", vendor: "ASUS", form: "ATX",
    match: [/maximus\s+z890\s+apex/i], art: { light: "/boards/asus-rog-maximus-z890-apex-light.svg", dark: "/boards/asus-rog-maximus-z890-apex-dark.svg" },
    origin: [12, 27], w: 764, h: 960, s: .66,
    parts: [
      { t: "zone", rect: R(381, 248, 489, 395), heat: "cpu.pkg_t", th: [60, 80, 92] },
      { t: "zone", rect: R(122, 120, 278, 497), heat: "cpu.vrm_t" },
      { t: "zone", rect: R(232, 43, 554, 182), heat: "cpu.vrm_t" },
      { t: "zone", rect: R(580, 91, 605, 533), heat: "dimm.2.t", th: [45, 55, 65] },
      { t: "zone", rect: R(614, 91, 639, 533), heat: "dimm.0.t", th: [45, 55, 65] },
      { t: "zone", rect: R(104, 507, 504, 584), heat: "ssd.t", th: [55, 65, 72] },
      { t: "zone", rect: R(99, 699, 725, 890), heat: "_pch", th: [55, 65, 75] },
      { t: "zone", rect: R(673, 923, 695, 940), heat: "mb.tsen", th: [25, 32, 38] },
    ],
    tags: [
      { at: [434, 328], k: "CPU", v: "cpu.pkg_t", f: "deg0", heat: [60, 80, 92], size: "l", sub: ["cpu.pkg_w", "w0"] },
      { at: [200, 463], k: "VRM", v: "cpu.vrm_t", f: "deg0", heat: true, sub: ["vrm.w", "w0"], size: "s" },
      { at: [258, 102], v: "_eps", f: "approxW", size: "s" },
      { at: [246, 809], k: "Z890", v: "_pch", f: "deg0", heat: [55, 65, 75] },
      { at: [609, 563], k: "DIMM A · B", v: "dimm.2.t", f: "deg1", heat: [45, 55, 65], sub: ["dimm.0.t", "deg1", "B "], size: "s" },
      { at: [180, 545], k: "M.2_1 SSD", v: "ssd.t", f: "deg0", heat: [55, 65, 72], size: "s" },
      { at: [332, 672], k: "GPU", v: "gpu.t", f: "deg0", sub: ["gpu.w", "w0"], size: "s" },
      { at: [658, 917], k: "Cube", v: "mb.tsen", f: "c0", heat: [25, 32, 38], align: "end", size: "s" },
    ],
    headers: [
      { hw: "CPU", name: "CPU_FAN", short: "CPU", rect: R(430, 68, 458, 83), side: "t", railX: 416 },
      { hw: "Chassis1", name: "CHA_FAN1", short: "CHA1", rect: R(537, 68, 554, 83), side: "t", railX: 541 },
      { hw: "AIO Pump", name: "AIO_PUMP", short: "AIO", rect: R(658, 68, 694, 83), side: "t", railX: 673 },
      { hw: "Extra Flow Fan", name: "EF_FAN", short: "EF", rect: R(732, 569, 750, 609), side: "r" },
      { hw: "W_PUMP+", name: "W_PUMP+", short: "W_PUMP+", rect: R(713, 923, 750, 940), side: "r" },
      { hw: "Chassis2", name: "CHA_FAN2", short: "CHA2", rect: R(684, 958, 721, 975), side: "r" },
    ],
  };


  // Generic ATX: standard part positions, no vendor marks; fans come from the sensors (rail)
  const generic = {
    id: "generic-atx", name: "Generic ATX board", vendor: "", form: "ATX", generic: true, w: 254, h: 317, chamfer: 9,
    parts: [
      { t: "sink", rect: R(0, 0, 30, 168), chamfer: 5, light: "right", fins: false },
      { t: "slashes", at: [8, 120], n: 4, step: [0, 10], vec: [14, -5] },
      { t: "sink", pts: [[30, 22], [86, 22], [90, 26], [90, 150], [30, 150]], heat: "cpu.vrm_t", light: "top" },
      { t: "sink", pts: [[100, 6], [186, 6], [186, 44], [110, 44], [100, 34]], heat: "cpu.vrm_t", light: "bottom" },
      { t: "block", rect: R(62, 9, 80, 19), rx: 2 },
      { t: "socket", rect: R(112, 58, 170, 132), ihs: R(120, 74, 162, 124), heat: "cpu.pkg_t", th: [60, 80, 92], label: "SOCKET" },
      ...[0, 1, 2, 3].map(i => ({ t: "slot", kind: "dimm", rect: R(188 + i * 10.5, 10, 196 + i * 10.5, 170), heat: `dimm.@${Math.floor(i / 2)}.t`, th: [45, 55, 65] })),
      { t: "slot", kind: "conn", rect: R(238, 70, 249, 130) },
      { t: "sink", rect: R(30, 160, 160, 178), chamfer: 3, heat: "ssd.t", th: [55, 65, 72], light: "top" },
      { t: "slot", kind: "pcie", rect: R(40, 190, 172, 200) },
      { t: "sink", rect: R(30, 214, 160, 232), chamfer: 3, light: "top" },
      { t: "sink", pts: [[176, 214], [244, 214], [244, 300], [196, 300], [176, 280]], heat: "_pch", th: [55, 65, 75] },
      { t: "slot", kind: "pcie", rect: R(40, 250, 172, 258) },
      { t: "slot", kind: "pcie", rect: R(40, 286, 120, 294) },
    ],
    tags: [
      { at: [141, 102], k: "CPU", v: "cpu.pkg_t", f: "deg0", heat: [60, 80, 92], size: "l", sub: ["cpu.pkg_w", "w0"] },
      { at: [60, 96], k: "VRM", v: "cpu.vrm_t", f: "deg0", heat: true },
      { at: [210, 262], k: "Chipset", v: "_pch", f: "deg0", heat: [55, 65, 75] },
      { at: [209, 184], k: "DIMM", v: "dimm.@0.t", f: "deg1", heat: [45, 55, 65], sub: ["dimm.@1.t", "deg1", "B "], size: "s" },
      { at: [150, 171], k: "SSD", v: "ssd.t", f: "deg0", heat: [55, 65, 72], align: "end", size: "s" },
      { at: [106, 195], k: "GPU", v: "gpu.t", f: "deg0", sub: ["gpu.w", "w0"], size: "s" },
    ],
    headers: [],
  };

  apexArt.socket = "LGA1851"; apexArt.chipset = "Z890";
  window.PCS_BOARDS = { [apexArt.id]: apexArt };

  // Catalog of owner's drawings (2026-11-24, docs/boards → boards/import_boards.py → boards/<id>-light/-dark.svg).
  // [id, name, vendor, socket, chipset, match on "maker · product", box from import_boards.py, socket centre as a
  // fraction of the box (read off a gridded contact sheet, ±3 %)]. Only the CPU zone and generic tag spots so far
  // (VRM left of the socket, DIMMs right, chipset bottom right): ASSUMED until each board gets its own trace; fan
  // headers come from the sensors (rail).
  const CATALOG = [
    ["asus-b550-f", "ROG Strix B550-F Gaming", "ASUS", "AM4", "B550", /strix\s+b550-f\b/i, [27.3, 28.1, 791.4, 1014], [.58, .32]],
    ["asus-b650-a", "ROG Strix B650-A Gaming WiFi", "ASUS", "AM5", "B650", /strix\s+b650-a\b/i, [17.8, 20.1, 796, 986], [.51, .30]],
    ["asus-crosshair-x870e-hero", "ROG Crosshair X870E Hero", "ASUS", "AM5", "X870E", /crosshair\s+x870e\s+hero/i, [16.4, 11.8, 791.2, 997], [.57, .29]],
    ["asus-maximus-z790-hero", "ROG Maximus Z790 Hero", "ASUS", "LGA1700", "Z790", /maximus\s+z790\s+hero/i, [21.2, 8.7, 781.3, 970.2], [.55, .27]],
    ["asus-p8z68-v-pro", "P8Z68-V Pro", "ASUS", "LGA1155", "Z68", /p8z68-v\s*pro/i, [20, 15, 789, 972], [.51, .27]],
    ["asus-tuf-b650-plus", "TUF Gaming B650-Plus", "ASUS", "AM5", "B650", /tuf.*b650-plus/i, [48.3, 21.4, 774.1, 993.4], [.55, .30]],
    ["asus-tuf-b760-plus", "TUF Gaming B760-Plus", "ASUS", "LGA1700", "B760", /tuf.*b760-plus/i, [30, 13.9, 781, 997.3], [.55, .29]],
    ["asus-tuf-b850-plus", "TUF Gaming B850-Plus", "ASUS", "AM5", "B850", /tuf.*b850-plus/i, [21.4, 16.5, 776.7, 972], [.53, .31]],
    ["asus-tuf-z790-plus", "TUF Gaming Z790-Plus", "ASUS", "LGA1700", "Z790", /tuf.*z790-plus/i, [21, 18.7, 759.9, 976], [.51, .25]],
    ["asus-x670e-e", "ROG Strix X670E-E Gaming WiFi", "ASUS", "AM5", "X670E", /strix\s+x670e-e\b/i, [19.8, 12.8, 775.1, 953], [.53, .27]],
    ["asus-x870e-e", "ROG Strix X870E-E Gaming WiFi", "ASUS", "AM5", "X870E", /strix\s+x870e-e\b/i, [21, 20.9, 797.1, 977], [.54, .28]],
    ["asus-z390-e", "ROG Strix Z390-E Gaming", "ASUS", "LGA1151-2", "Z390", /strix\s+z390-e\b/i, [20.4, 13, 785.1, 997], [.53, .28]],
    ["asus-z790-e", "ROG Strix Z790-E Gaming WiFi", "ASUS", "LGA1700", "Z790", /strix\s+z790-e\b/i, [25, 17.8, 779.8, 973], [.56, .27]],
    ["gigabyte-b650-aorus-elite", "B650 Aorus Elite", "Gigabyte", "AM5", "B650", /b650\s+aorus\s+elite/i, [49.2, 21.8, 790.2, 969], [.53, .30]],
    ["gigabyte-b650-gaming-x-ax", "B650 Gaming X AX", "Gigabyte", "AM5", "B650", /b650\s+gaming\s+x\b/i, [16, 22.9, 786.5, 990], [.55, .28]],
    ["gigabyte-b850-aorus-elite", "B850 Aorus Elite", "Gigabyte", "AM5", "B850", /b850\s+aorus\s+elite/i, [19, 14.5, 789.6, 1000], [.56, .30]],
    ["gigabyte-x870e-aorus-master", "X870E Aorus Master", "Gigabyte", "AM5", "X870E", /x870e\s+aorus\s+master/i, [27, 12.1, 800, 967], [.51, .26]],
    ["gigabyte-z790-aorus-elite", "Z790 Aorus Elite", "Gigabyte", "LGA1700", "Z790", /z790\s+aorus\s+elite/i, [12.6, 19.4, 802.2, 1000], [.53, .26]],
    ["msi-b550-tomahawk", "MAG B550 Tomahawk", "MSI", "AM4", "B550", /b550\s+tomahawk/i, [19.4, 8.6, 808.1, 998], [.53, .28]],
    ["msi-b850-tomahawk-max", "MAG B850 Tomahawk Max", "MSI", "AM5", "B850", /b850\s+tomahawk/i, [16.7, 14.6, 782, 1005.1], [.56, .24]],
    ["msi-mpg-x870e-carbon", "MPG X870E Carbon WiFi", "MSI", "AM5", "X870E", /x870e\s+carbon/i, [29.1, 13, 770.3, 969.4], [.53, .24]],
    ["msi-x870-tomahawk", "MAG X870 Tomahawk WiFi", "MSI", "AM5", "X870", /x870\s+tomahawk/i, [15.7, 29.6, 790.5, 977.4], [.49, .26]],
    ["msi-z890-tomahawk", "MAG Z890 Tomahawk WiFi", "MSI", "LGA1851", "Z890", /z890\s+tomahawk/i, [30.6, 29.4, 1752.3, 2240.6], [.53, .29]],
    // 2026-11-24 second batch
    ["asus-tuf-z890-plus", "TUF Gaming Z890-Plus WiFi", "ASUS", "LGA1851", "Z890", /tuf.*z890-plus/i, [25.6, 13.1, 792.4, 994.2], [.53, .28]],
    ["asus-z790-apex-encore", "ROG Maximus Z790 Apex Encore", "ASUS", "LGA1700", "Z790", /z790\s+apex/i, [14.8, 16.8, 797.4, 968], [.53, .28]],
    ["asus-z890-hero", "ROG Maximus Z890 Hero", "ASUS", "LGA1851", "Z890", /maximus\s+z890\s+hero/i, [32.3, 30.4, 1802, 2213.2], [.53, .28]],
    ["gigabyte-b550-aorus-elite-v2", "B550 Aorus Elite V2", "Gigabyte", "AM4", "B550", /b550\s+aorus\s+elite/i, [24.2, 20.4, 790.2, 957], [.54, .31]],
    ["gigabyte-z890-aorus-elite", "Z890 Aorus Elite", "Gigabyte", "LGA1851", "Z890", /z890\s+aorus\s+elite/i, [24.2, 10.6, 796, 997], [.53, .28]],
  ];
  for (const [id, name, vendor, socket, chipset, match, [x0, y0, x1, y1], [cx, cy]] of CATALOG) {
    const w = x1 - x0, h = y1 - y0, P = (fx, fy) => [x0 + fx * w, y0 + fy * h], u = w / 764;   // u: Apex units
    const [sx, sy] = P(cx, cy), half = 60 * u;
    window.PCS_BOARDS[id] = {
      id, name, vendor, socket, chipset, form: "ATX", match: [match],
      art: { light: `/boards/${id}-light.svg`, dark: `/boards/${id}-dark.svg` },
      origin: [x0, y0], w, h, s: 504 / w,
      parts: [{ t: "zone", rect: R(sx - half, sy - half * 1.2, sx + half, sy + half * 1.2), heat: "cpu.pkg_t", th: [60, 80, 92] }],
      tags: [
        { at: [sx, sy], k: "CPU", v: "cpu.pkg_t", f: "deg0", heat: [60, 80, 92], size: "l", sub: ["cpu.pkg_w", "w0"] },
        { at: P(.2, cy + .04), k: "VRM", v: "cpu.vrm_t", f: "deg0", heat: true, size: "s" },
        { at: P(.83, .47), k: "DIMM", v: "dimm.@0.t", f: "deg1", heat: [45, 55, 65], sub: ["dimm.@1.t", "deg1", "B "], size: "s" },
        { at: P(.38, .5), k: "GPU", v: "gpu.t", f: "deg0", sub: ["gpu.w", "w0"], size: "s" },
        { at: P(.76, .8), k: chipset, v: "_pch", f: "deg0", heat: [55, 65, 75] },
      ],
      headers: [],
    };
  }
  generic.socket = "";
  window.PCS_BOARDS[generic.id] = generic;

  // CPU → socket (2026-11-24, setup asks for the board among those that fit the CPU). Windows' SMBIOS socket string
  // wins when it is one ("AM5", "LGA1700"; some boards say "CPU 1" / "U3E1"), else from the model name.
  function socketOf(cpu, reported) {
    const r = (reported || "").toUpperCase().replace(/\s+/g, "");
    if (/^(AM[45]|LGA\d{4}|SOCKETAM[45]|FCLGA\d{4})/.test(r)) {
      const s = r.replace(/^SOCKET/, "").replace(/^FC/, "");
      if (s !== "LGA1151") return s;                       // 6/7th vs 8/9th gen share the name: decide from the CPU
    }
    const n = (cpu || "").replace(/\(R\)|\(TM\)/gi, "");
    let m;
    if (/core\s*ultra\s*[579]\s*2\d\d/i.test(n)) return "LGA1851";
    if ((m = n.match(/i[3579]-(\d{4,5})/i))) {
      const v = m[1], gen = v.length === 5 ? +v.slice(0, 2) : +v[0];
      return gen >= 12 ? "LGA1700" : gen >= 10 ? "LGA1200" : gen >= 8 ? "LGA1151-2" : gen >= 6 ? "LGA1151" : gen >= 4 ? "LGA1150" : "LGA1155";
    }
    if ((m = n.match(/ryzen\s+(?:[3579]|threadripper)\s*(?:pro\s*)?(\d{4})/i))) {
      if (/threadripper/i.test(n)) return "sTR";
      return +m[1] >= 7000 ? "AM5" : "AM4";                // desktop 7000 / 8000G / 9000 = AM5, 1000..5000 = AM4
    }
    return r ? r : "";
  }

  window.PCSBoard = {
    draw, update, boards: window.PCS_BOARDS, socketOf,
    match(text) { for (const b of Object.values(window.PCS_BOARDS)) if (!b.hidden && (b.match || []).some(r => r.test(text || ""))) return b.id; return null; },
    list() { return Object.keys(window.PCS_BOARDS).filter(id => !window.PCS_BOARDS[id].hidden); },
  };
})();
