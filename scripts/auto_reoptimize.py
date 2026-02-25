#!/usr/bin/env python3
"""Automated Reoptimization Pipeline for Atlas.

Orchestrates:
1. Run health_check.py to assess current performance
2. If degraded, trigger reoptimize_full_universe.py
3. Run validate_oos.py on new config
4. Compare new vs old; if new is better on BOTH full and OOS, update config/active/asx.json
5. Backup old config with timestamp

Usage: python3 scripts/auto_reoptimize.py
"""
import sys, json, subprocess, shutil, logging
from pathlib import Path
from datetime import datetime

PROJECT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT / 'scripts'
CONFIG_DIR = PROJECT / 'config'
RESULTS_DIR = PROJECT / 'backtest' / 'results'
LOGS_DIR = PROJECT / 'logs'

def setup_logging():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime('%Y-%m-%d')
    log_file = LOGS_DIR / f'auto_reoptimize_{today}.log'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ]
    )
    return log_file

def run_script(script_name, timeout=3600, extra_args=None):
    """Run a Python script and return (returncode, stdout, stderr)."""
    script_path = SCRIPTS / script_name
    if not script_path.exists():
        logging.error(f"Script not found: {script_path}")
        return -1, "", f"Script not found: {script_path}"
    cmd = [sys.executable, str(script_path), *(extra_args or [])]
    logging.info(f"Running: {' '.join(str(x) for x in cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
            cwd=str(PROJECT),
        )
        if result.stdout:
            logging.info(f"STDOUT:\n{result.stdout[-2000:]}")
        if result.stderr:
            logging.warning(f"STDERR:\n{result.stderr[-1000:]}")
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logging.error(f"Script timed out after {timeout}s: {script_name}")
        return -2, "", "Timeout"
    except Exception as e:
        logging.error(f"Error running {script_name}: {e}")
        return -3, "", str(e)

def load_health_report():
    """Load the most recent health check report."""
    today = datetime.now().strftime('%Y-%m-%d')
    rpt = LOGS_DIR / f'health_check_{today}.json'
    if rpt.exists():
        with open(rpt) as f:
            return json.load(f)
    return None

def backup_config():
    """Backup current active config with timestamp."""
    src = CONFIG_DIR / 'active' / 'asx.json'
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dst = CONFIG_DIR / f'backup_asx_{ts}.json'
    shutil.copy2(src, dst)
    logging.info(f"Config backed up: {dst}")
    return dst

def load_metrics(filepath):
    """Load metrics from a results JSON file."""
    if not Path(filepath).exists():
        return None
    with open(filepath) as f:
        return json.load(f)

def compare_configs(old_metrics, new_metrics):
    """Compare old vs new config metrics. Returns True if new is better on both full and OOS."""
    if not old_metrics or not new_metrics:
        return False
    try:
        old_full_cagr = old_metrics.get('full_metrics', {}).get('cagr_pct', 0)
        new_full_cagr = new_metrics.get('full_metrics', {}).get('cagr_pct', 0)
        old_oos_cagr = old_metrics.get('out_of_sample', {}).get('cagr_pct', 0)
        new_oos_cagr = new_metrics.get('out_of_sample', {}).get('cagr_pct', 0)
        old_full_sharpe = old_metrics.get('full_metrics', {}).get('sharpe', 0)
        new_full_sharpe = new_metrics.get('full_metrics', {}).get('sharpe', 0)
        old_oos_sharpe = old_metrics.get('out_of_sample', {}).get('sharpe', 0)
        new_oos_sharpe = new_metrics.get('out_of_sample', {}).get('sharpe', 0)
        full_better = new_full_cagr > old_full_cagr and new_full_sharpe > old_full_sharpe
        oos_better = new_oos_cagr > old_oos_cagr and new_oos_sharpe > old_oos_sharpe
        logging.info(f"Full: old CAGR={old_full_cagr:.2f}% new CAGR={new_full_cagr:.2f}%")
        logging.info(f"Full: old Sharpe={old_full_sharpe:.4f} new Sharpe={new_full_sharpe:.4f}")
        logging.info(f"OOS:  old CAGR={old_oos_cagr:.2f}% new CAGR={new_oos_cagr:.2f}%")
        logging.info(f"OOS:  old Sharpe={old_oos_sharpe:.4f} new Sharpe={new_oos_sharpe:.4f}")
        logging.info(f"Full better: {full_better}, OOS better: {oos_better}")
        return full_better and oos_better
    except Exception as e:
        logging.error(f"Error comparing metrics: {e}")
        return False

