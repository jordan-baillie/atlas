#!/usr/bin/env node
/**
 * Brave Search multi-endpoint news gatherer for Iran Monitor.
 *
 * Uses FOUR Brave API endpoints for maximum breadth & recency:
 *   1. /news/search   — dedicated news index (freshest breaking news)
 *   2. /web/search    — web results + extra_snippets (analysis, deeper coverage)
 *   3. /videos/search — video briefings (CBC, France24, Al Jazeera etc.)
 *   4. /images/search — (skipped — no value for geopolitical intel)
 *
 * Prioritises recency: news endpoint with freshness=pd first, then pw fallback.
 *
 * Usage:
 *   node scripts/brave_news.js                    # all sections
 *   node scripts/brave_news.js --section military # single section
 *   node scripts/brave_news.js --json             # JSON output
 */

const args = process.argv.slice(2);

const jsonMode = args.includes("--json");
const sectionFilter = args.includes("--section")
    ? args[args.indexOf("--section") + 1]
    : null;

const apiKey = process.env.BRAVE_API_KEY;
if (!apiKey) {
    console.error("Error: BRAVE_API_KEY required");
    process.exit(1);
}

// ─── Search Sections ────────────────────────────────────────
// Priority order: breaking news first, then analysis, then video context
const SECTIONS = [
    // ── BREAKING (news endpoint, past 24h) ──
    {
        id: "breaking",
        label: "BREAKING — LAST 24H",
        queries: [
            { endpoint: "news", q: "Iran war strikes latest", count: 8, freshness: "pd" },
            { endpoint: "news", q: "Iran ceasefire negotiations diplomacy", count: 4, freshness: "pd" },
        ],
    },
    {
        id: "hormuz_shipping",
        label: "STRAIT OF HORMUZ / SHIPPING / TANKER",
        queries: [
            { endpoint: "news", q: "Strait of Hormuz shipping closed blockade", count: 5, freshness: "pd" },
            { endpoint: "news", q: "VLCC tanker rates war risk insurance", count: 4, freshness: "pd" },
            // Fallback to past week if 24h is sparse
            { endpoint: "web", q: "VLCC spot rate tanker war risk premium Hormuz", count: 3, freshness: "pw", extra_snippets: true },
        ],
    },
    {
        id: "oil_markets",
        label: "OIL PRICES / SUPPLY DISRUPTION",
        queries: [
            { endpoint: "news", q: "crude oil price Iran supply disruption OPEC", count: 5, freshness: "pd" },
            { endpoint: "web", q: "Brent WTI oil futures Iran war premium contango backwardation", count: 4, freshness: "pw", extra_snippets: true },
        ],
    },
    {
        id: "gold_macro",
        label: "GOLD / SAFE HAVEN / RATES",
        queries: [
            { endpoint: "news", q: "gold price safe haven geopolitical risk", count: 4, freshness: "pd" },
            { endpoint: "news", q: "Federal Reserve rate decision inflation Iran", count: 3, freshness: "pd" },
            { endpoint: "web", q: "real yields TIPS gold central bank buying 2026", count: 3, freshness: "pw", extra_snippets: true },
        ],
    },
    {
        id: "cyber",
        label: "CYBER / INFRASTRUCTURE THREATS",
        queries: [
            { endpoint: "news", q: "Iran cyber attack infrastructure CISA warning", count: 4, freshness: "pd" },
            { endpoint: "news", q: "CrowdStrike Palo Alto cybersecurity Iran threat", count: 3, freshness: "pw" },
        ],
    },
    {
        id: "defence",
        label: "DEFENCE / BUDGET / DIPLOMACY",
        queries: [
            { endpoint: "news", q: "US defence budget supplemental military spending Iran", count: 4, freshness: "pd" },
            { endpoint: "news", q: "Oman Qatar mediation Iran diplomacy off-ramp", count: 3, freshness: "pd" },
        ],
    },
    {
        id: "lng_energy",
        label: "LNG / ENERGY SUPPLY CHAIN",
        queries: [
            { endpoint: "news", q: "LNG natural gas Qatar Iran Middle East supply disruption", count: 4, freshness: "pd" },
            { endpoint: "web", q: "JKM LNG spot price Qatar force majeure Iran", count: 3, freshness: "pw", extra_snippets: true },
        ],
    },
    {
        id: "sentiment",
        label: "MARKET SENTIMENT / RISK-OFF",
        queries: [
            { endpoint: "news", q: "stock market Iran war VIX fear risk-off", count: 4, freshness: "pd" },
            { endpoint: "web", q: "defence stocks cybersecurity stocks oil stocks Iran war portfolio", count: 3, freshness: "pw", extra_snippets: true },
        ],
    },
    // ── VIDEO INTEL (unique perspective, breaking video briefings) ──
    {
        id: "video_intel",
        label: "VIDEO BRIEFINGS",
        queries: [
            { endpoint: "videos", q: "Iran war Hormuz oil latest", count: 5, freshness: "pd" },
            { endpoint: "videos", q: "Iran ceasefire gold defence stocks", count: 3, freshness: "pd" },
        ],
    },
];

// ─── API Calls ──────────────────────────────────────────────

async function braveSearch(endpoint, query, count, freshness, extraSnippets = false) {
    const baseUrls = {
        news: "https://api.search.brave.com/res/v1/news/search",
        web: "https://api.search.brave.com/res/v1/web/search",
        videos: "https://api.search.brave.com/res/v1/videos/search",
    };

    const base = baseUrls[endpoint];
    if (!base) return { error: `Unknown endpoint: ${endpoint}`, results: [] };

    const params = new URLSearchParams({
        q: query,
        count: Math.min(count, 20).toString(),
        country: "US",
    });
    if (freshness) params.append("freshness", freshness);
    if (extraSnippets && endpoint === "web") params.append("extra_snippets", "1");

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

        const data = await resp.json();
        return normalise(endpoint, data);
    } catch (e) {
        return { error: e.message, results: [] };
    }
}

