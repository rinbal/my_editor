// SPDX-FileCopyrightText: 2026 rinbal
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The full view of a picture of a photo group, in an exported page: one
// viewer for the group, with its count, captions, zoom (+, -, 0, a double
// click, a pinch), Escape and the focus back on the picture it was opened
// from. Taken from the photo grid reference (reference/build/inner.js,
// "The full view") until STANDUP ships backend/htmlgen/photo_viewer.js;
// the words come from the page (export_html.py), in the app's language.
(function () {
  const W = JSON.parse(document.getElementById('e21-photo-words').textContent)
  const say = (words, k, n) => words.replace('{k}', () => k).replace('{n}', () => n)
  // Everything but the viewer is out of reach while it is open.
  const setInert = on => {
    for (const child of document.body.children) {
      if (!child.classList.contains('pv')) child.inert = on
    }
  }
  // ── The full view: one viewer for every picture of the article ────────
  const Viewer = (() => {
    const ICON = {
      close: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
      prev: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>',
      next: '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>',
      original: '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6"/><path d="M20 4 11 13"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>',
    }
    let root, stage, img, caption, count, live, prev, next, close, original
    let list = []
    let at = 0
    let opener = null
    let fit = { x: 0, y: 0, w: 0, h: 0 }
    let z = 1
    let tx = 0
    let ty = 0
    const reduce = () => matchMedia('(prefers-reduced-motion: reduce)').matches

    function build() {
      root = document.createElement('div')
      root.className = 'pv'
      root.hidden = true
      root.setAttribute('role', 'dialog')
      root.setAttribute('aria-modal', 'true')
      root.setAttribute('aria-label', W.viewer)
      root.innerHTML = `<div class="pv-bar"><button type="button" class="pv-btn pv-close" aria-label="${W.close}">${ICON.close}</button>`
        + '<span class="pv-count" aria-hidden="true"></span>'
        + `<button type="button" class="pv-btn pv-original" aria-label="${W.original}">${ICON.original}</button></div>`
        + '<div class="pv-stage"><img class="pv-img" alt="" draggable="false">'
        + `<button type="button" class="pv-btn pv-side pv-prev" aria-label="${W.previous}">${ICON.prev}</button>`
        + `<button type="button" class="pv-btn pv-side pv-next" aria-label="${W.next}">${ICON.next}</button></div>`
        + '<div class="pv-foot"><p class="pv-caption"></p><p class="pv-sr" aria-live="polite"></p></div>'
      document.body.append(root)
      stage = root.querySelector('.pv-stage')
      img = root.querySelector('.pv-img')
      caption = root.querySelector('.pv-caption')
      count = root.querySelector('.pv-count')
      live = root.querySelector('.pv-sr')
      prev = root.querySelector('.pv-prev')
      next = root.querySelector('.pv-next')
      close = root.querySelector('.pv-close')
      original = root.querySelector('.pv-original')
      close.addEventListener('click', hide)
      prev.addEventListener('click', () => show(at - 1))
      next.addEventListener('click', () => show(at + 1))
      original.addEventListener('click', () => window.open(list[at].src, '_blank', 'noopener'))
      root.addEventListener('keydown', onKey)
      stage.addEventListener('pointerdown', onDown)
      stage.addEventListener('pointermove', onMove)
      stage.addEventListener('pointerup', onUp)
      stage.addEventListener('pointercancel', onUp)
      stage.addEventListener('dblclick', event => { if (event.target === img) toggleZoom(event.clientX, event.clientY) })
      stage.addEventListener('wheel', onWheel, { passive: false })
      window.addEventListener('resize', () => { if (!root.hidden) { measure(); clamp(); apply(false) } })
    }

    function measure() {
      const item = list[at]
      const W = stage.clientWidth
      const H = stage.clientHeight
      const pad = W >= 700 ? 24 : 0
      const scale = Math.min((W - 2 * pad) / item.w, (H - 2 * pad) / item.h)
      fit = { w: item.w * scale, h: item.h * scale }
      fit.x = (W - fit.w) / 2
      fit.y = (H - fit.h) / 2
      img.style.width = `${fit.w}px`
      img.style.height = `${fit.h}px`
    }
    const maxZoom = () => Math.min(8, Math.max(3, (list[at].w / fit.w) * 2))
    function clamp() {
      const W = stage.clientWidth
      const H = stage.clientHeight
      const sw = fit.w * z
      const sh = fit.h * z
      tx = sw <= W ? (W - sw) / 2 - fit.x : Math.min(-fit.x, Math.max(W - sw - fit.x, tx))
      ty = sh <= H ? (H - sh) / 2 - fit.y : Math.min(-fit.y, Math.max(H - sh - fit.y, ty))
    }
    function apply(animated, dx = 0, dy = 0) {
      img.classList.toggle('is-animated', animated && !reduce())
      img.style.transform = `translate(${fit.x + tx + dx}px, ${fit.y + ty + dy}px) scale(${z})`
      stage.classList.toggle('is-zoomed', z > 1.01)
    }
    function zoomTo(value, px, py, animated) {
      const box = stage.getBoundingClientRect()
      const x = px - box.left
      const y = py - box.top
      const target = Math.min(maxZoom(), Math.max(1, value))
      const u = (x - fit.x - tx) / z
      const v = (y - fit.y - ty) / z
      tx = x - fit.x - target * u
      ty = y - fit.y - target * v
      z = target
      clamp()
      apply(animated)
    }
    function toggleZoom(px, py) {
      if (z > 1.01) zoomTo(1, px, py, true)
      else zoomTo(Math.max(2.5, list[at].w / fit.w), px, py, true)
    }
    function centre() {
      const box = stage.getBoundingClientRect()
      return [box.left + box.width / 2, box.top + box.height / 2]
    }

    function show(index) {
      if (index < 0 || index >= list.length) return
      at = index
      const item = list[at]
      img.src = item.src
      img.alt = item.alt
      z = 1; tx = 0; ty = 0
      measure()
      apply(false)
      caption.textContent = item.caption
      const many = list.length > 1
      count.textContent = many ? say(W.of, at + 1, list.length) : ''
      prev.hidden = !many || at === 0
      next.hidden = !many || at === list.length - 1
      live.textContent = (many ? say(W.photoOf, at + 1, list.length) + '. ' : '') + (item.caption || item.alt)
    }

    function open(items, index, from) {
      if (!root) build()
      list = items
      opener = from
      root.hidden = false
      document.documentElement.style.overflow = 'hidden'
      setInert(true)
      show(index)
      // Grows from its picture into place; with Reduce Motion it only fades.
      const tile = items[index].el.getBoundingClientRect()
      const box = stage.getBoundingClientRect()
      if (!reduce() && tile.width) {
        img.classList.remove('is-animated')
        img.style.transform = `translate(${tile.left - box.left}px, ${tile.top - box.top}px) scale(${tile.width / fit.w})`
        img.getBoundingClientRect()
      }
      requestAnimationFrame(() => { root.classList.add('is-open'); apply(true) })
      close.focus()
    }

    function hide() {
      // Focus goes back to the picture now shown, the one the reader is at.
      const shown = list[at].el
      const back = opener && opener.closest('figure') === shown.closest('figure') ? opener : shown
      root.classList.remove('is-open')
      const finish = () => {
        root.hidden = true
        document.documentElement.style.overflow = ''
        setInert(false)
        back?.focus()
      }
      if (reduce()) { setTimeout(finish, 150); return }
      const tile = shown.getBoundingClientRect()
      const box = stage.getBoundingClientRect()
      if (tile.width && z <= 1.01) {
        img.classList.add('is-animated')
        img.style.transform = `translate(${tile.left - box.left}px, ${tile.top - box.top}px) scale(${tile.width / fit.w})`
      }
      setTimeout(finish, 220)
    }

    function onKey(event) {
      const key = event.key
      if (key === 'Escape') { event.preventDefault(); hide(); return }
      if (key === 'Tab') {
        const stops = [close, original, prev, next].filter(button => !button.hidden && button.offsetParent !== null)
        const position = stops.indexOf(document.activeElement)
        event.preventDefault()
        const step = event.shiftKey ? -1 : 1
        stops[(position + step + stops.length) % stops.length].focus()
        return
      }
      const zoomed = z > 1.01
      const [cx, cy] = centre()
      if (key === 'ArrowLeft' || key === 'ArrowRight') {
        event.preventDefault()
        if (zoomed) { tx += key === 'ArrowLeft' ? 80 : -80; clamp(); apply(true) } else show(at + (key === 'ArrowLeft' ? -1 : 1))
      } else if ((key === 'ArrowUp' || key === 'ArrowDown') && zoomed) {
        event.preventDefault(); ty += key === 'ArrowUp' ? 80 : -80; clamp(); apply(true)
      } else if (key === '+' || key === '=') { event.preventDefault(); zoomTo(z * 1.6, cx, cy, true) }
      else if (key === '-') { event.preventDefault(); zoomTo(z / 1.6, cx, cy, true) }
      else if (key === '0') { event.preventDefault(); zoomTo(1, cx, cy, true) }
    }

    const pointers = new Map()
    let gesture = null
    let lastTap = 0
    function onDown(event) {
      if (event.target.closest('.pv-btn')) return
      stage.setPointerCapture(event.pointerId)
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY })
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()]
        gesture = { pinch: true, d: Math.hypot(a.x - b.x, a.y - b.y), z0: z }
      } else {
        gesture = { pinch: false, x: event.clientX, y: event.clientY, tx, ty, moved: false, onImage: event.target === img }
      }
    }
    function onMove(event) {
      if (!pointers.has(event.pointerId) || !gesture) return
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY })
      if (gesture.pinch && pointers.size === 2) {
        const [a, b] = [...pointers.values()]
        zoomTo(gesture.z0 * Math.hypot(a.x - b.x, a.y - b.y) / gesture.d, (a.x + b.x) / 2, (a.y + b.y) / 2, false)
        return
      }
      const dx = event.clientX - gesture.x
      const dy = event.clientY - gesture.y
      if (Math.abs(dx) + Math.abs(dy) > 6) gesture.moved = true
      if (z > 1.01) {
        stage.classList.add('is-panning')
        tx = gesture.tx + dx; ty = gesture.ty + dy; clamp(); apply(false)
      } else if (gesture.moved) {
        apply(false, dx, Math.abs(dy) > Math.abs(dx) && dy > 0 ? dy : 0)
      }
    }
    function onUp(event) {
      if (!pointers.has(event.pointerId)) return
      pointers.delete(event.pointerId)
      stage.classList.remove('is-panning')
      if (!gesture || gesture.pinch) { if (!pointers.size) gesture = null; return }
      const dx = event.clientX - gesture.x
      const dy = event.clientY - gesture.y
      const g = gesture
      gesture = null
      if (z > 1.01) return
      if (g.moved) {
        if (dy > 110 && dy > Math.abs(dx)) { hide(); return }
        if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) {
          const target = at + (dx < 0 ? 1 : -1)
          if (target >= 0 && target < list.length) { show(target); return }
        }
        apply(true)
        return
      }
      if (event.pointerType === 'touch') {
        const now = Date.now()
        if (now - lastTap < 320 && g.onImage) { lastTap = 0; toggleZoom(event.clientX, event.clientY); return }
        lastTap = now
      } else if (!g.onImage) {
        hide() // a click beside the picture closes, as a sheet does
      }
    }
    function onWheel(event) {
      event.preventDefault()
      if (event.ctrlKey || event.metaKey) zoomTo(z * Math.exp(-event.deltaY * 0.01), event.clientX, event.clientY, false)
      else if (z > 1.01) { tx -= event.deltaX; ty -= event.deltaY; clamp(); apply(false) }
    }

    return { open }
  })()

  // An exported page holds its pictures as data: addresses, which a
  // browser does not open as a page: each picture of a group opens here,
  // by click, Return or Space, and is named for what it does.
  function entriesOf(group) {
    return [...group.querySelectorAll('figure img')].map(picture => ({
      el: picture,
      src: picture.currentSrc || picture.src,
      alt: picture.alt,
      caption: picture.closest('figure').querySelector('figcaption')?.textContent || '',
      w: picture.naturalWidth || 1600,
      h: picture.naturalHeight || 1200,
    }))
  }

  function openFrom(picture) {
    const group = picture.closest('.e21-imagegroup')
    const entries = entriesOf(group)
    Viewer.open(entries, Math.max(0, entries.findIndex(entry => entry.el === picture)), picture)
  }

  document.querySelectorAll('.e21-imagegroup figure img').forEach(picture => {
    picture.tabIndex = 0
    picture.setAttribute('role', 'button')
    // A function, so a "$&" in a description is not a replacement pattern.
    picture.setAttribute('aria-label', picture.alt ? W.enlargeNamed.replace('{alt}', () => picture.alt) : W.enlarge)
    picture.dataset.pgOpen = ''
    picture.addEventListener('click', event => {
      if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
      openFrom(picture)
    })
    picture.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openFrom(picture) }
    })
  })

  document.documentElement.dataset.photoViewer = ''
})()