def main():
    log_file = setup_logging()
    logging.info("=" * 60)
    logging.info("AUTO-REOPTIMIZATION PIPELINE STARTED")
    logging.info("=" * 60)

    # Step 1: Health Check
    logging.info("\n--- STEP 1: Health Check ---")
    rc, stdout, stderr = run_script('health_check.py', timeout=300)
    report = load_health_report()

    if rc == 0:
        logging.info("System is HEALTHY. No reoptimization needed.")
        logging.info(f"Metrics: {json.dumps(report.get('metrics', {}), indent=2) if report else 'N/A'}")
        logging.info("Pipeline complete. Exiting.")
        return 0

    if report:
        logging.warning(f"System is DEGRADED. Flags: {report.get('flags', [])}")
    else:
        logging.warning(f"Health check returned code {rc}, no report found.")

    # Step 2: Backup current config
    logging.info("\n--- STEP 2: Backup Current Config ---")
    backup_path = backup_config()

    # Store old validation results path
    old_validation = RESULTS_DIR / 'v92_oos_validation.json'
    old_metrics = None
    if old_validation.exists():
        with open(old_validation) as f:
            old_data = json.load(f)
        old_metrics = old_data.get('test1_time_period_split', {})

    # Step 3: Run Reoptimization (stage candidate config; do not overwrite active yet)
    logging.info("\n--- STEP 3: Reoptimization ---")
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    candidate_config = CONFIG_DIR / f'config_candidate_auto_reopt_{ts}.json'
    candidate_validation = RESULTS_DIR / f'v92_oos_validation_candidate_{ts}.json'
    rc, stdout, stderr = run_script(
        'reoptimize_full_universe.py',
        timeout=7200,
        extra_args=['--candidate-path', str(candidate_config)]
    )
    if rc != 0:
        logging.error(f"Reoptimization failed (rc={rc})")
        logging.info("Active config remains unchanged (staged candidate flow).")
        logging.info(f"Backup remains available: {backup_path}")
        logging.info("Pipeline aborted.")
        return 1
    logging.info("Reoptimization completed successfully.")
    logging.info(f"Staged candidate config: {candidate_config}")

    # Step 4: Validate staged candidate config
    logging.info("\n--- STEP 4: OOS Validation ---")
    rc, stdout, stderr = run_script(
        'validate_oos.py',
        timeout=3600,
        extra_args=['--config-path', str(candidate_config), '--output-path', str(candidate_validation)]
    )
    if rc != 0:
        logging.warning(f"OOS validation returned code {rc}, checking results anyway...")

    # Step 5: Compare old vs new
    logging.info("\n--- STEP 5: Compare Old vs New ---")
    new_validation = candidate_validation
    new_metrics = None
    if new_validation.exists():
        with open(new_validation) as f:
            new_data = json.load(f)
        new_metrics = new_data.get('test1_time_period_split', {})

    if compare_configs(old_metrics, new_metrics):
        logging.info("NEW config is BETTER on both full and OOS periods.")
        shutil.copy2(candidate_config, CONFIG_DIR / 'active' / 'asx.json')
        shutil.copy2(candidate_validation, RESULTS_DIR / 'v92_oos_validation.json')
        logging.info("Promoted candidate config to config/active/asx.json.")
        logging.info("Promoted candidate validation report to v92_oos_validation.json.")
    else:
        logging.warning("NEW config is NOT better on both dimensions.")
        logging.info("Leaving active config unchanged. Candidate retained for review:")
        logging.info(f"  Candidate config: {candidate_config}")
        logging.info(f"  Candidate validation: {candidate_validation}")

    logging.info("\n" + "=" * 60)
    logging.info("AUTO-REOPTIMIZATION PIPELINE COMPLETE")
    logging.info(f"Log file: {log_file}")
    logging.info("=" * 60)
    return 0

if __name__ == '__main__':
    sys.exit(main())
