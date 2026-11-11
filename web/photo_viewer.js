// SPDX-FileCopyrightText: 2026 rinbal
// SPDX-License-Identifier: AGPL-3.0-or-later
/* The full view of a picture: one viewer for every picture of an article,
 * its group's pictures a step away (docs/publishing-experience.md, "Photo
 * groups"). Like Quick Look and Photos: a black backdrop; Close, "2 von 4"
 * and the original along the top; the whole caption below.
 *
 *   pointer   double-click zooms, Ctrl or Cmd with the wheel zooms, drag
 *             pans, a click beside the picture closes
 *   touch     pinch, double tap, swipe sideways to step, swipe down to close
 *   keys      Escape, Left and Right (pan when zoomed), Up and Down, + - 0,
 *             Tab through the visible buttons only, and a caption too long
 *             for its room (a third of the screen), which then scrolls
 *
 * It is a modal dialog: focus goes to Close, the rest of the page is inert
 * and does not scroll, Back closes it, and on closing focus returns to the
 * picture now shown (or to the "Vergrößern" button that opened it). With
 * Reduce Motion it only fades. A plain script: the published pages inline
 * it, the app imports it (src/utils/photoGroups.js); photo_groups.js opens it.
 *
 *   E21PhotoViewer.open(entries, index, opener, words)
 *     entries  [{ src, original, alt, caption, w, h, focus, post }] in
 *              Markdown order; original the address "Open Original" goes to
 *              ('' hides it), w and h the picture's size when known (0 while
 *              it is not), focus the element that stands for the picture on
 *              the page, post (optional) the post the picture belongs to as
 *              { href, go }: a link below the caption, which closes the view
 *              and calls go() (a click with a modifier is the browser's)
 *     words    the words of the picture's language: one language of
 *              shared/photo-group-messages.json and its code as `lang`, and
 *              `post`, the link's words, from a host whose entries have posts
 *   E21PhotoViewer.close()
 */
