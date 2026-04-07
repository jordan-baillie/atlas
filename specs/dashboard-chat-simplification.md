# Atlas Dashboard: Chat Integration + Simplification Spec

## Status: DRAFT
## Date: 2026-04-07
## Author: Orchestrator (synthesized from codebase analysis)

---

## 1. Goals

1. **Add headless chat** to the dashboard — talk to the multi-agent system from the browser, with persistent state that survives page closes
2. **Simplify the dashboard** — reduce visual complexity, merge redundant sections, make chat the primary interaction method

---

## 2. Current State Analysis

### Dashboard (1402-line `index.html` + 851-line `atlas.css`)
- **5 tabs**: Overview, Positions, Performance, Orders, Regime/AI
- **Widgets**: Summary strip (6 stats), equity chart with SPY benchmark, position cards with sparklines, donut chart (strategy allocation), orders table, regime timeline, AI overlay card, performance metrics
- **Data flow**: Static JSON (`/api/dashboard-data`) → initial paint, then SSE (`/api/stream`) for live updates
- **Redundancy**: Summary strip appears on ALL tabs. Positions show on both Overview and Positions tab. Performance shows on both Overview and Performance tab. Regime info is in both the sidebar AND the Regime tab.

### Server (989-line `dashboard_server.py`)
- Python `http.server.HTTPServer` + `SimpleHTTPRequestHandler`
- HTTP Basic Auth
- API endpoints: approve/reject plans, SSE stream, prices, SQLite queries (portfolio, trades, performance, equity curve, regime history, overlay decisions, system health)
- **No WebSocket support** — would need a complete server rewrite or parallel server

### Pi CLI (`pi --mode json`)
- Outputs structured JSONL with real-time streaming events (`text_delta`, `thinking_start/end`, `tool_call`, etc.)
- Supports `--session <path>` for persistent sessions and `--continue` to resume
- Can load extensions (`-e`) for multi-team orchestration
- Non-interactive mode (`-p`) processes a single prompt and exits

### Available Python Packages
- `fastapi 0.135.1` + `uvicorn 0.41.0` — modern async framework with native WebSocket
- `websockets 15.0.1` — standalone WebSocket library
- `aiohttp 3.13.3` — async HTTP client/server

---

## 3. Architecture Design

### 3.1 Server Migration: `http.server` → FastAPI

Replace the synchronous `http.server` with FastAPI. This gives us:
- Native WebSocket support for chat
- Async request handling (no more thread-per-request)
- Same auth, same API routes, better foundation

**Migration approach**: Keep all existing API endpoint logic, just wrap in FastAPI routes. Existing HTML/CSS/JS served as static files.

### 3.2 Chat Backend: Pi Subprocess Manager

```
Browser ↔ WebSocket ↔ FastAPI ↔ PiSessionManager ↔ pi CLI subprocess
                                       ↕
                                  SQLite (chat_history)
```

**PiSessionManager** — a Python class that:
1. Maintains a long-running `pi` subprocess in JSON mode
2. Reads JSONL output line-by-line, parses streaming events
3. Forwards `text_delta`, `tool_call`, `delegation` events to connected WebSocket clients
4. Accepts user messages from WebSocket, writes them to pi's stdin
5. Persists conversation to SQLite (`chat_messages` table)
6. Survives browser disconnects — the Pi process keeps running
7. On browser reconnect, replays missed messages from SQLite

**Pi subprocess command:**
```bash
pi --mode json \
   --session /root/atlas/data/chat/session.jsonl \
   -e /root/.pi/extensions/multi-team/index.ts \
   --model claude-sonnet-4-6
```

Using Sonnet (not Opus) for cost efficiency since chat will be frequent. User can upgrade per-session if needed.

### 3.3 Chat State Persistence

**SQLite table** (in existing Atlas DB or separate `chat.db`):

