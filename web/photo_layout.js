// SPDX-FileCopyrightText: 2026 DoktorShift
// SPDX-License-Identifier: MIT
/* Photo group layout: the one engine behind every surface that shows a group
 * of pictures (docs/publishing-experience.md, "Photo groups").
 *
 * A plain script: the published pages inline it, and the app imports it
 * (src/utils/photoLayout.js). photo_layout.py takes the same steps in the
 * same order, so both give bit-identical results, the markup text included:
 * only +, -, *, /, min, max, floor and comparisons are used (no log, pow or
 * exp, whose last bit differs between libm builds), every sum runs left to
 * right, and ties keep the earlier candidate. shared/photo-layout-vectors.json
 * pins both engines (tools/photo-layout-vectors.mjs writes it).
 *
 * Input: the group's pictures in author order, `{ w, h, caption, wide }`.
 * `w` and `h` are pixel sizes (unknown or out of bounds: the provisional
 * 3:2), `caption` the caption's length in code points of its Markdown source
 * between the outer asterisks (0: none) and `wide` true when the address
 * ends in #wide ("Groß" in a group).
 * Output: `layout` gives the rows of the wide screens (a cell is a picture
 * index or a stack of indices), `phoneSegments` the runs of the narrow ones,
 * and `groupMarkup` with `groupHTML` the group's markup: one flat list in
 * author order, so a run of similar pictures is always one element.
 */
