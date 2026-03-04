#!/usr/bin/env node
/**
 * Brave Search multi-endpoint news gatherer for Iran Monitor.
 *
 * Designed for 4-hourly cron: maximises recency and source diversity.
 *
 * Endpoints used (3 of 5 available on free plan):
 *   /news/search   — dedicated news index, freshest breaking stories
 *   /web/search    — analysis pieces, market data, deeper coverage
 *   /videos/search — video briefings from major networks
 *
 * NOT available on free plan (requires "Data for AI" $3/1K):
 *   /summarizer    — AI-generated summaries (LLM context endpoint)
 *   extra_snippets — extended text excerpts from web results
 *
 * Budget: 2000 calls/month ÷ 180 runs = ~11 calls/run
 * Strategy: 6 news (count=20) + 3 web (count=10) + 2 video (count=5)
 *          = up to 160 raw results → ~80-100 after dedup
 *
 * Output splits into:
 *   🔴 LAST 4 HOURS  — developments since previous cron run
 *   🟡 OLDER (4-24H)  — context from earlier today
 *
 * Usage:
 *   node scripts/brave_news.js                    # full run
 *   node scripts/brave_news.js --json             # JSON output
 *   node scripts/brave_news.js --section breaking # single section
 *   node scripts/brave_news.js --hours 6          # custom recency window
 */

const args = process.argv.slice(2);
const jsonMode = args.includes("--json");
const sectionFilter = args.includes("--section")
    ? args[args.indexOf("--section") + 1]
    : null;
const recentHours = args.includes("--hours")
    ? parseFloat(args[args.indexOf("--hours") + 1])
    : 4;

const apiKey = process.env.BRAVE_API_KEY;
if (!apiKey) {
    console.error("Error: BRAVE_API_KEY required");
    process.exit(1);
}

// ─── Query Plan ─────────────────────────────────────────────
// 11 API calls total. News queries use count=20 (max) for breadth.
// Each query targets different keywords to maximise source diversity.
const QUERIES = [
    // ── 6 NEWS queries (count=20 each) = up to 120 news results ──
    {
        id: "news_military",
        section: "military",
        label: "MILITARY / STRIKES",
        endpoint: "news",
        q: "Iran war strikes military attack latest",
        count: 20,
        freshness: "pd",
    },
    {
        id: "news_diplomacy",
        section: "diplomacy",
        label: "CEASEFIRE / DIPLOMACY / NEGOTIATIONS",
        endpoint: "news",
        q: "Iran ceasefire diplomacy negotiations Oman mediation",
        count: 20,
        freshness: "pd",
    },
    {
        id: "news_hormuz",
        section: "hormuz",
        label: "HORMUZ / SHIPPING / TANKER",
        endpoint: "news",
        q: "Strait Hormuz shipping tanker oil blockade VLCC",
        count: 20,
        freshness: "pd",
    },
    {
        id: "news_oil",
        section: "oil",
        label: "OIL PRICES / ENERGY",
        endpoint: "news",
        q: "oil price crude Brent WTI Iran supply disruption",
        count: 20,
        freshness: "pd",
    },
    {
        id: "news_gold_macro",
        section: "gold_macro",
        label: "GOLD / RATES / SAFE HAVEN",
        endpoint: "news",
        q: "gold price safe haven Fed rates inflation Iran war",
        count: 20,
        freshness: "pd",
    },
    {
        id: "news_cyber_defence",
        section: "cyber_defence",
        label: "CYBER / DEFENCE / BUDGET",
        endpoint: "news",
        q: "Iran cyber attack defence stocks budget CISA CrowdStrike",
        count: 20,
        freshness: "pd",
    },

    // ── 3 WEB queries (count=10) = up to 30 web results ──
    // Web search for analysis, data, and pieces news index misses
    {
        id: "web_tanker_rates",
        section: "hormuz",
        label: "VLCC RATES / WAR RISK DATA",
        endpoint: "web",
        q: "VLCC spot rate tanker war risk insurance premium Hormuz 2026",
        count: 10,
        freshness: "pw",
    },
    {
        id: "web_oil_analysis",
        section: "oil",
        label: "OIL MARKET ANALYSIS",
        endpoint: "web",
        q: "crude oil futures contango backwardation Iran war premium OPEC supply",
        count: 10,
        freshness: "pw",
    },
    {
        id: "web_market_impact",
        section: "markets",
        label: "MARKET IMPACT / PORTFOLIO",
        endpoint: "web",
        q: "stock market Iran war defence cybersecurity gold oil stocks portfolio",
        count: 10,
        freshness: "pw",
    },

    // ── 2 VIDEO queries (count=5) = up to 10 video results ──
    {
        id: "video_breaking",
        section: "video",
        label: "VIDEO — BREAKING",
        endpoint: "videos",
        q: "Iran war Hormuz oil latest news",
        count: 5,
        freshness: "pd",
    },
    {
        id: "video_analysis",
        section: "video",
        label: "VIDEO — ANALYSIS",
        endpoint: "videos",
        q: "Iran war oil tanker gold defence stocks analysis",
        count: 5,
        freshness: "pd",
    },
];