```sql
CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,           -- 'user', 'assistant', 'system', 'tool'
    content TEXT NOT NULL,        -- markdown text
    metadata JSON,               -- tool calls, delegations, costs, model info
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE chat_sessions (
    id TEXT PRIMARY KEY,          -- UUID
    name TEXT,                    -- user-given name or auto-generated
    pi_session_path TEXT,         -- path to pi's session.jsonl for --continue
    status TEXT DEFAULT 'active', -- active, archived
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 3.4 WebSocket Protocol

**Client → Server messages:**
```json
{"type": "send", "content": "analyze the equity curve for the last 30 days"}
{"type": "history", "before_id": 123, "limit": 50}
{"type": "cancel"}
{"type": "new_session", "name": "optional name"}
{"type": "switch_session", "session_id": "uuid"}
```

**Server → Client messages:**
```json
{"type": "text_start", "message_id": 456}
{"type": "text_delta", "delta": "Based on my analysis..."}
{"type": "text_end", "message_id": 456, "full_text": "..."}
{"type": "thinking_start"}
{"type": "thinking_delta", "delta": "Let me look at..."}
{"type": "thinking_end"}
{"type": "tool_start", "tool": "Read", "args": {"path": "config/active_config.json"}}
{"type": "tool_end", "tool": "Read", "result_summary": "512 bytes read"}
{"type": "delegation_start", "target": "Engineering Lead"}
{"type": "delegation_end", "target": "Engineering Lead", "summary": "..."}
{"type": "error", "message": "Pi process crashed, restarting..."}
{"type": "history", "messages": [...]}
{"type": "status", "pi_running": true, "session_id": "uuid", "model": "claude-sonnet-4-6"}
```

### 3.5 Reconnection Flow

1. Browser opens WebSocket → sends `{"type": "history", "limit": 50}`
2. Server responds with last 50 messages from SQLite
3. If Pi subprocess is currently generating, server sends `{"type": "status", "generating": true}` and streams remaining deltas
4. If Pi subprocess died, server restarts it with `--continue` flag to resume the session

---

## 4. Dashboard Simplification

### 4.1 Current → Simplified Layout

**Remove tabs entirely.** Replace with a single-page layout:

```
┌─────────────────────────────────────────────────────┐
│  ▲ Atlas          BULL_RISK_ON  NYSE ●  OPEN 3h12m  │
├─────────────────────────────────────────────────────┤
│  $7,842  │  +$127 (+1.6%)  │  3/10 pos  │  12% mgn │
├──────────────────────────┬──────────────────────────┤
│                          │                          │
│     PORTFOLIO VIEW       │       AI CHAT            │
│                          │                          │
│  Equity chart            │  Chat messages stream    │
│  Position cards           │  with tool call badges,  │
│  (sorted by P&L)         │  delegation indicators,  │
│                          │  thinking dots           │
│  Recent orders           │                          │
│  (collapsed, last 5)     │  ┌──────────────────┐   │
│                          │  │ Message input...  │   │
│                          │  └──────────────────┘   │
├──────────────────────────┴──────────────────────────┤
│  90-day regime timeline (thin bar)                   │
└─────────────────────────────────────────────────────┘
```

### 4.2 What Changes

| Current | Simplified |
|---------|-----------|
| 5 separate tabs | Single page, two-panel layout |
| Summary strip (6 stats) | Compact strip (4 stats: equity, today P&L, positions, margin) |
| Equity chart (full width) | Equity chart (left panel, ~60% width) |
| Position cards (separate tab) | Position cards (below equity chart, always visible) |
| Performance metrics (separate tab) | Removed — accessible via chat ("show me performance") |
| Orders table (separate tab) | Collapsed below positions (last 5, expand on click) |
| Regime sidebar card | Regime indicator in header (already exists), remove sidebar |
| AI Overlay sidebar card | Removed — overlay info accessible via chat |
| Strategy donut chart | Removed — low value for small portfolio |
| Regime/AI tab | Removed — regime in header, AI is now the chat |
| Tab navigation | Removed entirely |

### 4.3 What Stays (Left Panel)
1. **Header**: Logo, regime indicator, market clock, theme toggle
2. **Summary strip**: Equity, Today P&L, Positions count, Margin (4 stats, more compact)
3. **Equity chart**: Full width of left panel, with SPY benchmark
4. **Position cards**: Always visible, sorted by P&L magnitude
5. **Recent orders**: Collapsed list (last 5), expandable
6. **Regime timeline**: Thin 90-day bar at bottom

### 4.4 Chat Panel (Right Panel, ~40% width)
1. **Message history**: Scrollable, markdown-rendered
2. **Streaming indicators**: Thinking dots (⋯), tool call badges, delegation badges
3. **Message input**: Text area at bottom with send button
4. **Session controls**: New session, session history dropdown
5. **Status bar**: Model name, Pi process status, session cost

---

## 5. File Changes

### New Files
| File | Purpose |
|------|---------|
| `services/chat_server.py` | FastAPI server replacing `dashboard_server.py` |
| `services/pi_session.py` | PiSessionManager — subprocess lifecycle, JSONL parsing, event forwarding |
| `services/chat_db.py` | SQLite chat persistence (messages + sessions) |
| `dashboard/data/chat.js` | Client-side WebSocket handler + chat UI logic |
| `dashboard/data/chat.css` | Chat panel styles |

### Modified Files
| File | Changes |
|------|---------|
| `dashboard/data/index.html` | Simplified layout, add chat panel, remove tabs, remove sidebar |
| `dashboard/data/atlas.css` | Two-panel grid, compact summary strip, remove tab/sidebar styles |

### Deprecated Files
| File | Reason |
|------|--------|
| `services/dashboard_server.py` | Replaced by `chat_server.py` (FastAPI) |

### Systemd Update
| File | Changes |
|------|---------|
| `/etc/systemd/system/atlas-dashboard.service` | Point to new `chat_server.py`, use uvicorn |

---

## 6. Implementation Sequence

### Phase 1: FastAPI Server Migration (no chat yet)
1. Create `services/chat_server.py` — FastAPI app with all existing API routes
2. Add static file serving for `dashboard/data/`
3. Migrate HTTP Basic Auth to FastAPI middleware
4. Port all existing endpoint handlers
5. Test: all existing functionality works identically
6. Update systemd service

### Phase 2: Chat Backend
1. Create `services/chat_db.py` — SQLite schema + CRUD for messages/sessions
2. Create `services/pi_session.py` — PiSessionManager class
3. Add WebSocket endpoint to `chat_server.py`
4. Implement: spawn Pi subprocess, parse JSONL stream, forward events
5. Implement: reconnection, message replay, process restart
6. Test: send message via WebSocket, receive streaming response

### Phase 3: Dashboard Simplification
1. Simplify `index.html` — remove tabs, remove sidebar, two-panel layout
2. Update `atlas.css` — grid layout, compact styles
3. Strip unused JS (tab switching, donut chart, regime sidebar rendering)
4. Keep: equity chart, position cards, orders (collapsed), regime timeline
5. Test: simplified dashboard renders correctly

### Phase 4: Chat Frontend
1. Create `dashboard/data/chat.js` — WebSocket client, message rendering
2. Create `dashboard/data/chat.css` — chat panel styles
3. Add chat panel to `index.html`
4. Implement: message input, streaming display, thinking/tool indicators
5. Implement: session management, reconnection, history scroll
6. Test: full end-to-end chat works

### Phase 5: Polish
1. Mobile responsive (chat becomes full-screen on mobile)
2. Keyboard shortcuts (Ctrl+Enter to send, Escape to cancel)
3. Markdown rendering in chat messages (code blocks, tables, etc.)
4. Copy-to-clipboard on chat messages
5. Cost display per message and per session

---

## 7. Key Technical Decisions

### Q: Why FastAPI over adding WebSocket to http.server?
Python's `http.server` has no WebSocket support. Adding it would require a parallel server or a complex monkey-patch. FastAPI has first-class WebSocket support and is already installed.

### Q: Why Pi subprocess over direct Anthropic API calls?
The Pi CLI handles the full multi-team extension system, session management, tool execution, domain enforcement, and cost tracking. Reimplementing all of that in Python would be massive. Spawning Pi as a subprocess and parsing its JSON output is 10x simpler.

### Q: Why Sonnet for chat, not Opus?
Chat will be used frequently for quick questions. Sonnet is ~10x cheaper than Opus. The Pi orchestrator uses Opus internally, but for dashboard chat, Sonnet with the multi-team extension gives a good balance of capability and cost.

### Q: Why SQLite for chat history vs Pi's session.jsonl?
Pi's JSONL format is designed for session resumption, not efficient querying. SQLite lets us query by time range, paginate, and serve history to the browser efficiently. We keep BOTH — Pi's session file for `--continue`, SQLite for the dashboard.

### Q: How do we handle plan approval from chat?
The existing `/api/approve` and `/api/reject` endpoints stay. Additionally, the chat agent (via Pi) has access to the Atlas tools and can approve/reject plans through the `atlas_risk_approve_plan` tool directly from conversation.

---

## 8. Cost Estimate

- **Sonnet per chat message**: ~$0.003-0.01 for simple queries, $0.02-0.05 for delegated work
- **Monthly estimate** (assuming 20 messages/day): ~$2-10/month
- **Opus escalation** (if user asks for it): ~10x more per message

---

## 9. Risk Mitigations

| Risk | Mitigation |
|------|-----------|
| Pi subprocess crashes | Auto-restart with `--continue` flag; error message to client |
| WebSocket disconnects | SQLite persistence; replay missed messages on reconnect |
| High latency (Pi + multi-team) | Show thinking indicators; stream text deltas in real-time |
| Cost overrun (Opus from chat) | Default to Sonnet; show per-message cost; daily budget cap |
| Server memory (long sessions) | Cap message history in memory; older messages served from SQLite |
| Auth bypass via WebSocket | Same HTTP Basic Auth check on WebSocket upgrade |
