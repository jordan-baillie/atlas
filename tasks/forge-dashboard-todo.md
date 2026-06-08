# Forge Monitor Tab — build all 4 variants

## Backend
- [ ] services/api/forge.py — GET /api/forge/state (aggregate Hephaestus state)
- [ ] wire router into services/chat_server.py

## Frontend data layer
- [ ] src/api/forge-types.ts
- [ ] src/api/forge-queries.ts (useForgeState)
- [ ] src/api/keys.ts — add forge key
- [ ] index.css — forge keyframes (ember, pulse, travel, stamp)

## Components
- [ ] src/components/forge/ForgeTab.tsx — A/B/C/D switcher + shared data + countdown hook
- [ ] VariantForge.tsx (A — pipeline)
- [ ] VariantMissionControl.tsx (B — telemetry)
- [ ] VariantGauntlet.tsx (C — funnel)
- [ ] VariantNotebook.tsx (D — feed)

## Wiring
- [ ] App.tsx — replace research with forge
- [ ] TabBar.tsx — Forge tab
- [ ] lib/preloaders.ts — preloadForgeTab
- [ ] remove ResearchTab references

## Verify
- [ ] npm run build passes
- [ ] endpoint returns valid JSON
- [ ] restart dashboard, confirm tab renders all 4