// ─── API Calls ──────────────────────────────────────────────

const ENDPOINTS = {
    news: "https://api.search.brave.com/res/v1/news/search",
    web: "https://api.search.brave.com/res/v1/web/search",
    videos: "https://api.search.brave.com/res/v1/videos/search",
};

async function braveSearch(query) {
    const base = ENDPOINTS[query.endpoint];
    if (!base) return { error: `Unknown endpoint: ${query.endpoint}`, results: [] };

    const params = new URLSearchParams({
        q: query.q,
        count: Math.min(query.count, 20).toString(),
        country: "US",
    });
    if (query.freshness) params.append("freshness", query.freshness);

    try {
        const resp = await fetch(`${base}?${params}`, {
            headers: {
                Accept: "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": apiKey,
            },
            signal: AbortSignal.timeout(15000),
        });

        if (!resp.ok) {
            const text = await resp.text();
            return { error: `HTTP ${resp.status}: ${text.slice(0, 200)}`, results: [] };
        }

        return normalise(query.endpoint, await resp.json());
    } catch (e) {
        return { error: e.message, results: [] };
    }
}

function normalise(endpoint, data) {
    const results = [];

    if (endpoint === "news") {
        for (const r of data.results || []) {
            results.push({
                title: r.title || "",
                url: r.url || "",
                description: r.description || "",
                age: r.age || "",
                page_age: r.page_age || "",
                source: r.meta_url?.netloc || "",
                type: "news",
            });
        }
    }

    if (endpoint === "web") {
        for (const r of data.web?.results || []) {
            results.push({
                title: r.title || "",
                url: r.url || "",
                description: r.description || "",
                age: r.age || "",
                page_age: r.page_age || "",
                source: r.meta_url?.netloc || "",
                type: "web",
            });
        }
        // Web search sometimes returns a news infobox
        for (const r of data.news?.results || []) {
            results.push({
                title: r.title || "",
                url: r.url || "",
                description: r.description || "",
                age: r.age || "",
                page_age: r.page_age || "",
                source: r.meta_url?.netloc || "",
                type: "news_via_web",
            });
        }
    }

    if (endpoint === "videos") {
        for (const r of data.results || []) {
            results.push({
                title: r.title || "",
                url: r.url || "",
                description: r.description || "",
                age: r.age || "",
                page_age: r.page_age || "",
                source: r.meta_url?.netloc || "",
                duration: r.video?.duration || "",
                creator: r.video?.creator || r.video?.author?.name || "",
                type: "video",
            });
        }
    }

    return { results };
}

// ─── Dedup + Sort ───────────────────────────────────────────

function dedup(results) {
    const seen = new Set();
    return results.filter((r) => {
        const key = r.url
            .replace(/\?.*$/, "")
            .replace(/\/$/, "")
            .replace(/^https?:\/\/(www\.)?/, "")
            .toLowerCase();
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
    });
}

function parseAge(age) {
    if (!age) return null;
    const m = age.match(/(\d+)\s*(minute|hour|day|week|month)/i);
    if (!m) return null;
    const n = parseInt(m[1]);
    const unit = m[2].toLowerCase();
    const mins =
        unit.startsWith("minute") ? n :
        unit.startsWith("hour") ? n * 60 :
        unit.startsWith("day") ? n * 1440 :
        unit.startsWith("week") ? n * 10080 :
        unit.startsWith("month") ? n * 43200 : 999999;
    return mins;
}

function ageMinutes(r) {
    // Try page_age first (exact timestamp), fall back to age string
    if (r.page_age) {
        try {
            const d = new Date(r.page_age);
            if (!isNaN(d.getTime())) {
                return (Date.now() - d.getTime()) / 60000;
            }
        } catch {}
    }
    return parseAge(r.age) ?? 999999;
}

function sortByRecency(results) {
    return [...results].sort((a, b) => ageMinutes(a) - ageMinutes(b));
}

// ─── Recency Bucketing ──────────────────────────────────────

function bucketResults(results, recentMinutes) {
    const recent = [];  // within the cron window
    const older = [];   // older context

    for (const r of results) {
        const mins = ageMinutes(r);
        r._age_minutes = Math.round(mins);
        if (mins <= recentMinutes) {
            recent.push(r);
        } else {
            older.push(r);
        }
    }

    return { recent: sortByRecency(recent), older: sortByRecency(older) };
}

function formatAge(mins) {
    if (mins < 60) return `${Math.round(mins)}m ago`;
    if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
    return `${Math.round(mins / 1440)}d ago`;
}

// ─── Main ───────────────────────────────────────────────────

