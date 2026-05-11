# Atlas Dashboard — Design Tokens Reference

> **Authoritative source**: `src/index.css` (`@theme` block + light-mode overrides).
> This document is generated from the actual token values — do not edit tokens here;
> update `index.css` and regenerate this doc.

---

## 1. Color Palette

### Base / Background

| Token | Dark (default) | Light (`html.light`) | Role |
|-------|---------------|---------------------|------|
| `--color-bg` | `#09090b` | `#fafafa` | Page background (outermost layer) |
| `--color-surface` | `#161618` | `#ffffff` | Card / panel surface (1 level up from bg) |
| `--color-surface-alt` | `#27272a` | `#f4f4f5` | Secondary surface (table rows, code blocks, chips) |

**Rule**: `surface` sits on top of `bg`. `surface-alt` sits on top of `surface` for nested content (e.g., table alternating rows, sub-chips). Never use raw hex — always reference the token so light mode works automatically.

### Borders & Separators

| Token | Dark | Light | Role |
|-------|------|-------|------|
| `--color-border` | `#2a2a2e` | `#e4e4e7` | Card borders, dividers, table rules |

**Rule**: Cards use `border` OR `shadow` for depth. `dash-card` uses both (shadow on hover), which is intentional — the border provides structure at rest, the shadow provides elevation on interaction.

### Text

| Token | Dark | Light | Role |
|-------|------|-------|------|
| `--color-text` | `#fafafa` | `#09090b` | Primary text (headings, values) |
| `--color-text-muted` | `#a1a1aa` | `#52525b` | Secondary text (labels, descriptions, metadata) |
| `--color-muted` | `#a1a1aa` | `#52525b` | Alias of `text-muted`; used in section headers |

### Accent & Brand

| Token | Value | Role |
|-------|-------|------|
| `--color-accent` | `#6366f1` (indigo-500) | Primary brand accent, focus rings, section left-borders |

### Semantic Status Colors

| Token | Dark value | Light value | Role |
|-------|-----------|------------|------|
| `--color-green` | `#22c55e` | _(same)_ | Raw green; positive metrics |
| `--color-red` | `#ef4444` | _(same)_ | Raw red; negative metrics |
| `--color-amber` | `#f59e0b` | _(same)_ | Raw amber; warnings / caution |
| `--color-positive` | `#22c55e` | `#16a34a` | P&L positive (darkens in light mode for contrast) |
| `--color-negative` | `#ef4444` | `#dc2626` | P&L negative (darkens in light mode) |

### Semantic Aliases (added C1/C2/C6)

These are the **preferred** tokens for new component work. They decouple
semantic meaning from palette specifics:

| Token | Resolves to | Use when |
|-------|------------|---------|
| `--color-success` | `var(--color-green)` | System healthy, profit, gain, approved |
| `--color-warning` | `var(--color-amber)` | Caution, degraded, borderline |
| `--color-danger` | `var(--color-red)` | Error, loss, rejected, halted |
| `--color-info` | `var(--color-investment)` → `#3b82f6` | Informational, live data, research/neutral states |

### Domain-Specific Colors

| Token | Value | Role |
|-------|-------|------|
| `--color-savings` | `#22c55e` | Finance tab: savings category |
| `--color-spending` | `#f59e0b` | Finance tab: spending category |
| `--color-investment` | `#3b82f6` (blue-500) | Finance tab: investment category; also source of `--color-info` |

### Chart Series Colors

| Token | Dark | Light | Role |
|-------|------|-------|------|
| `--color-series-portfolio` | `#22c55e` | `#16a34a` | Equity curve / portfolio series line |
| `--color-series-benchmark` | `#a1a1aa` | `#71717a` | Benchmark comparison series |
| `--color-series-grid` | `#2a2a2e` | `#e4e4e7` | Chart grid lines |

---

## 2. Typography Scale

All body text uses `--font-sans` ("DM Sans"). All numeric content uses `--font-mono`
("JetBrains Mono") with `tabular-nums` (the `.tabular` utility class, or Tailwind's
`tabular-nums` class, or `font-variant-numeric: tabular-nums` inline).

