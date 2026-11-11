// SPDX-FileCopyrightText: 2026 DoktorShift
// SPDX-License-Identifier: MIT
/* Photo groups on a reading page (docs/publishing-experience.md, "Photo
 * groups"): what the markup and article.css cannot do alone. Without this
 * script the page reads the same: CSS lays out every group from the
 * renderer's numbers, a strip still swipes, a picture's link opens it.
 *
 * - Near the view. Nothing is done for a group (or a picture alone) until it
 *   comes within a screen of the view; then, in one step a frame for all
 *   that came near, it gets its words, its strips their dots, and its
 *   pictures are fetched. So a page costs what the reader sees of it,
 *   however long the article.
 *   An article of 200 groups and more is marked (`data-pg-many`), and
 *   article.css then lays its groups out only near the view as well.
 * - One settle. A group (or a picture alone) whose sizes were not known, or
 *   were wrong, is laid out once more from the pictures' real sizes: its
 *   pictures are fetched together when it comes near, and
 *   once all have arrived or failed, or 2.5 s after that, the engine
 *   (photo_layout.js) lays it out again and the figures move into the new
 *   cells. A picture that arrives later stays whole in its place; one that
 *   fails keeps its plate. Sizes that were right change nothing.
 * - A strip that really scrolls (a phone) says where the reader is: a dot
 *   per picture, or "3 von 14" past ten; "Foto 2 von 4" spoken politely when
 *   the reader moves; Left, Right, Home and End on the strip; an instant
 *   jump with Reduce Motion. The strip is a named group only while it scrolls.
 * - A picture opens in the viewer (photo_viewer.js), its group's pictures a
 *   step away; so does "Vergrößern".
 *
 * Every word is in the language of the text around the pictures (the nearest
 * `lang`), from shared/photo-group-messages.json, and follows Discover's
 * language switch. A plain script: published pages inline it after
 * photo_layout.js and photo_viewer.js, the app imports it
 * (src/utils/photoGroups.js).
 *
 *   E21PhotoGroups.attach(root, { messages }) -> { detach }
 */
