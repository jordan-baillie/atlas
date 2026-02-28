/**
 * PhosphorErosion — Animated trading dashboard background
 *
 * A full-screen canvas component rendering Matrix rain + CRT terminal aesthetics
 * with real market data lexicon. Designed as a z-index:0 background layer with
 * pointer-events:none so dashboard content sits on top.
 *
 * Usage:
 *   <PhosphorErosion className="my-bg" />
 *
 * Layers (bottom → top):
 *   0. Background data grid (faint scrolling numbers)
 *   1. Matrix rain columns (falling characters with phosphor trails)
 *   2. Glitch clusters (flickering number-station digit groups)
 *   3. Terminal blocks (typing command output)
 *   4. CRT overlay (scanlines + vignette + flicker) — CSS
 */

import React, { useRef, useEffect, useCallback } from 'react';

// ─── Types ──────────────────────────────────────────────────────────────────
interface PhosphorErosionProps {
  className?: string;
  /** Seed for deterministic randomness (default: 42) */
  seed?: number;
  /** Number of rain columns (default: 55) */
  rainColumns?: number;
  /** Rain fall speed multiplier (default: 1.0) */
  rainSpeed?: number;
  /** Characters in each rain trail (default: 22) */
  trailLength?: number;
  /** Number of terminal output blocks (default: 4) */
  terminalBlocks?: number;
  /** Number of flickering digit clusters (default: 18) */
  glitchClusters?: number;
  /** Background grid opacity 0-1 (default: 0.055) */
  gridOpacity?: number;
  /** CRT scanline opacity 0-1 (default: 0.06) */
  scanlineOpacity?: number;
  /** Probability of CRT flicker per frame (default: 0.015) */
  crtFlicker?: number;
  /** Overall opacity — keep low so dashboard content reads (default: 0.85) */
  opacity?: number;
}

// ─── Market Data Lexicon ────────────────────────────────────────────────────
const TICKERS = [
  'BHP','CBA','WDS','FMG','CSL','NAB','WBC','ANZ','RIO','TLS',
  'AAPL','MSFT','NVDA','TSLA','AMZN','GOOG','META','SPY','QQQ','AMD',
];
const PRICES = [
  '$82.47','$4.23','$142.80','$35.67','$308.45','$28.91','$124.56',
  '$67.33','$18.92','$245.10','$3847.20','$172.50','$505.62','$238.45',
];
const PCTS = [
  '+2.31%','-0.87%','+1.45%','-2.13%','+0.56%','-1.78%','+3.21%',
  '-0.32%','+0.95%','-1.23%','+4.07%','-3.45%',
];
const VOLS = ['1.2M','845K','3.4M','256K','1.8M','567K','2.1M','432K','12.6M','98K'];
const TERM_MSGS = [
  '[EXEC] BUY BHP @ 42.15 x 500',
  '[EXEC] SELL FMG @ 18.92 x 1000',
  '[EXEC] BUY NVDA @ 505.62 x 200',
  '[EXEC] SELL CBA @ 124.56 x 800',
  '[EXEC] BUY AMD @ 172.50 x 350',
  '[SIGNAL] RSI(14) OVERSOLD :: CBA',
  '[SIGNAL] MACD CROSS :: MSFT',
  '[SIGNAL] BREAKOUT :: AMD +3.2%',
  '[SIGNAL] MEAN_REV TRIGGER :: WDS',
  '[SIGNAL] MOMENTUM SHIFT :: NVDA',
  '[SCAN] ASX200 MOMENTUM SHIFT DETECTED',
  '[SCAN] SP500 SECTOR ROTATION: TECH->ENERGY',
  '[SCAN] UNUSUAL VOLUME :: TSLA 12.6M',
  '[ATLAS] PORTFOLIO DELTA: +$3,247.83',
  '[ATLAS] STRATEGY: MEAN_REVERSION ACTIVE',
  '[ATLAS] POSITION SIZE: 2.3% RISK/TRADE',
  '[ATLAS] DAILY P&L: +$1,847.20',
  '[SYNC] S&P500 FEED CONNECTED',
  '[SYNC] ASX200 STREAM: ONLINE',
  '[SYNC] MOOMOO BROKER: HEARTBEAT OK',
  '[RISK] VAR(95) = $12,450 WITHIN LIMITS',
  '[RISK] MAX DRAWDOWN: -4.2% < THRESHOLD',
  '[RISK] CORRELATION SPIKE: BHP/RIO 0.94',
  '[OPT] SHARPE: 1.87 → 2.14',
  '[OPT] ANNEALING TEMP: 0.42 COOLING...',
  '[OPT] PARAM GRID: 847/2048 COMPLETE',
  '[FLOW] DARK POOL ACTIVITY :: NVDA',
  '[FLOW] BLOCK TRADE: SPY 45K SHARES',
  '[DATA] REFRESH COMPLETE: 500 SYMBOLS',
  '[DATA] OHLCV INTEGRITY CHECK: PASS',
];
const GRID_ITEMS = [
  'SMA20:4847.2','EMA50:4831.7','RSI14:62.4','MACD:12.8','ATR14:23.5',
  'BB_UP:4892.1','BB_LO:4802.3','VOL20:18.4%','BETA:1.12','SHARPE:1.87',
  'SORTINO:2.14','CALMAR:1.45','Δ0.67','Θ-0.04','Γ0.012','V23.5',
  'ρ0.82','σ0.185','μ0.043','λ1.24','R²0.87','ADX:34.2','OBV:2.4M',
  'MFI:58','CCI:112','VWAP:4839','STOCH:72','WLMS:-18','VIX:16.4',
  'IV30:22.1','HV20:18.7','SKEW:4.2','KURT:3.1','CORR:0.78','TRIN:1.04',
];
const STATION_PHRASES = [
  'EXECUTE SEQUENCE','ALPHA SIGNAL LOCKED','CORRELATION MATRIX ACTIVE',
  'REGIME CHANGE DETECTED','SIGNAL CONFIRMED','FREQUENCY ALIGNED',
  'PATTERN CONVERGENCE','EIGENVALUE SHIFT','DECODE COMPLETE',
  'SEQUENCE VERIFIED','TRANSMISSION LOCKED','MATRIX DECOMPOSITION',
];