async function main() {
    const queries = sectionFilter
        ? QUERIES.filter((q) => q.section === sectionFilter)
        : QUERIES;

    if (queries.length === 0) {
        console.error(`Unknown section: ${sectionFilter}`);
        console.error(`Available: ${[...new Set(QUERIES.map((q) => q.section))].join(", ")}`);
        process.exit(1);
    }

    const allRaw = [];
    const errors = [];
    let apiCalls = 0;

    for (const q of queries) {
        apiCalls++;
        try {
            const data = await braveSearch(q);
            if (data.error) {
                errors.push({ id: q.id, query: q.q, error: data.error });
            }
            // Tag each result with its section
            for (const r of data.results) {
                r._section = q.section;
                r._label = q.label;
            }
            allRaw.push(...data.results);
        } catch (e) {
            errors.push({ id: q.id, query: q.q, error: e.message });
        }

        // Rate limit: 1 req/sec (free tier)
        await new Promise((r) => setTimeout(r, 1100));
    }

    // Global dedup, then bucket by recency
    const deduped = dedup(allRaw);
    const recentMinutes = recentHours * 60;
    const { recent, older } = bucketResults(deduped, recentMinutes);

    // Group by section for display
    function groupBySection(results) {
        const groups = {};
        for (const r of results) {
            const sec = r._section || "other";
            if (!groups[sec]) groups[sec] = [];
            groups[sec].push(r);
        }
        return groups;
    }

    // ─── Output ─────────────────────────────────────────────

    if (jsonMode) {
        console.log(JSON.stringify({
            timestamp: new Date().toISOString(),
            recentWindowHours: recentHours,
            apiCalls,
            totalDeduped: deduped.length,
            recentCount: recent.length,
            olderCount: older.length,
            recent,
            older,
            errors,
        }, null, 2));
        return;
    }

    // Human-readable output
    const now = new Date().toISOString();
    console.log("=== BRAVE SEARCH — Iran Conflict Intelligence ===");
    console.log(`Timestamp: ${now}`);
    console.log(`Window: last ${recentHours}h | Endpoints: news + web + video | API calls: ${apiCalls}`);
    console.log(`Results: ${deduped.length} total (${recent.length} recent, ${older.length} older)`);
    console.log("");

    // ── RECENT (last 4h) — the critical section ──
    console.log(`${"═".repeat(70)}`);
    console.log(`🔴  NEW SINCE LAST UPDATE (last ${recentHours}h) — ${recent.length} results`);
    console.log(`${"═".repeat(70)}`);

    if (recent.length === 0) {
        console.log("  (no new results in this window)\n");
    } else {
        const recentGroups = groupBySection(recent);
        for (const [sec, results] of Object.entries(recentGroups)) {
            const label = results[0]._label || sec.toUpperCase();
            console.log(`\n── ${label} (${results.length}) ──`);
            for (const r of results) {
                console.log(`  [${formatAge(r._age_minutes)}] [${r.source}] ${r.title}`);
                console.log(`    ${r.description.slice(0, 200)}`);
                if (r.type === "video" && r.duration) {
                    console.log(`    🎬 ${r.duration} by ${r.creator || r.source}`);
                }
                console.log(`    ${r.url}`);
                console.log("");
            }
        }
    }

    // ── OLDER (4-24h) — context ──
    console.log(`\n${"─".repeat(70)}`);
    console.log(`🟡  OLDER CONTEXT (${recentHours}h-24h) — ${older.length} results`);
    console.log(`${"─".repeat(70)}`);

    if (older.length === 0) {
        console.log("  (no older results)\n");
    } else {
        const olderGroups = groupBySection(older);
        for (const [sec, results] of Object.entries(olderGroups)) {
            const label = results[0]._label || sec.toUpperCase();
            console.log(`\n── ${label} (${results.length}) ──`);
            for (const r of results) {
                console.log(`  [${formatAge(r._age_minutes)}] [${r.source}] ${r.title}`);
                console.log(`    ${r.description.slice(0, 150)}`);
                console.log(`    ${r.url}`);
                console.log("");
            }
        }
    }

    // Errors
    if (errors.length > 0) {
        console.log("\n── ERRORS ──");
        for (const e of errors) {
            console.log(`  [${e.id}] "${e.query}": ${e.error}`);
        }
    }

    // Source diversity stats
    const sources = {};
    for (const r of deduped) {
        const s = r.source || "unknown";
        sources[s] = (sources[s] || 0) + 1;
    }
    const topSources = Object.entries(sources)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 15)
        .map(([s, n]) => `${s}(${n})`)
        .join(", ");

    const byType = {};
    for (const r of deduped) byType[r.type] = (byType[r.type] || 0) + 1;
    const typeStr = Object.entries(byType).map(([k, v]) => `${v} ${k}`).join(", ");

    console.log(`\n── Stats ──`);
    console.log(`  Results: ${deduped.length} (${typeStr})`);
    console.log(`  Recent (< ${recentHours}h): ${recent.length} | Older: ${older.length}`);
    console.log(`  Sources: ${Object.keys(sources).length} unique`);
    console.log(`  Top: ${topSources}`);
    console.log(`  API calls: ${apiCalls} | Errors: ${errors.length}`);
}

main().catch((e) => {
    console.error(`Fatal: ${e.message}`);
    process.exit(1);
});
