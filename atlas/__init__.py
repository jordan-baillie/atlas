"""Atlas — execution platform for the Crucible forge->live trading pipeline.

Packages (dependency direction: kernel <- db <- brokers <- execution <- dashboard):
- atlas.kernel    -- shared kernel: paths, config, secrets, notify, market hours, logging
- atlas.db        -- SQLite access layer for data/atlas.db
- atlas.brokers   -- venue adapters (alpaca, ib, ib_web) + price plumbing
- atlas.execution -- the forge->live loop: registry, providers, target executor, kill switch
- atlas.analytics -- post-hoc analytics (strategy EV)
- atlas.dashboard -- FastAPI backend (:8899) serving dashboard-ui + chat
"""