// ─── Seeded PRNG (Mulberry32) ───────────────────────────────────────────────
function mulberry32(seed: number) {
  let s = seed | 0;
  return () => {
    s = (s + 0x6d2b79f5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ─── Color Constants ────────────────────────────────────────────────────────
const COL_GREEN: [number, number, number] = [0, 255, 65];
const COL_AMBER: [number, number, number] = [255, 176, 0];
const COL_CYAN: [number, number, number] = [0, 212, 255];

type Hue = 'green' | 'amber' | 'cyan';
const HUE_RGB: Record<Hue, [number, number, number]> = {
  green: COL_GREEN,
  amber: COL_AMBER,
  cyan: COL_CYAN,
};

// ─── Helper ─────────────────────────────────────────────────────────────────
function pick<T>(arr: T[], rng: () => number): T {
  return arr[Math.floor(rng() * arr.length)];
}

function randChar(rng: () => number): string {
  const r = rng();
  if (r < 0.38) return String.fromCharCode(48 + Math.floor(rng() * 10));
  if (r < 0.62) return String.fromCharCode(65 + Math.floor(rng() * 26));
  if (r < 0.78) return pick(['$','%','.','+','-','/','@',':','#','*'], rng);
  if (r < 0.90) return pick(['Δ','Θ','Σ','Γ','α','β','μ','σ','ρ','λ'], rng);
  return pick(['↑','↓','→','←','░','▒','▓','█','■','◆'], rng);
}

function pickHue(rng: () => number): Hue {
  const r = rng();
  if (r < 0.60) return 'green';
  if (r < 0.85) return 'amber';
  return 'cyan';
}

function rgba(c: [number, number, number], a: number): string {
  return `rgba(${c[0]},${c[1]},${c[2]},${Math.max(0, Math.min(1, a)).toFixed(3)})`;
}

// ─── Entities ───────────────────────────────────────────────────────────────

interface RainColumn {
  x: number;
  fs: number;
  spacing: number;
  speed: number;
  headY: number;
  trail: number;
  alpha: number;
  hue: Hue;
  chars: string[];
  surging: boolean;
  surgeTimer: number;
}

function makeRainCol(x: number, ch: number, trailLen: number, rng: () => number): RainColumn {
  const fs = 12 + Math.floor(rng() * 4);
  const trail = Math.floor(trailLen * (0.5 + rng()));
  const chars: string[] = [];
  for (let i = 0; i < trail + 5; i++) chars.push(randChar(rng));
  return {
    x,
    fs,
    spacing: fs * 1.45,
    speed: 0.4 + rng() * 2.4,
    headY: -ch + rng() * ch * 1.4,
    trail,
    alpha: 0.35 + rng() * 0.65,
    hue: pickHue(rng),
    chars,
    surging: false,
    surgeTimer: 0,
  };
}

interface TermBlock {
  x: number;
  y: number;
  w: number;
  h: number;
  hue: Hue;
  maxLines: number;
  lines: string[];
  curMsg: string;
  tgtMsg: string;
  ci: number;
  typeDelay: number;
  typeTimer: number;
  pauseDelay: number;
  pauseTimer: number;
  paused: boolean;
  blink: boolean;
  blinkT: number;
  label: string;
}

const TERM_ZONES = [
  [0.02, 0.35, 0.02, 0.30],
  [0.55, 0.96, 0.02, 0.28],
  [0.02, 0.38, 0.62, 0.95],
  [0.55, 0.96, 0.65, 0.95],
  [0.30, 0.70, 0.02, 0.20],
  [0.30, 0.70, 0.75, 0.96],
  [0.02, 0.25, 0.35, 0.60],
  [0.72, 0.96, 0.35, 0.60],
];

function makeTermBlock(
  idx: number, cw: number, ch: number, rng: () => number
): TermBlock {
  const w = 240 + Math.floor(rng() * 130);
  const h = 110 + Math.floor(rng() * 75);
  const z = TERM_ZONES[idx % TERM_ZONES.length];
  const x = Math.floor(z[0] * cw + rng() * Math.max(10, z[1] * cw - z[0] * cw - w));
  const y = Math.floor(z[2] * ch + rng() * Math.max(10, z[3] * ch - z[2] * ch - h));
  const r = rng();
  const hue: Hue = r < 0.5 ? 'green' : r < 0.8 ? 'cyan' : 'amber';
  return {
    x, y, w, h, hue,
    maxLines: Math.floor((h - 30) / 15),
    lines: [],
    curMsg: '',
    tgtMsg: pick(TERM_MSGS, rng),
    ci: 0,
    typeDelay: 2 + Math.floor(rng() * 3),
    typeTimer: 0,
    pauseDelay: 40 + Math.floor(rng() * 60),
    pauseTimer: 0,
    paused: false,
    blink: true,
    blinkT: 0,
    label: pick(
      ['ATLAS TERMINAL','SYS MONITOR','EXEC ENGINE','RISK GATE','DATA FEED','OPT WORKER'],
      rng
    ),
  };
}

interface GlitchCluster {
  x: number;
  y: number;
  txt: string;
  life: number;
  age: number;
  scrambleAt: number;
  fs: number;
  hue: Hue;
  isPhrase: boolean;
}

function makeGlitch(cw: number, ch: number, rng: () => number): GlitchCluster {
  const isPhrase = rng() < 0.07;
  let txt: string;
  let life: number;
  if (isPhrase) {
    txt = pick(STATION_PHRASES, rng);
    life = 140 + Math.floor(rng() * 160);
  } else {
    const n = 5 + Math.floor(rng() * 3);
    txt = '';
    for (let i = 0; i < n; i++) txt += String(Math.floor(rng() * 10));
    life = 50 + Math.floor(rng() * 130);
  }
  return {
    x: rng() * cw,
    y: rng() * ch,
    txt,
    life,
    age: 0,
    scrambleAt: life * (isPhrase ? 0.78 : 0.62),
    fs: isPhrase ? 14 + Math.floor(rng() * 4) : 12 + Math.floor(rng() * 5),
    hue: rng() < 0.65 ? 'amber' : 'cyan',
    isPhrase,
  };
}

// ─── Grid Buffer Builder ────────────────────────────────────────────────────
function buildGridBuffer(cw: number, ch: number, rng: () => number): HTMLCanvasElement {
  const bh = ch * 3;
  const c = document.createElement('canvas');
  c.width = cw;
  c.height = bh;
  const ctx = c.getContext('2d')!;
  ctx.font = '10px "JetBrains Mono", "Fira Code", "Courier New", monospace';
  ctx.fillStyle = rgba(COL_GREEN, 1);
  ctx.textBaseline = 'top';
  let y = 4;
  while (y < bh) {
    let x = 8;
    while (x < cw - 40) {
      const item = pick(GRID_ITEMS, rng);
      ctx.fillText(item, x, y);
      x += item.length * 6.5 + 18 + rng() * 17;
    }
    y += 13 + Math.floor(rng() * 4);
  }
  return c;
}

// ─── Main Component ─────────────────────────────────────────────────────────
const PhosphorErosion: React.FC<PhosphorErosionProps> = ({
  className,
  seed = 42,
  rainColumns = 55,
  rainSpeed = 1.0,
  trailLength = 22,
  terminalBlocks = 4,
  glitchClusters = 18,
  gridOpacity = 0.055,
  scanlineOpacity = 0.06,
  crtFlicker = 0.015,
  opacity = 0.85,
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rafRef = useRef<number>(0);
  const stateRef = useRef<{
    cols: RainColumn[];
    terms: TermBlock[];
    glitchs: GlitchCluster[];
    gridBuf: HTMLCanvasElement | null;
    gridScroll: number;
    rng: () => number;
  }>({
    cols: [],
    terms: [],
    glitchs: [],
    gridBuf: null,
    gridScroll: 0,
    rng: mulberry32(seed),
  });

  // ── Build the system ──────────────────────────────────────────────────
  const buildSystem = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const cw = canvas.width;
    const ch = canvas.height;
    const rng = mulberry32(seed);
    const st = stateRef.current;
    st.rng = rng;
    st.gridScroll = 0;

    // Grid buffer
    st.gridBuf = buildGridBuffer(cw, ch, rng);

    // Rain
    st.cols = [];
    const colW = cw / rainColumns;
    for (let i = 0; i < rainColumns; i++) {
      st.cols.push(makeRainCol(i * colW + colW * 0.5, ch, trailLength, rng));
    }

    // Terminals
    st.terms = [];
    for (let i = 0; i < terminalBlocks; i++) {
      st.terms.push(makeTermBlock(i, cw, ch, rng));
    }

    // Glitches
    st.glitchs = [];
    for (let i = 0; i < glitchClusters; i++) {
      st.glitchs.push(makeGlitch(cw, ch, rng));
    }
  }, [seed, rainColumns, trailLength, terminalBlocks, glitchClusters]);

  // ── Render one frame ──────────────────────────────────────────────────
  const renderFrame = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d')!;
    const cw = canvas.width;
    const ch = canvas.height;
    const st = stateRef.current;
    const rng = st.rng;
    const font = '"JetBrains Mono", "Fira Code", "Courier New", monospace';

    // Clear
    ctx.fillStyle = '#0a0a0a';
    ctx.fillRect(0, 0, cw, ch);

    // ── Layer 0: Background Data Grid ──
    if (st.gridBuf) {
      ctx.save();
      ctx.globalAlpha = gridOpacity;
      st.gridScroll = (st.gridScroll + 0.18) % st.gridBuf.height;
      ctx.drawImage(st.gridBuf, 0, -st.gridScroll);
      if (st.gridScroll > 0) ctx.drawImage(st.gridBuf, 0, st.gridBuf.height - st.gridScroll);
      ctx.globalAlpha = 1;
      ctx.restore();
    }

    // ── Layer 1: Rain Trails (no shadow) ──
    ctx.shadowBlur = 0;
    ctx.textBaseline = 'top';
    for (const col of st.cols) {
      // Update
      if (!col.surging && rng() < 0.0008) {
        col.surging = true;
        col.surgeTimer = 15 + Math.floor(rng() * 25);
      }
      if (col.surging) {
        col.surgeTimer--;
        col.speed = 5.0;
        col.alpha = 1.0;
        if (col.surgeTimer <= 0) {
          col.surging = false;
          col.speed = 0.4 + rng() * 2.4;
          col.alpha = 0.35 + rng() * 0.65;
        }
      }
      col.headY += col.speed * rainSpeed;
      if (rng() < 0.06) {
        col.chars[Math.floor(rng() * col.chars.length)] = randChar(rng);
      }
      if (col.headY - col.trail * col.spacing > ch + 60) {
        col.headY = -350 + rng() * 310;
        col.speed = 0.4 + rng() * 2.4;
        col.trail = Math.floor(trailLength * (0.5 + rng()));
        col.alpha = 0.35 + rng() * 0.65;
      }

      // Draw trail
      ctx.font = `${col.fs}px ${font}`;
      const rgb = HUE_RGB[col.hue];
      for (let i = 2; i < col.trail; i++) {
        const y = col.headY - i * col.spacing;
        if (y < -30 || y > ch + 30) continue;
        const t = i / col.trail;
        const a = (1 - t) * 0.75 * col.alpha;
        if (a < 0.01) continue;
        ctx.fillStyle = rgba(rgb, a);
        ctx.fillText(col.chars[i % col.chars.length], col.x, y);
      }
    }

    // ── Layer 1b: Rain Heads (with glow) ──
    for (const col of st.cols) {
      ctx.font = `${col.fs}px ${font}`;
      const rgb = HUE_RGB[col.hue];
      for (let i = 0; i < Math.min(2, col.trail); i++) {
        const y = col.headY - i * col.spacing;
        if (y < -30 || y > ch + 30) continue;
        if (i === 0) {
          ctx.shadowColor = rgba(rgb, 0.7);
          ctx.shadowBlur = 12;
          ctx.fillStyle = rgba([255, 255, 255], 0.94 * col.alpha);
        } else {
          ctx.shadowColor = rgba(rgb, 0.4);
          ctx.shadowBlur = 6;
          ctx.fillStyle = rgba(rgb, 0.86 * col.alpha);
        }
        ctx.fillText(col.chars[i % col.chars.length], col.x, y);
      }
    }
    ctx.shadowBlur = 0;

    // ── Layer 2: Glitch Clusters ──
    for (const g of st.glitchs) {
      g.age++;
      if (!g.isPhrase && g.age > g.scrambleAt && rng() < 0.25) {
        const a = g.txt.split('');
        a[Math.floor(rng() * a.length)] = String(Math.floor(rng() * 10));
        g.txt = a.join('');
      }
      if (g.age > g.life) {
        Object.assign(g, makeGlitch(cw, ch, rng));
        continue;
      }
      const fadeIn = Math.min(1, g.age / 12);
      const fadeOut = Math.max(0, 1 - Math.max(0, g.age - g.life + 25) / 25);
      let a = Math.min(fadeIn, fadeOut);
      if (rng() < 0.12) a *= 0.2 + rng() * 0.5;
      if (a < 0.02) continue;

      const rgb = HUE_RGB[g.hue];
      ctx.font = `${g.fs}px ${font}`;
      ctx.shadowColor = rgba(rgb, a * 0.5);
      ctx.shadowBlur = 8;
      ctx.fillStyle = rgba(rgb, a * 0.82);
      ctx.fillText(g.txt, g.x, g.y);
    }
    ctx.shadowBlur = 0;

    // ── Layer 3: Terminal Blocks ──
    for (const t of st.terms) {
      // Update
      t.blinkT++;
      if (t.blinkT > 25) { t.blink = !t.blink; t.blinkT = 0; }
      if (t.paused) {
        t.pauseTimer++;
        if (t.pauseTimer > t.pauseDelay) {
          t.paused = false;
          t.pauseTimer = 0;
          t.tgtMsg = pick(TERM_MSGS, rng);
          t.curMsg = '';
          t.ci = 0;
        }
      } else {
        t.typeTimer++;
        if (t.typeTimer >= t.typeDelay && t.ci < t.tgtMsg.length) {
          t.curMsg += t.tgtMsg[t.ci];
          t.ci++;
          t.typeTimer = 0;
        }
        if (t.ci >= t.tgtMsg.length && t.curMsg.length > 0) {
          const h = String(Math.floor(rng() * 24)).padStart(2, '0');
          const m = String(Math.floor(rng() * 60)).padStart(2, '0');
          const s = String(Math.floor(rng() * 60)).padStart(2, '0');
          const ms = String(Math.floor(rng() * 1000)).padStart(3, '0');
          t.lines.push(`${h}:${m}:${s}.${ms} ${t.curMsg}`);
          while (t.lines.length > t.maxLines) t.lines.shift();
          t.curMsg = '';
          t.ci = 0;
          t.paused = true;
        }
      }

      // Draw
      const rgb = HUE_RGB[t.hue];
      ctx.fillStyle = 'rgba(0,0,0,0.67)';
      ctx.strokeStyle = rgba(rgb, 0.18);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(t.x, t.y, t.w, t.h, 3);
      ctx.fill();
      ctx.stroke();

      // Header bar
      ctx.fillStyle = rgba(rgb, 0.08);
      ctx.beginPath();
      ctx.roundRect(t.x + 1, t.y + 1, t.w - 2, 18, [3, 3, 0, 0]);
      ctx.fill();

      ctx.font = `9px ${font}`;
      ctx.shadowColor = rgba(rgb, 0.4);
      ctx.shadowBlur = 4;
      ctx.fillStyle = rgba(rgb, 0.63);
      ctx.fillText(t.label, t.x + 8, t.y + 5);
      ctx.shadowBlur = 0;

      // Lines
      ctx.font = `10px ${font}`;
      let ly = t.y + 26;
      for (const ln of t.lines) {
        ctx.fillStyle = rgba(rgb, 0.28);
        ctx.fillText(ln.substring(0, 12), t.x + 6, ly);
        ctx.fillStyle = rgba(rgb, 0.51);
        ctx.fillText(ln.substring(12), t.x + 6 + 12 * 6.2, ly);
        ly += 15;
      }

      // Typing line
      if (!t.paused && t.curMsg.length > 0) {
        ctx.fillStyle = rgba(rgb, 0.71);
        ctx.shadowColor = rgba(rgb, 0.3);
        ctx.shadowBlur = 3;
        ctx.fillText(t.curMsg + (t.blink ? '█' : ' '), t.x + 6, ly);
        ctx.shadowBlur = 0;
      } else {
        ctx.fillStyle = rgba(rgb, t.blink ? 0.71 : 0);
        ctx.fillText('█', t.x + 6, ly);
      }
    }

    // ── CRT Flicker ──
    if (rng() < crtFlicker) {
      ctx.fillStyle = `rgba(255,255,255,${(0.004 + rng() * 0.012).toFixed(4)})`;
      ctx.fillRect(0, 0, cw, ch);
    }
    if (rng() < 0.003) {
      const by = rng() * ch;
      ctx.fillStyle = `rgba(0,${Math.floor(rng() * 255)},${Math.floor(rng() * 65)},${(0.03 + rng() * 0.05).toFixed(3)})`;
      ctx.fillRect(0, by, cw, 2 + rng() * 4);
    }

    rafRef.current = requestAnimationFrame(renderFrame);
  }, [rainSpeed, trailLength, gridOpacity, crtFlicker]);

  // ── Lifecycle ─────────────────────────────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const resize = () => {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
      buildSystem();
    };

    resize();
    window.addEventListener('resize', resize);
    rafRef.current = requestAnimationFrame(renderFrame);

    return () => {
      window.removeEventListener('resize', resize);
      cancelAnimationFrame(rafRef.current);
    };
  }, [buildSystem, renderFrame]);

  // Rebuild when structural props change
  useEffect(() => {
    buildSystem();
  }, [buildSystem]);

  return (
    <div
      className={className}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 0,
        pointerEvents: 'none',
        overflow: 'hidden',
        background: '#0a0a0a',
        opacity,
      }}
    >
      <canvas
        ref={canvasRef}
        style={{ display: 'block', width: '100%', height: '100%' }}
      />
      {/* CRT Scanlines */}
      <div
        style={{
          position: 'absolute',
          inset: 0,
          background:
            'repeating-linear-gradient(to bottom, transparent 0px, transparent 1px, rgba(0,0,0,1) 1px, rgba(0,0,0,1) 2px)',
          opacity: scanlineOpacity,
          pointerEvents: 'none',
        }}
      />
      {/* CRT Vignette */}
      <div
        style={{
          position: 'absolute',
          inset: 0,
          background:
            'radial-gradient(ellipse at center, transparent 45%, rgba(0,0,0,0.75) 100%)',
          pointerEvents: 'none',
        }}
      />
    </div>
  );
};

export default PhosphorErosion;