function normalise(endpoint, data) {
    const results = [];

    if (endpoint === "news" && data.results) {
        for (const r of data.results) {
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
        for (const r of (data.web?.results || [])) {
            const item = {
                title: r.title || "",
                url: r.url || "",
                description: r.description || "",
                age: r.age || "",
                page_age: r.page_age || "",
                source: r.meta_url?.netloc || "",
                type: "web",
            };
            // Include extra snippets for deeper context
            if (r.extra_snippets?.length) {
                item.extra = r.extra_snippets.slice(0, 2).join(" | ");
            }
            results.push(item);
        }
        // Web search sometimes returns a news infobox — grab those too
        for (const r of (data.news?.results || [])) {
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

    if (endpoint === "videos" && data.results) {
        for (const r of data.results) {
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

// ─── Deduplication ──────────────────────────────────────────

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

// ─── Sort by recency ────────────────────────────────────────

function ageToMinutes(age) {
    if (!age) return 999999;
    const m = age.match(/(\d+)\s*(minute|hour|day|week|month)/i);
    if (!m) return 999999;
    const n = parseInt(m[1]);
    const unit = m[2].toLowerCase();
    if (unit.startsWith("minute")) return n;
    if (unit.startsWith("hour")) return n * 60;
    if (unit.startsWith("day")) return n * 1440;
    if (unit.startsWith("week")) return n * 10080;
    if (unit.startsWith("month")) return n * 43200;
    return 999999;
}

function sortByRecency(results) {
    return [...results].sort((a, b) => ageToMinutes(a.age) - ageToMinutes(b.age));
}

// ─── Main ───────────────────────────────────────────────────

async function main() {
    const sections = sectionFilter
        ? SECTIONS.filter((s) => s.id === sectionFilter)
        : SECTIONS;

    if (sections.length === 0) {
        console.error(`Unknown section: ${sectionFilter}`);
        console.error(`Available: ${SECTIONS.map((s) => s.id).join(", ")}`);
        process.exit(1);
    }

    const allResults = {};
    const errors = [];
    let totalApiCalls = 0;

    for (const section of sections) {
        const sectionResults = [];

        for (const q of section.queries) {
            totalApiCalls++;
            try {
                const data = await braveSearch(
                    q.endpoint,
                    q.q,
                    q.count,
                    q.freshness,
                    q.extra_snippets || false
                );
                if (data.error) {
                    errors.push({ section: section.id, query: q.q, error: data.error });
                }
                sectionResults.push(...data.results);
            } catch (e) {
                errors.push({ section: section.id, query: q.q, error: e.message });
            }

            // Rate limit: 1 req/sec (free tier) — 1.1s to be safe
            await new Promise((r) => setTimeout(r, 1100));
        }

        // Dedup within section, sort by recency
        allResults[section.id] = {
            label: section.label,
            results: sortByRecency(dedup(sectionResults)),
        };
    }

    // ─── Output ─────────────────────────────────────────────

    if (jsonMode) {
        console.log(
            JSON.stringify(
                { sections: allResults, errors, totalApiCalls, timestamp: new Date().toISOString() },
                null,
                2
            )
        );
        return;
    }

    // Human-readable output — recency-first
    console.log("=== BRAVE SEARCH — Iran Conflict Intelligence ===");
    console.log(`Timestamp: ${new Date().toISOString()}`);
    console.log(`Endpoints: news + web + videos | API calls: ${totalApiCalls}`);
    console.log("");

    for (const [id, section] of Object.entries(allResults)) {
        const results = section.results;
        console.log(`── ${section.label} ── [${results.length} results, sorted by recency]`);

        if (results.length === 0) {
            console.log("  (no results)\n");
            continue;
        }

        for (let i = 0; i < results.length; i++) {
            const r = results[i];
            console.log(`--- Result ${i + 1} [${r.type}] ---`);
            console.log(`Title: ${r.title}`);
            console.log(`Link: ${r.url}`);
            if (r.age) console.log(`Age: ${r.age}`);
            if (r.source) console.log(`Source: ${r.source}`);
            if (r.duration) console.log(`Duration: ${r.duration}`);
            if (r.creator) console.log(`Creator: ${r.creator}`);
            console.log(`Snippet: ${r.description}`);
            if (r.extra) console.log(`Extra: ${r.extra}`);
            console.log("");
        }
        console.log("");
    }

    if (errors.length > 0) {
        console.log("── ERRORS ──");
        for (const e of errors) {
            console.log(`  [${e.section}] "${e.query}": ${e.error}`);
        }
        console.log("");
    }

    // Stats
    const totalResults = Object.values(allResults).reduce(
        (sum, s) => sum + s.results.length,
        0
    );
    const byType = {};
    for (const s of Object.values(allResults)) {
        for (const r of s.results) {
            byType[r.type] = (byType[r.type] || 0) + 1;
        }
    }
    const typeStr = Object.entries(byType)
        .map(([k, v]) => `${v} ${k}`)
        .join(", ");
    console.log(
        `── Stats: ${totalResults} results (${typeStr}) | ${totalApiCalls} API calls | ${errors.length} errors ──`
    );
}

main().catch((e) => {
    console.error(`Fatal: ${e.message}`);
    process.exit(1);
});
