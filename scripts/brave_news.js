#!/usr/bin/env node
/**
 * Brave Search multi-endpoint news gatherer for Iran Monitor.
 *
 * Uses THREE Brave API endpoints for breadth:
 *   1. /news/search  — dedicated news index (freshest, news-only)
 *   2. /web/search   — web results (deeper coverage, analysis pieces)
 *   3. /web/search   — with different queries for market impact angles
 *
 * Usage:
 *   node scripts/brave_news.js                    # all queries, default
 *   node scripts/brave_news.js --section military # single section
 *   node scripts/brave_news.js --json             # JSON output
 */

const args = process.argv.slice(2);

const jsonMode = args.includes("--json");
const sectionFilter = args.includes("--section") ?
    args[args.indexOf("--section") + 1] : null;

const apiKey = process.env.BRAVE_API_KEY;
if (!apiKey) {
    console.error("Error: BRAVE_API_KEY required");
    process.exit(1);
}

// ─── Search Sections ────────────────────────────────────────
// Each section uses the optimal endpoint + query combination
const SECTIONS = [
    {
        id: "military",
        label: "MILITARY / STRIKES / CEASEFIRE",
        endpoint: "news",
        queries: [
            { q: "Iran war strikes ceasefire", count: 6, freshness: "pd" },
            { q: "US military Iran Gulf attack", count: 4, freshness: "pd" },
        ],
    },
    {
        id: "hormuz_oil",
        label: "STRAIT OF HORMUZ / OIL SUPPLY",
        endpoint: "news",
        queries: [
            { q: "Strait of Hormuz shipping tanker", count: 5, freshness: "pd" },
            { q: "VLCC rates tanker war risk insurance", count: 4, freshness: "pw" },
        ],
    },
    {
        id: "oil_markets",
        label: "OIL PRICES / MARKET IMPACT",
        endpoint: "web",
        queries: [
            { q: "crude oil price Iran conflict supply disruption", count: 5, freshness: "pd" },
            { q: "Brent WTI oil futures Iran war premium", count: 4, freshness: "pw" },
        ],
    },
    {
        id: "gold_macro",
        label: "GOLD / SAFE HAVEN / FED POLICY",
        endpoint: "news",
        queries: [
            { q: "gold price safe haven geopolitical risk", count: 4, freshness: "pd" },
            { q: "Fed interest rate decision inflation outlook", count: 4, freshness: "pw" },
        ],
    },
    {
        id: "cyber",
        label: "CYBER / INFRASTRUCTURE THREATS",
        endpoint: "news",
        queries: [
            { q: "Iran cyber attack critical infrastructure CISA", count: 4, freshness: "pd" },
        ],
    },
    {
        id: "defence",
        label: "DEFENCE / BUDGET / DIPLOMACY",
        endpoint: "web",
        queries: [
            { q: "US defence budget supplemental Iran military spending", count: 4, freshness: "pw" },
            { q: "Iran diplomacy negotiations ceasefire Oman Qatar mediation", count: 3, freshness: "pd" },
        ],
    },
    {
        id: "lng_energy",
        label: "LNG / ENERGY SUPPLY CHAIN",
        endpoint: "news",
        queries: [
            { q: "LNG natural gas price Qatar Iran Middle East supply", count: 4, freshness: "pw" },
        ],
    },
    {
        id: "sentiment",
        label: "MARKET SENTIMENT / RISK-OFF",
        endpoint: "web",
        queries: [
            { q: "stock market Iran war risk sentiment VIX fear", count: 4, freshness: "pd" },
            { q: "defence stocks cybersecurity stocks Iran conflict", count: 3, freshness: "pw" },
        ],
    },
];

// ─── API Calls ──────────────────────────────────────────────

