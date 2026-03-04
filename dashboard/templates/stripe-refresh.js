/**
 * stripe-refresh.js
 * Loaded after the existing inline dashboard scripts.
 * Adds: CRT cleanup, grid cross-markers, scroll reveals, keyboard shortcuts.
 */
(function () {
  'use strict';

  /* ─── 1. Remove CRT effects on load ─────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', function () {
    ['phosphor-bg'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.style.display = 'none';
    });
    document.querySelectorAll('.crt-scanlines, .crt-vignette').forEach(function (el) {
      el.style.display = 'none';
    });
  });

  /* ─── 2. Grid cross-marker generation ───────────────────────────────── */
  var crossSVG = '<svg width="8" height="8" viewBox="0 0 8 8" fill="none">'
    + '<path d="M3.5 4.5V8H4.5V4.5H8V3.5H4.5V0H3.5V3.5H0V4.5H3.5Z" fill="currentColor"/>'
    + '</svg>';

  function initGridCrosses() {
    var overlay = document.getElementById('grid-overlay');
    if (!overlay) return;

    var cols = 7; // 6 columns = 7 intersection points (including edges)
    var rows = [0, 280, 560]; // pixel offsets: top, mid, lower

    rows.forEach(function (rowY, ri) {
      for (var ci = 0; ci < cols; ci++) {
        var cross = document.createElement('div');
        cross.className = 'grid-cross';
        cross.innerHTML = crossSVG;
        cross.style.position = 'absolute';
        cross.style.left = (ci / (cols - 1) * 100) + '%';
        cross.style.top = rowY + 'px';
        cross.style.transform = 'translate(-50%, -50%)';
        cross.style.animationDelay = ((ri * cols + ci) * 0.05) + 's';
        overlay.appendChild(cross);
      }
    });
  }

  /* ─── 3. Intersection Observer for scroll-triggered reveals ──────────── */
  var _scrollObserver = null;

  function initScrollReveals() {
    _scrollObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('sr-visible');
        }
      });
    }, { threshold: 0.08, rootMargin: '0px 0px -40px 0px' });

    document.querySelectorAll('.section, .card, .mon-card, .research-card').forEach(function (el) {
      _scrollObserver.observe(el);
    });
  }

  /* ─── 4. Keyboard shortcuts for tab switching ────────────────────────── */
  function initKeyboardShortcuts() {
    document.addEventListener('keydown', function (e) {
      // Don't trigger when typing in inputs
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

      switch (e.key.toLowerCase()) {
        case 't':
          if (typeof switchTab === 'function') switchTab('trading');
          break;
        case 'r':
          if (typeof switchTab === 'function') switchTab('research');
          break;
        case 'm':
          if (typeof switchTab === 'function') switchTab('monitor');
          break;
      }
    });
  }

  /* ─── 5. MutationObserver — re-observe dynamically added elements ──── */
  function initMutationWatcher() {
    if (!_scrollObserver) return;

    var mo = new MutationObserver(function (mutations) {
      mutations.forEach(function (mutation) {
        mutation.addedNodes.forEach(function (node) {
          if (node.nodeType !== 1) return; // element nodes only

          // Check the node itself
          if (node.matches && node.matches('.section, .card, .mon-card, .research-card')) {
            _scrollObserver.observe(node);
          }

          // Check descendants
          if (node.querySelectorAll) {
            node.querySelectorAll('.section, .card, .mon-card, .research-card').forEach(function (el) {
              _scrollObserver.observe(el);
            });
          }
        });
      });
    });

    mo.observe(document.body, { childList: true, subtree: true });
  }

  /* ─── 6. Bootstrap on DOMContentLoaded ──────────────────────────────── */
  document.addEventListener('DOMContentLoaded', function () {
    initGridCrosses();
    initScrollReveals();
    initKeyboardShortcuts();
    initMutationWatcher();
  });
}());