(function (global) {
  'use strict';
  if (global.E21PhotoLayout) return;

  const CONSTANTS = Object.freeze({
    version: 5,
    panorama: 2.4, // ratio at or above: a row of its own, full width
    tall: 0.4, // ratio at or below: a row of its own, height capped
    // A #wide picture is a row of its own too, inside its group, and as tall
    // as a lone #wide figure may be: min(px, vh * viewport height).
    wideCap: Object.freeze({ px: 900, vh: 0.85 }),
    provisional: Object.freeze([3, 2]), // shape assumed until the size is known
    // What the engine accepts, so a stranger's sizes cannot make its work or
    // its numbers unbounded. A picture side above maxSide pixels, or a shape
    // more than maxRatio times wider than tall (or taller than wide), counts
    // as unknown. A group of more than maxGroup pictures is not searched: each
    // picture gets a row of its own, and on a phone it stands alone.
    limits: Object.freeze({ maxSide: 65535, maxRatio: 32, maxGroup: 60 }),
    regular: Object.freeze({ width: 680, gap: 12, cap: 560, minSide: 0.16, captionWidth: 0.28, captionPerPx: 0.12, lowHeight: 0.3, highHeight: 1.0 }),
    // Narrow columns (phones; below 600 px). Pictures of a similar shape that
    // follow each other form a run: the widest shape at most `spread` times
    // the narrowest. A run is one picture alone, a row (when every picture
    // side by side still shows at least 1/spread of its size alone) or a
    // swipe strip of equal height. Readable minimum: no picture shows smaller
    // than 1/spread (5/8) of its size alone, min(width, cap * ratio). Height
    // cap: min(capPx, capVh * viewport height). A captioned picture is at
    // least captionMin wide (8em of 17 px). The row test runs once, at the
    // reference phone (container refWidth, viewport refViewport), so the
    // markup is static.
    phone: Object.freeze({ gap: 8, capPx: 520, capVh: 0.65, captionMin: 136, spread: 1.6, refWidth: 350, refViewport: 844 }),
    weights: Object.freeze({ empty: 3, sliver: 10, caption: 4, low: 1, high: 2, row: 0.15, balance: 0.04, deviation: 1 }),
    targetRow: 0.34, // justified rows (5 and more): preferred row height, share of the width
  });

  /** `[w, h]` when both are whole pixel sizes the engine accepts, else null. */
  function pictureSize(w, h) {
    const { maxSide, maxRatio } = CONSTANTS.limits;
    if (!Number.isInteger(w) || !Number.isInteger(h) || w < 1 || h < 1 || w > maxSide || h > maxSide) return null;
    if (w > h * maxRatio || h > w * maxRatio) return null;
    return [w, h];
  }

  function ratioOf(item) {
    const size = pictureSize(item.w, item.h);
    return size ? size[0] / size[1] : CONSTANTS.provisional[0] / CONSTANTS.provisional[1];
  }

  const captionLength = item => (Number.isInteger(item.caption) && item.caption > 0 ? item.caption : 0);

  /**
   * Cell ratio, internal gaps (k) and internal captions (c). A stack's ratio
   * is 1 / sum(1/r). Only a caption with a picture below it in the same stack
   * is internal; the last caption of a column hangs below the row, as a
   * single picture's does, so pictures in a row keep one height.
   */
  function cellInfo(cell, items) {
    if (typeof cell === 'number') return { r: ratioOf(items[cell]), k: 0, c: 0, figs: [cell] };
    let inverse = 0;
    let c = 0;
    for (let n = 0; n < cell.length; n++) {
      inverse = inverse + 1 / ratioOf(items[cell[n]]);
      if (n < cell.length - 1 && captionLength(items[cell[n]])) c = c + 1;
    }
    return { r: 1 / inverse, k: cell.length - 1, c, figs: cell };
  }

  /**
   * Geometry of one row at width W with gap g and picture-height cap.
   * Captions hang below their picture and are left out here.
   * Returns { height, width, tiles: [{ index, x, y, w, h }] }, y from the row top.
   */
  function rowGeometry(row, items, W, g, cap) {
    const cells = row.map(cell => cellInfo(cell, items));
    let sumR = 0;
    let sumRK = 0;
    for (const cell of cells) { sumR = sumR + cell.r; sumRK = sumRK + cell.r * cell.k; }
    const m = cells.length;
    let H = (W - (m - 1) * g + g * sumRK) / sumR;
    if (H > cap) H = cap;
    let width = (m - 1) * g;
    for (const cell of cells) width = width + cell.r * (H - cell.k * g);
    const tiles = [];
    let x = (W - width) / 2;
    for (const cell of cells) {
      const cw = cell.r * (H - cell.k * g);
      let y = 0;
      for (const index of cell.figs) {
        const th = cell.figs.length === 1 ? H : cw / ratioOf(items[index]);
        tiles.push({ index, x, y, w: cw, h: th });
        y = y + th + g;
      }
      x = x + cw + g;
    }
    return { height: H, width, tiles };
  }

  /**
   * Whether every caption inside a stack fits on one line, as the geometry
   * counts it: at most floor(tile width * captionPerPx) characters, measured
   * at the narrowest container of the wide layout (600 px) with the caption
   * font (0.833em of 17 px, about 7.1 px a character).
   */
  function insideCaptionsFit(row, items, ref) {
    const geo = rowGeometry(row, items, ref.width, ref.gap, ref.cap);
    let t = 0;
    for (const cell of row) {
      if (typeof cell === 'number') { t = t + 1; continue; }
      for (let n = 0; n < cell.length - 1; n++) {
        if (captionLength(items[cell[n]]) > Math.floor(geo.tiles[t + n].w * ref.captionPerPx)) return false;
      }
      t = t + cell.length;
    }
    return true;
  }

  /** Cost of a candidate (list of rows) at the reference size; lower is better. */
  function cost(rows, items, ref, weights) {
    const W = ref.width;
    let empty = 0;
    let sliver = 0;
    let captionShort = 0;
    let total = 0;
    let amin = 0;
    let amax = 0;
    let first = true;
    for (let n = 0; n < rows.length; n++) {
      const geo = rowGeometry(rows[n], items, W, ref.gap, ref.cap);
      empty = empty + ((W - geo.width) / W) * (geo.height / W);
      total = total + geo.height + (n > 0 ? ref.gap : 0);
      for (const tile of geo.tiles) {
        const side = Math.min(tile.w, tile.h) / W;
        if (side < ref.minSide) sliver = sliver + (ref.minSide - side);
        if (captionLength(items[tile.index]) && tile.w / W < ref.captionWidth) captionShort = captionShort + (ref.captionWidth - tile.w / W);
        const area = (tile.w / W) * (tile.h / W);
        if (first) { amin = area; amax = area; first = false; } else { amin = Math.min(amin, area); amax = Math.max(amax, area); }
      }
    }
    const t = total / W;
    const low = t < ref.lowHeight ? ref.lowHeight - t : 0;
    const high = t > ref.highHeight ? t - ref.highHeight : 0;
    return weights.empty * empty + weights.sliver * sliver + weights.caption * captionShort
      + weights.low * low + weights.high * high + weights.row * (rows.length - 1)
      + weights.balance * (amax / amin - 1);
  }

  /** Order-keeping templates for 2 to 4 pictures, in tie-break order. */
  function templates(s) {
    const [a, b, c, d] = s;
    if (s.length === 2) return [['pair', [[a, b]]], ['column', [[a], [b]]]];
    if (s.length === 3) {
      return [
        ['trio', [[a, b, c]]], ['lead-left', [[a, [b, c]]]], ['lead-right', [[[a, b], c]]],
        ['lead-top', [[a], [b, c]]], ['lead-bottom', [[a, b], [c]]],
      ];
    }
    return [
      ['grid', [[a, b], [c, d]]], ['lead-left', [[a, [b, c, d]]]], ['lead-right', [[[a, b, c], d]]],
      ['lead-top', [[a], [b, c, d]]], ['lead-bottom', [[a, b, c], [d]]], ['quartet', [[a, b, c, d]]],
    ];
  }

  /** Justified rows (5 and more pictures): least total cost, rows of 1 to 4. */
  function justified(s, items, ref, weights) {
    const W = ref.width;
    const best = [0];
    const from = [0];
    for (let i = 1; i <= s.length; i++) {
      let chosen = -1;
      let value = 0;
      for (let size = 1; size <= 4 && size <= i; size++) {
        const row = s.slice(i - size, i);
        const geo = rowGeometry(row, items, W, ref.gap, ref.cap);
        const h = geo.height / W;
        const deviation = h > CONSTANTS.targetRow ? (h - CONSTANTS.targetRow) / CONSTANTS.targetRow : (CONSTANTS.targetRow - h) / h;
        let sliver = 0;
        for (const tile of geo.tiles) {
          const side = Math.min(tile.w, tile.h) / W;
          if (side < ref.minSide) sliver = sliver + (ref.minSide - side);
        }
        const total = best[i - size] + weights.deviation * deviation + weights.sliver * sliver
          + weights.empty * ((W - geo.width) / W) * h;
        if (chosen < 0 || total < value) { chosen = size; value = total; }
      }
      best.push(value);
      from.push(chosen);
    }
    const rows = [];
    for (let i = s.length; i > 0; i -= from[i]) rows.push(s.slice(i - from[i], i));
    return rows.reverse();
  }

  /** A panorama, a very tall picture and a #wide one: a row of its own on wide
   *  screens and a run of its own on narrow ones. */
  function standsAlone(item) {
    const r = ratioOf(item);
    return item.wide === true || r >= CONSTANTS.panorama || r <= CONSTANTS.tall;
  }

  const aloneName = item => (item.wide === true ? 'wide' : ratioOf(item) >= CONSTANTS.panorama ? 'panorama' : 'tall');

  /** Whether a group has more pictures than the engine searches. */
  const capped = items => items.length > CONSTANTS.limits.maxGroup;

  /**
   * Runs of similar shape, in author order: a picture joins the run while
   * the run's widest ratio stays at most `spread` times its narrowest. A
   * picture that stands alone is a run of its own, as it is a row of its
   * own; so is every picture of a capped group.
   */
  function compatibleRuns(items) {
    const spread = CONSTANTS.phone.spread;
    const runs = [];
    let run = null;
    let lo = 0;
    let hi = 0;
    const alone = capped(items);
    items.forEach((item, index) => {
      const r = ratioOf(item);
      if (alone || standsAlone(item)) { runs.push([index]); run = null; return; }
      if (run && Math.max(hi, r) <= Math.min(lo, r) * spread) {
        run.push(index);
        lo = Math.min(lo, r);
        hi = Math.max(hi, r);
        return;
      }
      run = [index];
      runs.push(run);
      lo = r;
      hi = r;
    });
    return runs;
  }

  /**
   * The wide-screen layout of one group: `[{ cells, name }]`, one entry per
   * row. A stack never splits a run of similar pictures: its pictures all
   * belong to one run, or each is a run of its own. So every run of two or
   * more is made of whole cells and is one element in the markup.
   */
  function layout(items) {
    if (capped(items)) return items.map((item, index) => ({ cells: [index], name: standsAlone(item) ? aloneName(item) : 'capped' }));
    const ref = CONSTANTS.regular;
    const weights = CONSTANTS.weights;
    const rows = [];
    const names = [];
    const runOf = [];
    const runSize = [];
    for (const run of compatibleRuns(items)) for (const index of run) { runOf[index] = run[0]; runSize[index] = run.length; }
    const keepsRuns = stack => stack.every(index => runSize[index] === 1) || stack.every(index => runOf[index] === runOf[stack[0]]);
    let segment = [];
    const flush = () => {
      if (!segment.length) return;
      if (segment.length === 1) { rows.push([segment[0]]); names.push('single'); }
      else if (segment.length <= 4) {
        let chosen = null;
        let value = 0;
        for (const [name, candidate] of templates(segment)) {
          if (candidate.some(row => row.some(cell => typeof cell !== 'number' && !keepsRuns(cell)))) continue;
          if (!candidate.every(row => insideCaptionsFit(row, items, ref))) continue;
          const score = cost(candidate, items, ref, weights);
          if (!chosen || score < value) { chosen = [name, candidate]; value = score; }
        }
        for (const row of chosen[1]) { rows.push(row); names.push(chosen[0]); }
      } else {
        for (const row of justified(segment, items, ref, weights)) { rows.push(row); names.push('rows'); }
      }
      segment = [];
    };
    items.forEach((item, index) => {
      if (standsAlone(item)) { flush(); rows.push([index]); names.push(aloneName(item)); }
      else segment.push(index);
    });
    flush();
    return rows.map((cells, n) => ({ cells, name: names[n] }));
  }

  /** Whether all the pictures side by side still read well on the reference phone. */
  function readsAsRow(figures, items) {
    const p = CONSTANTS.phone;
    const W = p.refWidth;
    const cap = Math.min(p.capPx, p.capVh * p.refViewport);
    let sumR = 0;
    for (const index of figures) sumR = sumR + ratioOf(items[index]);
    const H = Math.min(cap, (W - (figures.length - 1) * p.gap) / sumR);
    for (const index of figures) {
      const r = ratioOf(items[index]);
      const w = r * H;
      if (w * p.spread < Math.min(W, cap * r)) return false;
      if (captionLength(items[index]) && w < p.captionMin) return false;
    }
    return true;
  }

  /**
   * Narrow-column segments of a group, in author order: one for each run of
   * similar shape, never cut. A segment is 'single' (one picture), 'row' (all
   * side by side) or 'strip' (one swipe strip of equal height on a phone
   * reader; one picture under another in editors and print). `cells` lists
   * the wide layout's cells it holds, as [row, cell] pairs: a run of two or
   * more always holds whole cells, so its element wraps them.
   */
  function phoneSegments(result, items) {
    const cellOf = [];
    result.forEach((row, r) => row.cells.forEach((cell, c) => {
      for (const index of typeof cell === 'number' ? [cell] : cell) cellOf[index] = [r, c];
    }));
    const segments = [];
    for (const figures of compatibleRuns(items)) {
      let sumR = 0;
      let maxR = 0;
      for (const index of figures) { const r = ratioOf(items[index]); sumR = sumR + r; maxR = Math.max(maxR, r); }
      let mode = 'strip';
      if (figures.length === 1) mode = 'single';
      else if (readsAsRow(figures, items)) mode = 'row';
      const cells = [];
      for (const index of figures) {
        const [r, c] = cellOf[index];
        const last = cells[cells.length - 1];
        if (!last || last[0] !== r || last[1] !== c) cells.push([r, c]);
      }
      segments.push({
        figures, mode, cells,
        properties: { '--n': String(figures.length), '--sr': fixed4(sumR), '--srmax': fixed4(maxR) },
      });
    }
    return segments;
  }

  /**
   * Geometry of a phone segment on a phone reader at container width W and
   * viewport height V: the shared picture height, each picture's width, each
   * figure's width (a captioned picture in a strip or alone is at least
   * captionMin wide, never wider than W) and the total width (more than W:
   * the strip scrolls). `smallest`: the least share of its size alone that
   * any picture shows (the readable minimum asks 1/spread).
   */
  function phoneGeometry(segment, items, W, V) {
    const p = CONSTANTS.phone;
    const alone = segment.figures.length === 1 && items[segment.figures[0]].wide === true;
    const cap = alone ? Math.min(CONSTANTS.wideCap.px, CONSTANTS.wideCap.vh * V) : Math.min(p.capPx, p.capVh * V);
    const n = segment.figures.length;
    let sumR = 0;
    let maxR = 0;
    for (const index of segment.figures) { const r = ratioOf(items[index]); sumR = sumR + r; maxR = Math.max(maxR, r); }
    const H = Math.min(cap, segment.mode === 'row' ? (W - (n - 1) * p.gap) / sumR : W / maxR);
    const pictures = [];
    const figures = [];
    let total = (n - 1) * p.gap;
    let smallest = 1;
    for (const index of segment.figures) {
      const r = ratioOf(items[index]);
      const w = r * H;
      const f = segment.mode !== 'row' && captionLength(items[index]) ? Math.max(w, Math.min(p.captionMin, W)) : w;
      pictures.push(w);
      figures.push(f);
      total = total + f;
      smallest = Math.min(smallest, w / Math.min(W, cap * r));
    }
    return { height: H, pictures, figures, total, scrolls: total > W + 0.5, smallest };
  }

  /** `0 | 1/2 ; 3`: rows split by `;`, cells by `|`, a stack's figures by `/`. */
  function notation(result) {
    return result.map(row => row.cells.map(cell => (typeof cell === 'number' ? String(cell) : cell.join('/'))).join(' | ')).join(' ; ');
  }

  /** Fixed-point text for markup: floor(x * 10^4 + 0.5), written from the
   *  integer, trailing zeros dropped; a negative value gets its sign in front. */
  function fixed4(x) {
    const n = Math.floor(x * 10000 + 0.5);
    const a = n < 0 ? -n : n;
    const whole = Math.floor(a / 10000);
    const frac = String(a - whole * 10000).padStart(4, '0').replace(/0+$/, '');
    return (n < 0 ? '-' : '') + (frac ? `${whole}.${frac}` : String(whole));
  }

  /**
   * The wide-layout place of each cell of one row, as custom properties. The
   * track is a grid of one column; a cell sits in grid row `--row` and the
   * CSS finds its width and start from these sums:
   *   row:  --rs  sum of the cells' ratios r
   *         --rg  (cells - 1) - sum of r * k      (k: gaps inside a stack)
   *         --rl  sum of r * c                    (c: captions inside a stack)
   *         --rc  the most captions inside one stack of the row
   *   cell: --r, --k, --c  its own ratio, inside gaps, inside captions
   *         --xr, --xg, --xl  sum of r, (count - sum of r * k) and sum of r * c
   *                           over the cells before it in the row
   * A property whose value would be 0 is left out; the CSS reads it as 0.
   */
  function cellProperties(row, rowIndex, items) {
    const cells = row.cells.map(cell => cellInfo(cell, items));
    let rs = 0;
    let rk = 0;
    let rl = 0;
    let rc = 0;
    for (const cell of cells) { rs = rs + cell.r; rk = rk + cell.r * cell.k; rl = rl + cell.r * cell.c; rc = Math.max(rc, cell.c); }
    const rowPart = { '--row': String(rowIndex + 1), '--rs': fixed4(rs), '--rg': fixed4((cells.length - 1) - rk), '--rl': fixed4(rl), '--rc': String(rc) };
    let xr = 0;
    let xk = 0;
    let xl = 0;
    return cells.map((cell, n) => {
      const all = { ...rowPart, '--r': fixed4(cell.r), '--k': String(cell.k), '--c': String(cell.c), '--xr': fixed4(xr), '--xg': fixed4(n - xk), '--xl': fixed4(xl) };
      xr = xr + cell.r;
      xk = xk + cell.r * cell.k;
      xl = xl + cell.r * cell.c;
      const properties = {};
      for (const [key, value] of Object.entries(all)) if (value !== '0') properties[key] = value;
      return properties;
    });
  }

  /**
   * The markup of one group, in author order: a flat list of nodes. A run of
   * two or more pictures is one `run` node holding its whole cells; every
   * other cell stands in the list itself. A cell is a `figure` (one picture)
   * or a `stack` (pictures one under another on wide screens).
   *   { kind: 'run', mode: 'row' | 'strip', properties, cells: [cell, ...] }
   *   { kind: 'figure', index, properties }
   *   { kind: 'stack', figures: [index, ...], properties }
   */
  function groupMarkup(result, items) {
    const segmentOf = [];
    for (const segment of phoneSegments(result, items)) for (const index of segment.figures) segmentOf[index] = segment;
    const nodes = [];
    let open = null;
    result.forEach((row, r) => {
      const placed = cellProperties(row, r, items);
      row.cells.forEach((cell, c) => {
        const node = typeof cell === 'number'
          ? { kind: 'figure', index: cell, properties: placed[c] }
          : { kind: 'stack', figures: cell, properties: placed[c] };
        const segment = segmentOf[typeof cell === 'number' ? cell : cell[0]];
        if (segment.mode === 'single') { open = null; nodes.push(node); return; }
        if (!open || open.segment !== segment) {
          open = { segment, node: { kind: 'run', mode: segment.mode, properties: segment.properties, cells: [] } };
          nodes.push(open.node);
        }
        open.node.cells.push(node);
      });
    });
    return nodes;
  }

  /** Custom properties as style text, in the order given: `--row:1;--rs:1.5`. */
  function styleText(properties) {
    return Object.entries(properties).map(([key, value]) => `${key}:${value}`).join(';');
  }

  /**
   * The group's track content as HTML, from groupMarkup's nodes. The wrappers
   * (run and stack) are written here, so every renderer writes them the same.
   * `figure(index, properties)` returns the HTML of picture `index`, the
   * renderer's own <figure class="e21-figure">; when `properties` is not null
   * the figure is a cell: it also carries the class e21-pg-cell and, after
   * its own --ar, styleText(properties) in its style.
   */
  function groupHTML(nodes, figure) {
    const cell = node => (node.kind === 'figure'
      ? figure(node.index, node.properties)
      : `<div class="e21-pg-cell e21-pg-stack" style="${styleText(node.properties)}">${node.figures.map(index => figure(index, null)).join('')}</div>`);
    return nodes.map(node => (node.kind === 'run'
      ? `<div class="e21-pg-run" data-phone="${node.mode}" style="${styleText(node.properties)}">${node.cells.map(cell).join('')}</div>`
      : cell(node))).join('');
  }

  global.E21PhotoLayout = Object.freeze({
    CONSTANTS, pictureSize, ratioOf, layout, rowGeometry, phoneSegments, phoneGeometry,
    notation, fixed4, groupMarkup, styleText, groupHTML,
  });
})(globalThis);