(function (global) {
  'use strict';
  if (global.E21PhotoViewer) return;

  const ICON = {
    close: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
    prev: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>',
    next: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>',
    original: '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6"/><path d="M20 4 11 13"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>',
  };
  const ZOOMED = 1.01;
  const PROVISIONAL = [3, 2];

  let root = null;
  let stage, img, caption, count, live, failed, prev, next, closeButton, original, post;
  let list = [];
  let at = 0;
  let opener = null;
  let words = null;
  let fit = { x: 0, y: 0, w: 0, h: 0 };
  let z = 1;
  let tx = 0;
  let ty = 0;
  let open = false;
  let restore = null; // what the page was before: inert, overflow, history
  let pendingBack = 0; // Back steps the viewer took itself, not the reader
  let afterBack = null; // what runs once the viewer's own Back arrived
  const reduce = () => global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const fill = (text, values) => Object.keys(values).reduce((out, key) => out.split('{' + key + '}').join(String(values[key])), text);

  function build() {
    root = document.createElement('div');
    root.className = 'e21-pv';
    root.hidden = true;
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    // Focus stays in the viewer when the picture itself is clicked.
    root.tabIndex = -1;
    root.innerHTML = '<div class="e21-pv-bar">'
      + `<button type="button" class="e21-pv-btn e21-pv-close">${ICON.close}</button>`
      + '<span class="e21-pv-count" aria-hidden="true"></span>'
      + `<a class="e21-pv-btn e21-pv-original" target="_blank" rel="noopener noreferrer">${ICON.original}</a></div>`
      + '<div class="e21-pv-stage"><img class="e21-pv-img" alt="" draggable="false"><p class="e21-pv-failed" hidden></p>'
      + `<button type="button" class="e21-pv-btn e21-pv-side e21-pv-prev">${ICON.prev}</button>`
      + `<button type="button" class="e21-pv-btn e21-pv-side e21-pv-next">${ICON.next}</button></div>`
      + '<div class="e21-pv-foot"><p class="e21-pv-caption"></p><a class="e21-pv-post" hidden></a>'
      + '<p class="e21-pv-sr" aria-live="polite"></p></div>';
    stage = root.querySelector('.e21-pv-stage');
    img = root.querySelector('.e21-pv-img');
    failed = root.querySelector('.e21-pv-failed');
    caption = root.querySelector('.e21-pv-caption');
    count = root.querySelector('.e21-pv-count');
    live = root.querySelector('.e21-pv-sr');
    prev = root.querySelector('.e21-pv-prev');
    next = root.querySelector('.e21-pv-next');
    closeButton = root.querySelector('.e21-pv-close');
    original = root.querySelector('.e21-pv-original');
    post = root.querySelector('.e21-pv-post');
    closeButton.addEventListener('click', () => close());
    post.addEventListener('click', onPost);
    prev.addEventListener('click', () => show(at - 1));
    next.addEventListener('click', () => show(at + 1));
    stage.addEventListener('pointerdown', onDown);
    stage.addEventListener('pointermove', onMove);
    stage.addEventListener('pointerup', onUp);
    stage.addEventListener('pointercancel', onUp);
    stage.addEventListener('dblclick', event => { if (event.target === img) toggleZoom(event.clientX, event.clientY); });
    stage.addEventListener('wheel', onWheel, { passive: false });
    img.addEventListener('load', onLoad);
    img.addEventListener('error', () => { img.hidden = true; failed.hidden = false; });
    global.addEventListener('popstate', onPop);
    global.addEventListener('resize', () => { if (open) { measure(); clamp(); apply(false); } });
  }

  // A picture whose size was not known yet takes its real one when it arrives.
  function onLoad() {
    const item = list[at];
    if (!item || !img.naturalWidth) return;
    if (item.w !== img.naturalWidth || item.h !== img.naturalHeight) {
      item.w = img.naturalWidth;
      item.h = img.naturalHeight;
      z = 1; tx = 0; ty = 0;
      measure();
      apply(false);
    }
  }

  function size(item) {
    return item.w > 0 && item.h > 0 ? [item.w, item.h] : PROVISIONAL;
  }

  // The picture fitted to the stage: whole, 24 px from its edges on a large
  // screen and edge to edge on a small one.
  function measure() {
    const [w, h] = size(list[at]);
    const W = stage.clientWidth;
    const H = stage.clientHeight;
    const pad = W >= 700 ? 24 : 0;
    const scale = Math.max(0, Math.min((W - 2 * pad) / w, (H - 2 * pad) / h));
    fit = { w: w * scale, h: h * scale };
    fit.x = (W - fit.w) / 2;
    fit.y = (H - fit.h) / 2;
    img.style.width = `${fit.w}px`;
    img.style.height = `${fit.h}px`;
  }
  // Up to twice the picture's own pixels, at least 3x and at most 8x.
  const maxZoom = () => Math.min(8, Math.max(3, fit.w ? (size(list[at])[0] / fit.w) * 2 : 3));
  function clamp() {
    const W = stage.clientWidth;
    const H = stage.clientHeight;
    const sw = fit.w * z;
    const sh = fit.h * z;
    tx = sw <= W ? (W - sw) / 2 - fit.x : Math.min(-fit.x, Math.max(W - sw - fit.x, tx));
    ty = sh <= H ? (H - sh) / 2 - fit.y : Math.min(-fit.y, Math.max(H - sh - fit.y, ty));
  }
  function apply(animated, dx = 0, dy = 0) {
    img.classList.toggle('is-animated', animated && !reduce());
    img.style.transform = `translate(${fit.x + tx + dx}px, ${fit.y + ty + dy}px) scale(${z})`;
    stage.classList.toggle('is-zoomed', z > ZOOMED);
  }
  function zoomTo(value, px, py, animated) {
    const box = stage.getBoundingClientRect();
    const x = px - box.left;
    const y = py - box.top;
    const target = Math.min(maxZoom(), Math.max(1, value));
    const u = (x - fit.x - tx) / z;
    const v = (y - fit.y - ty) / z;
    tx = x - fit.x - target * u;
    ty = y - fit.y - target * v;
    z = target;
    clamp();
    apply(animated);
  }
  function toggleZoom(px, py) {
    if (z > ZOOMED) zoomTo(1, px, py, true);
    else zoomTo(Math.max(2.5, size(list[at])[0] / (fit.w || 1)), px, py, true);
  }
  function centre() {
    const box = stage.getBoundingClientRect();
    return [box.left + box.width / 2, box.top + box.height / 2];
  }

  function show(index) {
    if (index < 0 || index >= list.length) return;
    at = index;
    const item = list[at];
    img.hidden = false;
    failed.hidden = true;
    img.removeAttribute('src');
    img.src = item.src;
    img.alt = item.alt;
    // The caption first: its height is the stage's loss. A caption longer
    // than its room scrolls, and is then a stop for Tab, so keys scroll it.
    caption.textContent = item.caption;
    caption.scrollTop = 0;
    post.hidden = !item.post;
    if (item.post) post.href = item.post.href;
    else post.removeAttribute('href');
    if (caption.scrollHeight > caption.clientHeight + 1) caption.tabIndex = 0;
    else caption.removeAttribute('tabindex');
    z = 1; tx = 0; ty = 0;
    measure();
    apply(false);
    original.hidden = !item.original;
    if (item.original) original.href = item.original;
    else original.removeAttribute('href');
    const many = list.length > 1;
    count.textContent = many ? fill(words.count, { k: at + 1, n: list.length }) : '';
    const focused = document.activeElement;
    prev.hidden = !many || at === 0;
    next.hidden = !many || at === list.length - 1;
    // A side button that hides with focus on it hands focus to the other
    // side, or to Close: focus never falls to the page behind.
    if ((focused === prev || focused === next) && focused.hidden) {
      const other = focused === prev ? next : prev;
      (other.hidden ? closeButton : other).focus({ preventScroll: true });
    }
    live.textContent = (many ? fill(words.photo, { k: at + 1, n: list.length }) + '. ' : '') + (item.caption || item.alt);
  }

  function label() {
    root.setAttribute('aria-label', words.viewer);
    root.setAttribute('lang', words.lang);
    closeButton.setAttribute('aria-label', words.close);
    original.setAttribute('aria-label', words.original);
    prev.setAttribute('aria-label', words.previous);
    next.setAttribute('aria-label', words.next);
    failed.textContent = words.failed;
    post.textContent = words.post || '';
  }

  // The post a picture belongs to: the view closes, its own history entry
  // goes, and then the host opens the post, so Back from the post comes to
  // the page and not to a closed view.
  function onPost(event) {
    const item = list[at];
    if (!item || !item.post || !item.post.go) return;
    if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    close(item.post.go);
  }

  // The page behind the viewer: inert, not scrolling, and Back closes the
  // viewer (one history entry of the page's own address, removed again).
  function lockPage() {
    const html = document.documentElement;
    const token = String(Math.random()).slice(2);
    // Opened again before the Back of the last close arrived: the entry is
    // still there, so it is taken over, and onPop puts it back after that Back.
    const method = global.history.state && global.history.state.e21PhotoViewer ? 'replaceState' : 'pushState';
    let history = false;
    try {
      global.history[method]({ e21PhotoViewer: token }, '');
      history = true;
    } catch (error) {
      // A sandboxed page may not touch its history (SecurityError): Escape
      // and Close still work. Anything else is a fault and goes on, before
      // the page behind is changed.
      if (!error || error.name !== 'SecurityError') throw error;
    }
    const inert = [];
    for (const child of document.body.children) {
      if (child !== root && !child.inert) { child.inert = true; inert.push(child); }
    }
    restore = { inert, overflow: html.style.overflow, token, history };
    html.style.overflow = 'hidden';
  }
  function unlockPage(then) {
    if (!restore) return;
    const { inert, overflow, token, history } = restore;
    restore = null;
    for (const child of inert) child.inert = false;
    document.documentElement.style.overflow = overflow;
    // Closed by the page, not by Back: take the viewer's history entry away.
    // A Back still on its way (the view was opened again before it arrived,
    // and took the entry over) removes it already; a second one would leave
    // the page.
    if (history && global.history.state && global.history.state.e21PhotoViewer === token) {
      afterBack = then || null;
      if (!pendingBack) {
        pendingBack += 1;
        global.history.back();
      }
    } else if (then) {
      then();
    }
  }
  function onPop() {
    if (pendingBack) {
      pendingBack -= 1;
      if (open && restore && restore.history) global.history.pushState({ e21PhotoViewer: restore.token }, '');
      // After every other listener of this Back (an app's router) had it,
      // so what `then` starts is the newer step and is not taken back.
      if (!pendingBack && afterBack) {
        global.setTimeout(afterBack);
        afterBack = null;
      }
      return;
    }
    if (!open) return;
    restore.history = false;
    close();
  }

  function openViewer(entries, index, from, pageWords) {
    if (!entries.length) return;
    if (!root) build();
    if (!root.isConnected) document.body.append(root);
    list = entries.map(entry => ({ ...entry }));
    opener = from || null;
    words = pageWords;
    label();
    if (!open) {
      lockPage();
      open = true;
      // While open, every key is the viewer's, wherever focus is.
      document.addEventListener('keydown', onKey, true);
    }
    root.hidden = false;
    show(Math.max(0, Math.min(list.length - 1, index)));
    // Grows from its picture into place; with Reduce Motion it only fades.
    const tile = list[at].focus && list[at].focus.closest('figure')?.querySelector('img');
    const start = tile && tile.getBoundingClientRect();
    const box = stage.getBoundingClientRect();
    if (!reduce() && start && start.width && fit.w) {
      img.classList.remove('is-animated');
      img.style.transform = `translate(${start.left - box.left}px, ${start.top - box.top}px) scale(${start.width / fit.w})`;
      img.getBoundingClientRect();
    }
    global.requestAnimationFrame(() => { root.classList.add('is-open'); apply(true); });
    closeButton.focus();
  }

  // Focus goes back to the picture now shown, or to the button that opened
  // it. `then` runs once the page has its history back (onPost).
  function close(then) {
    if (!open) return;
    open = false;
    document.removeEventListener('keydown', onKey, true);
    const shown = list[at] && list[at].focus;
    const back = opener && shown && opener.closest('figure') === shown.closest('figure') ? opener : shown;
    root.classList.remove('is-open');
    unlockPage(then);
    const finish = () => {
      if (open) return;
      root.hidden = true;
      img.removeAttribute('src');
      if (back && back.isConnected) back.focus({ preventScroll: false });
    };
    if (reduce()) { finish(); return; }
    const tile = back && back.closest('figure')?.querySelector('img');
    const to = tile && tile.getBoundingClientRect();
    const box = stage.getBoundingClientRect();
    if (to && to.width && fit.w && z <= ZOOMED) {
      img.classList.add('is-animated');
      img.style.transform = `translate(${to.left - box.left}px, ${to.top - box.top}px) scale(${to.width / fit.w})`;
    }
    global.setTimeout(finish, 220);
  }

  // A modal sheet owns the keyboard: no key reaches the page's own shortcuts.
  // Listened for on the document, first, while the viewer is open.
  function onKey(event) {
    event.stopPropagation();
    const key = event.key;
    if (key === 'Escape') { event.preventDefault(); close(); return; }
    if (key === 'Tab') {
      const stops = [closeButton, original, prev, next, ...(caption.tabIndex === 0 ? [caption] : []), post]
        .filter(stop => !stop.hidden && stop.getClientRects().length);
      const position = stops.indexOf(document.activeElement);
      event.preventDefault();
      const step = event.shiftKey ? -1 : 1;
      // From outside the buttons, Tab goes to the first and Shift-Tab to the last.
      const from = position < 0 ? (event.shiftKey ? 0 : -1) : position;
      stops[(from + step + stops.length) % stops.length].focus();
      return;
    }
    const zoomed = z > ZOOMED;
    const [cx, cy] = centre();
    if (key === 'ArrowLeft' || key === 'ArrowRight') {
      event.preventDefault();
      if (zoomed) { tx += key === 'ArrowLeft' ? 80 : -80; clamp(); apply(true); } else show(at + (key === 'ArrowLeft' ? -1 : 1));
    } else if ((key === 'ArrowUp' || key === 'ArrowDown') && zoomed) {
      event.preventDefault(); ty += key === 'ArrowUp' ? 80 : -80; clamp(); apply(true);
    } else if (key === '+' || key === '=') { event.preventDefault(); zoomTo(z * 1.6, cx, cy, true); }
    else if (key === '-') { event.preventDefault(); zoomTo(z / 1.6, cx, cy, true); }
    else if (key === '0') { event.preventDefault(); zoomTo(1, cx, cy, true); }
  }

  const pointers = new Map();
  let gesture = null;
  let lastTap = 0;
  function onDown(event) {
    if (event.target.closest('.e21-pv-btn')) return;
    // A click on the picture or beside it would leave focus on the page
    // behind (the body), where no key reaches the viewer.
    root.focus({ preventScroll: true });
    stage.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      gesture = { pinch: true, d: Math.hypot(a.x - b.x, a.y - b.y) || 1, z0: z };
    } else {
      gesture = { pinch: false, x: event.clientX, y: event.clientY, tx, ty, moved: false, onImage: event.target === img };
    }
  }
  function onMove(event) {
    if (!pointers.has(event.pointerId) || !gesture) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (gesture.pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      zoomTo(gesture.z0 * Math.hypot(a.x - b.x, a.y - b.y) / gesture.d, (a.x + b.x) / 2, (a.y + b.y) / 2, false);
      return;
    }
    const dx = event.clientX - gesture.x;
    const dy = event.clientY - gesture.y;
    if (Math.abs(dx) + Math.abs(dy) > 6) gesture.moved = true;
    if (z > ZOOMED) {
      stage.classList.add('is-panning');
      tx = gesture.tx + dx; ty = gesture.ty + dy; clamp(); apply(false);
    } else if (gesture.moved) {
      apply(false, dx, Math.abs(dy) > Math.abs(dx) && dy > 0 ? dy : 0);
    }
  }
  function onUp(event) {
    if (!pointers.has(event.pointerId)) return;
    pointers.delete(event.pointerId);
    stage.classList.remove('is-panning');
    if (!gesture || gesture.pinch) { if (!pointers.size) gesture = null; return; }
    const dx = event.clientX - gesture.x;
    const dy = event.clientY - gesture.y;
    const g = gesture;
    gesture = null;
    if (z > ZOOMED) return;
    if (g.moved) {
      if (dy > 110 && dy > Math.abs(dx)) { close(); return; }
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) {
        const target = at + (dx < 0 ? 1 : -1);
        if (target >= 0 && target < list.length) { show(target); return; }
      }
      apply(true);
      return;
    }
    if (event.pointerType === 'touch') {
      const now = Date.now();
      if (now - lastTap < 320 && g.onImage) { lastTap = 0; toggleZoom(event.clientX, event.clientY); return; }
      lastTap = now;
    } else if (!g.onImage && event.type === 'pointerup') {
      close(); // a click beside the picture closes, as a sheet does
    }
  }
  function onWheel(event) {
    event.preventDefault();
    if (event.ctrlKey || event.metaKey) zoomTo(z * Math.exp(-event.deltaY * 0.01), event.clientX, event.clientY, false);
    else if (z > ZOOMED) { tx -= event.deltaX; ty -= event.deltaY; clamp(); apply(false); }
  }
  global.E21PhotoViewer = Object.freeze({ open: openViewer, close: () => close(), get isOpen() { return open; } });
})(globalThis);