(function (global) {
  'use strict';
  if (global.E21PhotoGroups) return;

  const MAX_DOTS = 10;
  const MANY_GROUPS = 200; // from here on article.css shows a group only near the view (photo_scripts.py marks the page)
  const DOT = 44; // a dot's target, px (article.css): dots show only while all fit in a row
  const SETTLE_MS = 2500;
  const SAME_SHAPE = 0.01; // a real shape within 1 % of the written one is the same
  const OPENABLE = /^(https?:\/\/|\/(?!\/))/i;
  // A raster picture written into the page itself, as MyEditor's
  // self-contained export writes every picture; never SVG or another type.
  const INLINE = /^data:image\/(?:png|jpeg|gif|webp|avif);base64,/i;
  const reduce = () => global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const fill = (text, values) => Object.keys(values).reduce((out, key) => out.split('{' + key + '}').join(String(values[key])), text);
  const codePoints = text => Array.from(text || '').length;
  let attached = 0; // pages attached; the zoom buttons show while one is

  function attach(root, { messages }) {
    const engine = global.E21PhotoLayout;
    const viewer = global.E21PhotoViewer;
    const events = new AbortController();
    const listen = (target, type, handler, options) => target.addEventListener(type, handler, { ...options, signal: events.signal });
    const timers = new Set();
    const strips = new Map(); // run element -> its controller
    const html = document.documentElement;

    const language = node => (/^de/i.test(node.closest('[lang]')?.getAttribute('lang') || '') ? 'de' : 'en');
    const words = node => ({ lang: language(node), ...messages[language(node)] });
    const figuresOf = unit => (unit.matches('figure') ? [unit] : Array.from(unit.querySelectorAll('figure.e21-figure')));
    const imageOf = figure => figure.querySelector(':scope > img, :scope > a > img');

    // ── One settle ─────────────────────────────────────────────────────
    function shapeOf(figure) {
      if (figure.hasAttribute('data-provisional')) return null;
      const [w, h] = (figure.style.getPropertyValue('--ar') || '').split('/').map(Number);
      return engine.pictureSize(w, h);
    }
    function itemsOf(figures) {
      return figures.map(figure => {
        const size = shapeOf(figure);
        return {
          w: size ? size[0] : 0, h: size ? size[1] : 0,
          caption: codePoints(figure.querySelector(':scope > figcaption')?.textContent),
          wide: figure.classList.contains('e21-figure--wide'),
        };
      });
    }
    // The figures move into the cells the engine writes, so pictures that
    // have arrived stay; a figure keeps only --ar of its old cell numbers.
    function arrange(group) {
      const track = group.querySelector(':scope > .e21-imagegroup-track');
      if (!track) return;
      const figures = figuresOf(track);
      const items = itemsOf(figures);
      const result = engine.layout(items);
      const focused = track.contains(document.activeElement) ? document.activeElement : null;
      for (const run of track.querySelectorAll('.e21-pg-run')) strips.get(run)?.destroy();
      const cells = [];
      track.innerHTML = engine.groupHTML(engine.groupMarkup(result, items), (index, properties) => {
        cells[index] = properties;
        return `<i data-e21-slot="${index}"></i>`;
      });
      for (const slot of track.querySelectorAll('[data-e21-slot]')) {
        const index = Number(slot.getAttribute('data-e21-slot'));
        const figure = figures[index];
        for (const name of Array.from(figure.style)) if (name.startsWith('--') && name !== '--ar') figure.style.removeProperty(name);
        figure.classList.toggle('e21-pg-cell', cells[index] !== null);
        if (cells[index]) for (const [name, value] of Object.entries(cells[index])) figure.style.setProperty(name, value);
        slot.replaceWith(figure);
      }
      group.setAttribute('data-layout', engine.notation(result));
      track.querySelectorAll('.e21-pg-run[data-phone="strip"]').forEach(addStrip);
      if (focused && focused.isConnected) focused.focus({ preventScroll: true });
    }
    // A picture's real size replaces the written one when they differ.
    function takeRealSize(figure) {
      const img = imageOf(figure);
      const real = img && img.naturalWidth ? engine.pictureSize(img.naturalWidth, img.naturalHeight) : null;
      if (!real) return false;
      const written = shapeOf(figure);
      if (written && Math.abs(real[0] / real[1] - written[0] / written[1]) <= SAME_SHAPE * (written[0] / written[1])) return false;
      figure.style.setProperty('--ar', `${real[0]}/${real[1]}`);
      img.setAttribute('width', real[0]);
      img.setAttribute('height', real[1]);
      figure.removeAttribute('data-provisional');
      return true;
    }
    function fetchUnit(unit) {
      const images = figuresOf(unit).map(imageOf).filter(Boolean);
      let pending = 0;
      let done = false;
      const settle = () => {
        if (done || events.signal.aborted) return;
        done = true;
        global.clearTimeout(deadline);
        timers.delete(deadline);
        let changed = false;
        for (const figure of figuresOf(unit)) changed = takeRealSize(figure) || changed;
        if (changed && unit.classList.contains('e21-imagegroup')) arrange(unit);
      };
      const arrived = () => { pending -= 1; if (pending <= 0) settle(); };
      const deadline = global.setTimeout(settle, SETTLE_MS);
      timers.add(deadline);
      for (const img of images) {
        if (img.complete) continue;
        pending += 1;
        listen(img, 'load', arrived, { once: true });
        listen(img, 'error', arrived, { once: true });
        if (img.loading === 'lazy') img.loading = 'eager';
      }
      if (!pending) settle();
    }

    // ── The strip's position ───────────────────────────────────────────
    const resize = typeof ResizeObserver === 'function'
      ? new ResizeObserver(entries => {
        const writes = entries.map(entry => strips.get(entry.target)?.measure()).filter(Boolean);
        writes.forEach(write => write());
      })
      : null;
    function addStrip(run) {
      if (strips.has(run)) return;
      const figures = Array.from(run.querySelectorAll('figure'));
      const n = figures.length;
      const own = new AbortController();
      const on = (target, type, handler, options) => target.addEventListener(type, handler, { ...options, signal: own.signal });
      const pager = document.createElement('div');
      pager.className = 'e21-pg-pager';
      pager.hidden = true;
      const dots = n <= MAX_DOTS ? figures.map((_, k) => {
        const dot = document.createElement('button');
        dot.type = 'button';
        dot.className = 'e21-pg-dot';
        on(dot, 'click', () => go(k));
        return dot;
      }) : [];
      const count = document.createElement('span');
      count.className = 'e21-pg-count';
      count.setAttribute('aria-hidden', 'true');
      const live = document.createElement('p');
      live.className = 'e21-pg-sr';
      live.setAttribute('aria-live', 'polite');
      count.hidden = dots.length > 0;
      pager.append(...dots, count, live);
      run.after(pager);

      let current = -1;
      let target = null;
      let spoken = false;
      let scrolls = false;
      let frame = 0;
      const rtl = () => global.getComputedStyle(run).direction === 'rtl';
      const snapLine = () => {
        const box = run.getBoundingClientRect();
        const pad = parseFloat(global.getComputedStyle(run).scrollPaddingInlineStart) || 0;
        return rtl() ? box.right - pad : box.left + pad;
      };
      const edge = figure => { const box = figure.getBoundingClientRect(); return rtl() ? box.right : box.left; };
      function go(k) {
        k = Math.max(0, Math.min(n - 1, k));
        target = k;
        spoken = true;
        run.scrollTo({ left: run.scrollLeft + edge(figures[k]) - snapLine(), behavior: reduce() ? 'instant' : 'smooth' });
        if (reduce()) update();
      }
      // Reads only. The current picture: the one whose leading edge is
      // nearest the snap line. At the end of the strip the last pictures
      // cannot reach it: the one asked for counts once it is fully in view.
      function nearest() {
        const line = snapLine();
        let best = 0;
        let distance = Infinity;
        figures.forEach((figure, k) => {
          const d = Math.abs(edge(figure) - line);
          if (d < distance) { distance = d; best = k; }
        });
        const atEnd = Math.abs(run.scrollLeft) >= run.scrollWidth - run.clientWidth - 1;
        if (atEnd && target !== null) {
          const box = figures[target].getBoundingClientRect();
          const view = run.getBoundingClientRect();
          if (box.left >= view.left - 1 && box.right <= view.right + 1) best = target;
        }
        return best;
      }
      // Writes only. Dots, the name and the keys only while the strip really
      // scrolls; the position said aloud only after the reader moved.
      function show(now, best) {
        if (now !== scrolls) {
          scrolls = now;
          pager.hidden = !scrolls;
          if (scrolls) {
            run.tabIndex = 0;
            run.setAttribute('role', 'group');
          } else {
            run.removeAttribute('tabindex');
            run.removeAttribute('role');
            run.removeAttribute('aria-label');
          }
          label();
        }
        if (best === current) return;
        current = best;
        const say = words(run);
        dots.forEach((dot, k) => dot.setAttribute('aria-current', String(k === best)));
        count.textContent = fill(say.count, { k: best + 1, n });
        if (spoken) live.textContent = fill(say.photo, { k: best + 1, n });
      }
      const update = () => show(scrolls, nearest());
      function label() {
        const say = words(run);
        pager.setAttribute('lang', say.lang);
        dots.forEach((dot, k) => dot.setAttribute('aria-label', fill(say.photo, { k: k + 1, n })));
        if (scrolls) run.setAttribute('aria-label', fill(say.strip, { n }));
        if (current >= 0) count.textContent = fill(say.count, { k: current + 1, n });
      }
      // Dots while all fit in a row as wide as the text, else "3 von 14".
      function fitDots(fit) {
        dots.forEach(dot => { dot.hidden = !fit; });
        count.hidden = fit;
      }
      // After a resize: what the strip is now, read before any strip is
      // written, so a page of many strips is laid out once, not once each.
      function measure() {
        const now = run.scrollWidth > run.clientWidth + 1;
        const best = now ? nearest() : 0;
        const style = global.getComputedStyle(run);
        const room = run.clientWidth - (parseFloat(style.paddingInlineStart) || 0) - (parseFloat(style.paddingInlineEnd) || 0);
        const fit = dots.length > 0 && dots.length * DOT <= room;
        return () => { fitDots(fit); show(now, best); };
      }
      on(run, 'scroll', () => { if (!frame) frame = global.requestAnimationFrame(() => { frame = 0; update(); }); }, { passive: true });
      on(run, 'pointerdown', () => { target = null; spoken = true; });
      on(run, 'keydown', event => {
        if (event.target !== run || !scrolls) return;
        const forward = rtl() ? 'ArrowLeft' : 'ArrowRight';
        const back = rtl() ? 'ArrowRight' : 'ArrowLeft';
        const k = { [forward]: current + 1, [back]: current - 1, Home: 0, End: n - 1 }[event.key];
        if (k === undefined) return;
        event.preventDefault();
        go(k);
      });
      // A picture reached with Tab scrolls into view by itself: say where.
      on(run, 'focusin', event => { if (event.target !== run) spoken = true; });
      const controller = {
        measure, label,
        destroy() {
          own.abort();
          if (frame) global.cancelAnimationFrame(frame);
          resize?.unobserve(run);
          pager.remove();
          run.removeAttribute('tabindex');
          run.removeAttribute('role');
          run.removeAttribute('aria-label');
          strips.delete(run);
        },
      };
      strips.set(run, controller);
      if (resize) resize.observe(run); else measure()();
    }

    // ── The viewer ─────────────────────────────────────────────────────
    // A web address (http or https), or ''.
    function webAddress(value) {
      try {
        const url = new URL(value);
        return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '';
      } catch {
        return '';
      }
    }
    // The pictures a picture steps through: its group's (or gallery's), in
    // Markdown order, each with an address the browser can open. A picture
    // written into the page opens only where its page offers it (an opener,
    // as in MyEditor's export; STANDUP's renderers give one none), and its
    // original is the web address it came from (`data-source-url`) or none.
    function entriesOf(figure) {
      const scope = figure.closest('.e21-imagegroup, .e21-gallery');
      const figures = scope ? Array.from(scope.querySelectorAll('figure')) : [figure];
      const entries = [];
      for (const each of figures) {
        const img = imageOf(each);
        const src = img?.getAttribute('src') || '';
        const inline = INLINE.test(src) && !!each.querySelector(':scope > a.e21-pg-open, :scope > .e21-pg-zoom');
        if (!img || !(OPENABLE.test(src) || inline)) continue;
        const size = img.naturalWidth ? [img.naturalWidth, img.naturalHeight] : shapeOf(each) || [0, 0];
        entries.push({
          figure: each, src: img.currentSrc || img.src, alt: img.alt, w: size[0], h: size[1],
          original: inline ? webAddress(img.getAttribute('data-source-url') || '') : img.currentSrc || img.src,
          caption: each.querySelector(':scope > figcaption')?.textContent.trim() || '',
          focus: each.querySelector(':scope > a') || each.querySelector(':scope > .e21-pg-zoom'),
        });
      }
      return entries;
    }
    listen(root, 'click', event => {
      if (event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || !viewer) return;
      const opener = event.target.closest?.('.e21-pg-zoom, a.e21-pg-open');
      const figure = opener?.closest('figure');
      if (!figure || !root.contains(figure)) return;
      const entries = entriesOf(figure);
      const index = entries.findIndex(entry => entry.figure === figure);
      if (index < 0) return;
      event.preventDefault();
      viewer.open(entries, index, opener, words(figure));
    });

    // ── Words in the page's language ───────────────────────────────────
    function label(unit) {
      if (unit.matches('.e21-imagegroup')) unit.setAttribute('aria-label', fill(words(unit).group, { n: figuresOf(unit).length }));
      for (const zoom of unit.querySelectorAll('.e21-pg-zoom')) {
        const say = words(zoom);
        const alt = imageOf(zoom.closest('figure'))?.alt || '';
        zoom.setAttribute('aria-label', alt ? fill(say.enlargeNamed, { alt }) : say.enlarge);
        const text = Array.from(zoom.childNodes).find(node => node.nodeType === 3);
        if (text) text.nodeValue = say.enlarge;
      }
    }
    // A unit not yet near keeps the renderer's words until it comes near.
    listen(global, 'discover:language', () => { ready.forEach(label); strips.forEach(strip => strip.label()); });

    // ── Near the view ──────────────────────────────────────────────────
    // A unit is a group, a gallery (which may be the root itself) or a
    // picture alone. Setting one up only writes (words, the viewer's promise
    // on its openers, the pagers, the fetch); the strips are measured
    // afterwards, all together (`resize`). So the units that came near in a
    // frame cost the page one layout, and the units far away cost nothing.
    const UNIT = '.e21-imagegroup, .e21-gallery, figure.e21-figure';
    const units = [...(root.matches?.(UNIT) ? [root] : []), ...root.querySelectorAll(UNIT)]
      .filter(unit => !unit.matches('figure') || !unit.closest('.e21-imagegroup'));
    // An article of very many groups is marked for the stylesheet, where
    // its page has not done so already.
    const groups = units.filter(unit => unit.matches('.e21-imagegroup'));
    const marked = groups.length >= MANY_GROUPS
      ? Array.from(new Set(groups.map(group => group.closest('.article-content')))).filter(article => article && !article.hasAttribute('data-pg-many'))
      : [];
    marked.forEach(article => article.setAttribute('data-pg-many', ''));
    const ready = new Set();
    const openersOf = unit => unit.querySelectorAll('a.e21-pg-open, .e21-pg-zoom');
    function prepare(unit) {
      ready.add(unit);
      label(unit);
      if (viewer) for (const opener of openersOf(unit)) opener.setAttribute('aria-haspopup', 'dialog');
      unit.querySelectorAll('.e21-pg-run[data-phone="strip"]').forEach(addStrip);
      fetchUnit(unit);
    }
    const near = typeof IntersectionObserver === 'function' ? new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        near.unobserve(entry.target);
        prepare(entry.target);
      }
    }, { rootMargin: '100% 0px' }) : null;
    // Without IntersectionObserver every unit is set up at once.
    units.forEach(unit => (near ? near.observe(unit) : prepare(unit)));
    if (viewer) html.setAttribute('data-photo-viewer', '');
    attached += 1;

    return {
      detach() {
        if (events.signal.aborted) return;
        events.abort();
        timers.forEach(timer => global.clearTimeout(timer));
        near?.disconnect();
        Array.from(strips.values()).forEach(strip => strip.destroy());
        resize?.disconnect();
        for (const unit of ready) for (const opener of openersOf(unit)) opener.removeAttribute('aria-haspopup');
        marked.forEach(article => article.removeAttribute('data-pg-many'));
        attached -= 1;
        if (!attached) html.removeAttribute('data-photo-viewer');
        if (viewer?.isOpen) viewer.close();
      },
    };
  }

  global.E21PhotoGroups = Object.freeze({ attach });
})(globalThis);