### Size Ramp

| Size | Tailwind class | Usage |
|------|---------------|-------|
| 9px | `text-[9px]` | Badge/pill labels (xs), AsOfBadge |
| 10px | `text-[10px]` | Micro labels, stat card headers, chip text |
| 11px | `text-[11px]` | Badge md variant, compact secondary |
| 12px | `text-xs` (0.75rem) | Small body, table cells, ChartTooltip labels |
| 13px | `text-[13px]` | EmptyState headings, compact section content |
| 14px | `text-sm` (0.875rem) | Body text, default table content, button labels |
| 16px | `text-base` (1rem) | Default paragraph text |
| 20px | `text-xl` (1.25rem) | StatCard value (non-hero) |
| 24px | `text-2xl` (1.5rem) | Section headings |
| 30px | `text-3xl` (1.875rem) | StatCard hero value |
| 32px+ | `text-4xl+` | Reserved for major dashboard KPI numbers |

### Weight Scale

| Weight | Class | Usage |
|--------|-------|-------|
| 400 | `font-normal` | Body text, descriptions |
| 500 | `font-medium` | Tooltip values, secondary emphasis |
| 600 | `font-semibold` | Labels, table headers, chip text |
| 700 | `font-bold` | Hero stat values, primary headings |

### Numeric Typography Rule

Any number that changes over time (P&L, prices, percentages, counts) must use:
```tsx
className="font-mono tabular-nums"   // Tailwind
className="font-mono tabular"        // using .tabular utility class
```
This prevents layout jitter as digits change width. Apply at the container level when possible.

---

## 3. Spacing Scale

Base unit: **4px**. All spacing follows the 4px grid.

| Token | Value | Tailwind equivalent |
|-------|-------|-------------------|
| `--space-xs` | 4px | `p-1`, `gap-1` |
| `--space-sm` | 8px | `p-2`, `gap-2` |
| `--space-md` | 12px | `p-3`, `gap-3` |
| `--space-lg` | 16px | `p-4`, `gap-4` |
| `--space-xl` | 24px | `p-6`, `gap-6` |

### Ramp (for reference when CSS vars aren't available)

`4 / 8 / 12 / 16 / 24 / 32 / 48`

### Card Padding Convention

- `.dash-card` → `1rem` (16px) — standard card
- `.dash-card-tight` → `0.75rem` (12px) — compact stat/grid card

---

## 4. Border Radius

The project uses a **6/10/16 scale** (not a 4/6/8/12 scale as some design systems use).
Do not introduce intermediate values — pick the nearest defined token.

| Token | Value | Usage |
|-------|-------|-------|
| `--radius-sm` | `6px` | Small chips, badges, pills, skeleton blocks |
| `--radius-md` | `10px` | Cards (`dash-card`), modals, popovers |
| `--radius-lg` | `16px` | Large panels, full-page overlays |
| `rounded-full` (Tailwind) | `9999px` | Pills, status dots, circular avatars |

---

## 5. Shadow Scale

Three elevation levels. Shadow opacity adapts between light and dark themes.

| Token | Dark value | Light value | Usage |
|-------|-----------|------------|-------|
| `--shadow-sm` | `0 1px 2px rgba(0,0,0,0.18)` | `0 1px 2px rgba(0,0,0,0.04)` | Card at rest |
| `--shadow-md` | `0 4px 12px rgba(0,0,0,0.28)` | `0 4px 12px rgba(0,0,0,0.08)` | Card on hover, dropdown |
| `--shadow-lg` | `0 8px 25px rgba(0,0,0,0.38)` | `0 8px 25px rgba(0,0,0,0.12)` | Modal / overlay |

**Card rule**: `.dash-card` rests at `shadow-sm` + `border`. On hover, shadow promotes
to `shadow-md` and border shifts to `color-mix(in srgb, --color-accent 30%, --color-border)`.
This means cards have both border AND shadow — the border provides clear structure at rest,
shadow adds focus on hover. This is intentional and should not be simplified to border-only.

