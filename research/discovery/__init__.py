"""Atlas Research Discovery Package.

Exports:
    discover_daily        — run today's paper → strategy pipeline
    discover_full         — run full sweep across all sources
    DailyReport           — result dataclass
    STRATEGY_UNIVERSE     — master dict of all strategies
    queue_discovery_batch — generate new experiments for the queue
    AVAILABLE_FILTERS     — filter names for combinatorial exploration
    get_unbuilt_strategies
    get_untested_existing
    update_strategy_status
    generate_filter_combinations
    generate_param_cross_pollination
    generate_ablation_experiments
    generate_robustness_experiments
    get_next_experiments
    log_combination
    init_vault_notes
"""

from research.discovery.discovery import discover_daily, discover_full, DailyReport

from research.discovery.strategy_universe import (
    STRATEGY_UNIVERSE,
    queue_discovery_batch,
    AVAILABLE_FILTERS,
    STRATEGY_UNIVERSE_PATH,
    COMBINATION_LOG_PATH,
    get_unbuilt_strategies,
    get_untested_existing,
    update_strategy_status,
    get_tested_combinations,
    generate_filter_combinations,
    generate_param_cross_pollination,
    generate_ablation_experiments,
    generate_robustness_experiments,
    get_next_experiments,
    log_combination,
    init_vault_notes,
)

__all__ = [
    # discovery orchestrator
    "discover_daily", "discover_full", "DailyReport",
    # strategy universe
    "STRATEGY_UNIVERSE", "STRATEGY_UNIVERSE_PATH",
    "AVAILABLE_FILTERS", "COMBINATION_LOG_PATH",
    "queue_discovery_batch",
    "get_unbuilt_strategies", "get_untested_existing",
    "update_strategy_status", "get_tested_combinations",
    "generate_filter_combinations", "generate_param_cross_pollination",
    "generate_ablation_experiments", "generate_robustness_experiments",
    "get_next_experiments", "log_combination", "init_vault_notes",
]