async function braveSearch(endpoint, query, count, freshness, country = "US") {
    const base = endpoint === "news"
        ? "https://api.search.brave.com/res/v1/news/search"
        : "https://api.search.brave.com/res/v1/web/search";

    const params = new URLSearchParams({
        q: query,
        count: Math.min(count, 20).toString(),
        country: country,
    });
    if (freshness) params.append("freshness", freshness);

    const resp = await fetch(`${base}?${params}`, {
        headers: {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": apiKey,
        },
    });

    if (!resp.ok) {
        const text = await resp.text();
        return { error: `HTTP ${resp.status}: ${text.slice(0, 200)}`, results: [] };
    }

    const data = await resp.json();

    // Normalise results from both endpoints
    const results = [];

    // News endpoint → data.results
    if (endpoint === "news" && data.results) {
        for (const r of data.results) {
            results.push({
                title: r.title || "",
                url: r.url || "",
                description: r.description || "",
                age: r.age || "",
                source: r.meta_url?.netloc || "",
                type: "news",
            });
        }
    }

    // Web endpoint → data.web.results + data.news.results (if present)
    if (endpoint === "web") {
        // Web results
        if (data.web?.results) {
            for (const r of data.web.results) {
                results.push({
                    title: r.title || "",
                    url: r.url || "",
                    description: r.description || "",
                    age: r.age || "",
                    source: r.meta_url?.netloc || "",
                    type: "web",
                });
            }
        }
        // News infobox that web search sometimes includes
        if (data.news?.results) {
            for (const r of data.news.results) {
                results.push({
                    title: r.title || "",
                    url: r.url || "",
                    description: r.description || "",
                    age: r.age || "",
                    source: r.meta_url?.netloc || "",
                    type: "news_infobox",
                });
            }
        }
    }

    return { query, endpoint, results };
}

// ─── Deduplication ──────────────────────────────────────────

function dedup(results) {
    const seen = new Set();
    return results.filter(r => {
        // Dedup by URL (strip trailing slash + query params)
        const key = r.url.replace(/\?.*$/, "").replace(/\/$/, "").toLowerCase();
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
    });
}

// ─── Main ───────────────────────────────────────────────────

async function main() {
    const sections = sectionFilter
        ? SECTIONS.filter(s => s.id === sectionFilter)
        : SECTIONS;

    if (sections.length === 0) {
        console.error(`Unknown section: ${sectionFilter}`);
        console.error(`Available: ${SECTIONS.map(s => s.id).join(", ")}`);
        process.exit(1);
    }

    const allResults = {};
    const errors = [];

    // Run all queries — respect rate limits (1 req/sec for free tier)
    for (const section of sections) {
        const sectionResults = [];

        for (const q of section.queries) {
            try {
                const data = await braveSearch(section.endpoint, q.q, q.count, q.freshness);
                if (data.error) {
                    errors.push({ section: section.id, query: q.q, error: data.error });
                }
                sectionResults.push(...data.results);
            } catch (e) {
                errors.push({ section: section.id, query: q.q, error: e.message });
            }

            // Rate limit: 1 req/sec (free tier)
            await new Promise(r => setTimeout(r, 1100));
        }

        allResults[section.id] = {
            label: section.label,
            endpoint: section.endpoint,
            results: dedup(sectionResults),
        };
    }

    // ─── Output ─────────────────────────────────────────────

    if (jsonMode) {
        console.log(JSON.stringify({ sections: allResults, errors, timestamp: new Date().toISOString() }, null, 2));
        return;
    }

    // Human-readable output
    console.log("=== BRAVE SEARCH — Iran Conflict Intelligence (Multi-Endpoint) ===");
    console.log(`Search time: ${new Date().toISOString()}`);
    console.log(`Sections: ${Object.keys(allResults).length} | Endpoints: news + web`);
    console.log("");

    for (const [id, section] of Object.entries(allResults)) {
        const dedupResults = section.results;
        console.log(`── ${section.label} ── [${section.endpoint} endpoint, ${dedupResults.length} results]`);

        if (dedupResults.length === 0) {
            console.log("  (no results)");
        }

        for (let i = 0; i < dedupResults.length; i++) {
            const r = dedupResults[i];
            console.log(`--- Result ${i + 1} ---`);
            console.log(`Title: ${r.title}`);
            console.log(`Link: ${r.url}`);
            if (r.age) console.log(`Age: ${r.age}`);
            if (r.source) console.log(`Source: ${r.source}`);
            console.log(`Snippet: ${r.description}`);
            console.log("");
        }

        console.log("");
    }

    if (errors.length > 0) {
        console.log("── ERRORS ──");
        for (const e of errors) {
            console.log(`  ${e.section} [${e.query}]: ${e.error}`);
        }
    }

    // Stats
    const totalResults = Object.values(allResults).reduce((sum, s) => sum + s.results.length, 0);
    const newsCount = Object.values(allResults).reduce((sum, s) =>
        sum + s.results.filter(r => r.type === "news" || r.type === "news_infobox").length, 0);
    const webCount = totalResults - newsCount;
    console.log(`\n── Stats: ${totalResults} total results (${newsCount} news, ${webCount} web), ${errors.length} errors ──`);
}

main().catch(e => {
    console.error(`Fatal: ${e.message}`);
    process.exit(1);
});
