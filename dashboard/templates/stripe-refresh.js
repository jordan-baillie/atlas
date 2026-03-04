/**
 * stripe-refresh.js — Stripe Dev Blog style animation system
 *
 * Animation philosophy (from stripe.dev):
 *   - Single easing: cubic-bezier(0.19, 1, 0.22, 1) — easeOutExpo
 *   - Everything transitions, nothing snaps
 *   - Subtle, purposeful motion — never decorative
 *   - Staggered reveals create rhythm
 */
(function () {
  'use strict';

  // ── The Stripe easing curve ──────────────────────────────────────────
  // cubic-bezier(0.19, 1, 0.22, 1) — fast start, smooth exponential land
  // Used for: accordions (0.6s), chevrons (0.3s), general transitions (0.3s)

  /* ═══ 1. CRT effect removal ═══════════════════════════════════════════ */

  function removeCRTEffects() {
    var ids = ['phosphor-bg'];
    ids.forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.style.display = 'none';
    });
    document.querySelectorAll('.crt-scanlines, .crt-vignette').forEach(function (el) {
      el.style.display = 'none';
    });
  }

  /* ═══ 2. Grid cross-marker generation + parallax ══════════════════════ */

  var crossSVG =
    '<svg width="8" height="8" viewBox="0 0 8 8" fill="none">' +
    '<path d="M3.5 4.5V8H4.5V4.5H8V3.5H4.5V0H3.5V3.5H0V4.5H3.5Z" fill="currentColor"/>' +
    '</svg>';

  var gridCrosses = [];

  function initGridCrosses() {
    var overlay = document.getElementById('grid-overlay');
    if (!overlay) return;

    var cols = 7;
    var rowOffsets = [60, 320, 580];

    rowOffsets.forEach(function (rowY, ri) {
      for (var ci = 0; ci < cols; ci++) {
        var cross = document.createElement('div');
        cross.className = 'grid-cross';
        cross.innerHTML = crossSVG;
        cross.style.position = 'absolute';
        cross.style.left = ci / (cols - 1) * 100 + '%';
        cross.style.top = rowY + 'px';
        cross.style.transform = 'translate(-50%, -50%)';
        cross.style.animationDelay = (ri * cols + ci) * 0.04 + 's';
        cross._baseY = rowY;
        cross._speed = 0.02 + Math.random() * 0.03; // subtle parallax factor
        overlay.appendChild(cross);
        gridCrosses.push(cross);
      }
    });
  }

  /* ═══ 3. Grid cross parallax on scroll ════════════════════════════════ */

  var ticking = false;

  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      var scrollY = window.scrollY || window.pageYOffset;
      for (var i = 0; i < gridCrosses.length; i++) {
        var c = gridCrosses[i];
        var offsetY = c._baseY - scrollY * c._speed;
        c.style.top = offsetY + 'px';
      }
      ticking = false;
    });
  }

  /* ═══ 4. Scroll-triggered reveals (IntersectionObserver) ══════════════ */

  var scrollObserver = null;
  var staggerCounters = {}; // track stagger per parent

  function initScrollReveals() {
    scrollObserver = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;

          var el = entry.target;

          // Stagger siblings that become visible together
          var parent = el.parentElement;
          var parentId = parent ? parent.id || parent.className.slice(0, 30) : 'root';
          if (!staggerCounters[parentId]) staggerCounters[parentId] = 0;

          var delay = staggerCounters[parentId] * 0.06;
          staggerCounters[parentId]++;

          // Cap delay so it never feels slow
          if (delay > 0.4) delay = 0;

          el.style.transitionDelay = delay + 's';
          el.classList.add('sr-visible');

          // Clean up after animation completes
          setTimeout(function () {
            el.style.transitionDelay = '0s';
            staggerCounters[parentId] = 0;
          }, (delay + 0.6) * 1000);
        });
      },
      { threshold: 0.06, rootMargin: '0px 0px -60px 0px' }
    );

    var targets = '.section, .card, .mon-card, .research-card, .pnl-item, .metric';
    document.querySelectorAll(targets).forEach(function (el) {
      scrollObserver.observe(el);
    });
  }

  /* ═══ 5. KPI value count-up animation ═════════════════════════════════ */

  function animateCountUp(el) {
    var text = el.textContent;
    // Only animate numeric values (currency/percentage)
    var match = text.match(/^([^0-9]*)([\d,]+\.?\d*)(.*)/);
    if (!match) return;

    var prefix = match[1];
    var numStr = match[2];
    var suffix = match[3];
    var target = parseFloat(numStr.replace(/,/g, ''));
    if (isNaN(target) || target === 0) return;

    var decimals = numStr.includes('.') ? numStr.split('.')[1].length : 0;
    var hasCommas = numStr.includes(',');
    var startTime = null;
    var duration = 800; // ms

    function formatNum(n) {
      var s = n.toFixed(decimals);
      if (hasCommas) {
        var parts = s.split('.');
        parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
        s = parts.join('.');
      }
      return s;
    }

    el.classList.add('counting');

    function step(timestamp) {
      if (!startTime) startTime = timestamp;
      var progress = Math.min((timestamp - startTime) / duration, 1);
      // easeOutExpo curve
      var eased = progress === 1 ? 1 : 1 - Math.pow(2, -10 * progress);
      var current = target * eased;
      el.textContent = prefix + formatNum(current) + suffix;

      if (progress < 1) {
        requestAnimationFrame(step);
      } else {
        el.textContent = text; // restore exact original
        el.classList.remove('counting');
      }
    }

    requestAnimationFrame(step);
  }

  function initCountUps() {
    // Observe card values — animate when they first become visible
    var valueObserver = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          animateCountUp(entry.target);
          valueObserver.unobserve(entry.target);
        });
      },
      { threshold: 0.5 }
    );

    document.querySelectorAll('.card-value').forEach(function (el) {
      valueObserver.observe(el);
    });
  }

  /* ═══ 6. Keyboard shortcuts ═══════════════════════════════════════════ */

  function initKeyboardShortcuts() {
    document.addEventListener('keydown', function (e) {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;

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

  /* ═══ 7. MutationObserver — re-observe dynamically added elements ═══ */

  function initMutationWatcher() {
    if (!scrollObserver) return;

    var mo = new MutationObserver(function (mutations) {
      mutations.forEach(function (mutation) {
        mutation.addedNodes.forEach(function (node) {
          if (node.nodeType !== 1) return;

          var sel = '.section, .card, .mon-card, .research-card, .pnl-item, .metric';

          if (node.matches && node.matches(sel)) {
            scrollObserver.observe(node);
          }
          if (node.querySelectorAll) {
            node.querySelectorAll(sel).forEach(function (el) {
              scrollObserver.observe(el);
            });
            // Also observe new card-values for count-up
            node.querySelectorAll('.card-value').forEach(function (el) {
              animateCountUp(el);
            });
          }
        });
      });
    });

    mo.observe(document.body, { childList: true, subtree: true });
  }

  /* ═══ 8. Smooth section toggle override ═══════════════════════════════ */
  // Enhance the existing toggle() function for smoother collapse/expand

  function enhanceSectionToggle() {
    // Wrap the existing toggle function to add height animation
    if (typeof window.toggle !== 'function') return;

    var originalToggle = window.toggle;
    window.toggle = function (id) {
      var body = document.getElementById('body-' + id);
      if (!body) return originalToggle(id);

      var isCollapsed = body.classList.contains('collapsed');

      if (isCollapsed) {
        // Expanding: measure target height, animate from 0
        body.classList.remove('collapsed');
        body.style.display = '';
        var targetHeight = body.scrollHeight;
        body.style.maxHeight = '0px';
        body.style.opacity = '0';
        // Force reflow
        body.offsetHeight; // eslint-disable-line no-unused-expressions
        body.style.maxHeight = targetHeight + 'px';
        body.style.opacity = '1';

        // Clean up after animation
        setTimeout(function () {
          body.style.maxHeight = '';
        }, 650);
      } else {
        // Collapsing: animate to 0, then add collapsed class
        body.style.maxHeight = body.scrollHeight + 'px';
        body.offsetHeight; // eslint-disable-line
        body.style.maxHeight = '0px';
        body.style.opacity = '0';

        setTimeout(function () {
          body.classList.add('collapsed');
          body.style.maxHeight = '';
          body.style.opacity = '';
        }, 650);
      }

      // Rotate chevron
      var chev = document.getElementById('chev-' + id);
      if (chev) {
        chev.classList.toggle('down');
        chev.classList.toggle('right');
      }
    };
  }

  /* ═══ 9. Smooth tab switching with crossfade ══════════════════════════ */

  function enhanceTabSwitch() {
    if (typeof window.switchTab !== 'function') return;

    var originalSwitchTab = window.switchTab;
    window.switchTab = function (tabId) {
      // Fade out current active tab content
      var activeContent = document.querySelector('.tab-content.active');
      if (activeContent && activeContent.id !== 'tab-' + tabId) {
        activeContent.style.opacity = '0';
        activeContent.style.transform = 'translateY(8px)';

        setTimeout(function () {
          originalSwitchTab(tabId);
          var newContent = document.querySelector('.tab-content.active');
          if (newContent) {
            newContent.style.opacity = '0';
            newContent.style.transform = 'translateY(8px)';
            // Force reflow
            newContent.offsetHeight; // eslint-disable-line
            newContent.style.opacity = '';
            newContent.style.transform = '';

            // Re-trigger scroll reveals for newly visible content
            if (scrollObserver) {
              newContent.querySelectorAll('.section, .card, .mon-card, .research-card').forEach(function (el) {
                el.classList.remove('sr-visible');
                scrollObserver.observe(el);
              });
            }
          }
        }, 200);
      } else {
        originalSwitchTab(tabId);
      }
    };
  }

  /* ═══ 10. Hover arrow on section headers ══════════════════════════════ */
  // Add a subtle → arrow that fades in on section header hover (Stripe pattern)

  function initHoverArrows() {
    document.querySelectorAll('.section-head').forEach(function (head) {
      var arrow = document.createElement('span');
      arrow.className = 'section-hover-arrow';
      arrow.textContent = '→';
      arrow.style.cssText =
        'opacity:0; transition:opacity 0.15s linear; margin-left:8px; font-size:12px; color:var(--text-tertiary);';
      head.querySelector('.section-title').appendChild(arrow);

      head.addEventListener('mouseenter', function () {
        arrow.style.opacity = '0.6';
      });
      head.addEventListener('mouseleave', function () {
        arrow.style.opacity = '0';
      });
    });
  }

  /* ═══ Bootstrap ═══════════════════════════════════════════════════════ */

  document.addEventListener('DOMContentLoaded', function () {
    removeCRTEffects();
    initGridCrosses();
    initScrollReveals();
    initCountUps();
    initKeyboardShortcuts();
    initMutationWatcher();
    initHoverArrows();

    // Enhance existing functions (must run after inline scripts)
    setTimeout(function () {
      enhanceSectionToggle();
      enhanceTabSwitch();
    }, 100);

    // Start scroll listener for parallax
    window.addEventListener('scroll', onScroll, { passive: true });
  });
})();
