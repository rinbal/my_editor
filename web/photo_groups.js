// SPDX-FileCopyrightText: 2026 rinbal
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The swipe row of a photo group on a phone, in an exported page: the dots,
// the spoken position and the arrow keys (the row itself swipes with CSS
// alone). Taken from the photo grid reference (reference/build/inner.js,
// "Variant C") until STANDUP ships backend/htmlgen/photo_groups.js; the
// words come from the page (export_html.py), in the app's language.
(function () {
  const W = JSON.parse(document.getElementById('e21-photo-words').textContent)
  const say = (words, k, n) => words.replace('{k}', () => k).replace('{n}', () => n)
  // ── Variant C: the swipe strip on phones ──────────────────────────────
  // Without script the strip still swipes (CSS scroll snap), and Tab moves
  // through its pictures. The script only adds what scrolling cannot say:
  // the dots, the spoken position, the arrow keys on the strip.
  const MAX_DOTS = 10
  function strips(group) {
    group.querySelectorAll('.e21-pg-run[data-phone="strip"]').forEach(seg => {
      const figures = [...seg.querySelectorAll('figure')]
      const n = figures.length
      const pager = document.createElement('div')
      pager.className = 'e21-pg-pager'
      pager.hidden = true
      const dots = n <= MAX_DOTS
        ? figures.map((fig, k) => {
          const dot = document.createElement('button')
          dot.type = 'button'
          dot.className = 'e21-pg-dot'
          dot.setAttribute('aria-label', say(W.photoOf, k + 1, n))
          dot.addEventListener('click', () => go(k))
          return dot
        })
        : []
      const count = document.createElement('span')
      count.className = 'e21-pg-count'
      count.setAttribute('aria-hidden', 'true')
      const live = document.createElement('p')
      live.className = 'e21-pg-sr'
      live.setAttribute('aria-live', 'polite')
      pager.append(...(dots.length ? dots : [count]), live)
      seg.after(pager)

      let current = -1
      let target = null
      let spoken = false
      const reduce = () => matchMedia('(prefers-reduced-motion: reduce)').matches
      const snapLine = () => seg.getBoundingClientRect().left + (parseFloat(getComputedStyle(seg).scrollPaddingLeft) || 0)
      function go(k) {
        k = Math.max(0, Math.min(n - 1, k))
        target = k
        spoken = true
        const left = figures[k].getBoundingClientRect().left - snapLine() + seg.scrollLeft
        seg.scrollTo({ left, behavior: reduce() ? 'auto' : 'smooth' })
      }
      function update() {
        const line = snapLine()
        let best = 0
        let distance = Infinity
        figures.forEach((fig, k) => {
          const d = Math.abs(fig.getBoundingClientRect().left - line)
          if (d < distance) { distance = d; best = k }
        })
        // At the end of the strip the last pictures cannot reach the snap
        // line: the one asked for counts when it is fully in view.
        const atEnd = seg.scrollLeft >= seg.scrollWidth - seg.clientWidth - 1
        if (atEnd && target !== null) {
          const box = figures[target].getBoundingClientRect()
          const view = seg.getBoundingClientRect()
          if (box.left >= view.left - 1 && box.right <= view.right + 1) best = target
        }
        if (best === current) return
        current = best
        dots.forEach((dot, k) => dot.setAttribute('aria-current', String(k === best)))
        count.textContent = say(W.of, best + 1, n)
        if (spoken) live.textContent = say(W.photoOf, best + 1, n)
      }
      // Dots only while the strip really scrolls (a phone width).
      const fit = () => {
        const scrolls = seg.scrollWidth > seg.clientWidth + 1
        pager.hidden = !scrolls
        if (scrolls) {
          seg.tabIndex = 0
          seg.setAttribute('role', 'group')
          seg.setAttribute('aria-label', say(W.swipe, 0, n))
        } else {
          seg.removeAttribute('tabindex')
          seg.removeAttribute('role')
          seg.removeAttribute('aria-label')
        }
        current = -1
        update()
      }
      let frame = 0
      seg.addEventListener('scroll', () => { if (!frame) frame = requestAnimationFrame(() => { frame = 0; update() }) }, { passive: true })
      seg.addEventListener('pointerdown', () => { target = null; spoken = true })
      seg.addEventListener('keydown', event => {
        if (event.target !== seg) return
        const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key]
        if (step) { event.preventDefault(); go(current + step) }
        else if (event.key === 'Home') { event.preventDefault(); go(0) }
        else if (event.key === 'End') { event.preventDefault(); go(n - 1) }
      })
      // A picture reached with Tab scrolls into view by itself; say where.
      seg.addEventListener('focusin', event => { if (event.target !== seg) spoken = true })
      new ResizeObserver(fit).observe(seg)
      fit()
    })
  }

  document.querySelectorAll('.e21-imagegroup').forEach(strips)
})()