---

## 6. Motion / Animation

| Token | Value | Usage |
|-------|-------|-------|
| `--transition-fast` | `150ms ease` | Micro-interactions (dot color, opacity) |
| `--transition-normal` | `250ms cubic-bezier(0.16, 1, 0.3, 1)` | Card hover, modal enter, tab switch |
| `--transition-slow` | `400ms cubic-bezier(0.16, 1, 0.3, 1)` | Large layout shifts, accordion open |

### Keyframes

| Name | Usage |
|------|-------|
| `dash-fade-in` | Tab content enter (`.animate-in` class, 200ms) |
| `fadeSlideUp` | Legacy entry animation (12px Y slide) |
| `shimmer` | Skeleton loading gradient sweep |
| `status-pulse` | StatusDot live indicator (`.status-pulse` CSS class) |

### Stagger System

Wrap a list container in `.stagger` to get nth-child delays (0ms → 550ms in 50ms steps,
up to 12 children). Used for tab content reveal animations.

### Reduced Motion

`@media (prefers-reduced-motion: reduce)` disables all animations globally.
Respect this — do not use `animation-duration: 0ms` with `!important` on new keyframes
outside the reduced-motion block, because the block already handles `*, *::before, *::after`.

---

## 7. Semantic Component Guidance

### Surface vs Surface-alt

```
Page bg (--color-bg)
 └── Card (--color-surface)          ← dash-card
      └── Nested element (--color-surface-alt)  ← chip, badge bg, table alt row
```

Use `surface-alt` for elements that need to sit visually "inside" a card without
getting their own border. Example: the sub-chip in StatCard, table row alt background.

### When to use Border vs Shadow for cards

- **Border only**: Not used for `dash-card` (too flat)
- **Shadow only**: Not used for `dash-card` (floats without anchor)
- **Both** (current `dash-card`): Border provides crisp structural edge in dark mode;
  shadow adds depth. This is the standard. Do not remove either.

### Numeric Display Rule

```tsx
// Always combine all three for monetary/percentage values:
<span className="font-mono font-semibold tabular-nums">
  {fmtSignedCcy(pnl)}
</span>
```

For statCard hero values, use `font-bold` instead of `font-semibold`.

### Badge / Pill Hierarchy

```
Badge (semantic pill)
 ├── variant: success | warning | danger | info | neutral | accent
 ├── size: xs (9px) | sm (10px) | md (11px)
 └── optional: dot, icon

Pill (= Badge with dot=true, size="xs" defaults)
 └── thin wrapper over Badge — use for inline "🟢 Live" type indicators

AsOfBadge (data provenance)
 └── wraps Badge: live → info variant, snapshot → neutral variant
```

### StatusDot

Colored dot with optional pulse animation for live indicators:
```tsx
<StatusDot status="green" size="md" pulse />  // live market indicator
<StatusDot status="amber" />                   // warning (6px default)
```

Sizes: sm=6px (default), md=8px, lg=10px.

---

## Appendix: Token Quick Reference

```css
/* Paste into any component comment for quick lookup */
--color-bg           /* outermost page background */
--color-surface      /* card background */
--color-surface-alt  /* nested chip / row background */
--color-border       /* card border, divider */
--color-text         /* primary text */
--color-text-muted   /* secondary / label text */
--color-accent       /* brand indigo #6366f1 */
--color-success      /* green — positive, healthy */
--color-warning      /* amber — caution, borderline */
--color-danger       /* red — error, loss, rejected */
--color-info         /* blue #3b82f6 — info, live data */
--color-positive     /* P&L positive (adapts to light mode) */
--color-negative     /* P&L negative (adapts to light mode) */
--radius-sm / --radius-md / --radius-lg   /* 6 / 10 / 16px */
--shadow-sm / --shadow-md / --shadow-lg   /* card / hover / modal */
--transition-fast / --transition-normal / --transition-slow  /* 150/250/400ms */
--space-xs / --space-sm / --space-md / --space-lg / --space-xl  /* 4/8/12/16/24px */
```
