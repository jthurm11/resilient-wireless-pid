#!/usr/bin/env python3
"""
scripts/test/validate_pipeline_integrity.py
Automated End-to-End Pipeline Verification Suite for Resilient Wireless DCS.
Validates CSV schemas, execution timing statistics, netem qdisc states,
and KPI calculation consistency across baseline and sweep artifacts.

Use this deployment checklist on to ensure operational parity between Docker and bare metal:
+----------------------------------------------------------------------------------------------------+
|                                    HARDWARE PRE-FLIGHT CHECKLIST                                   |
+---+----------------------+-------------------------------------------------------------------------+
| # | Step                 | Linux Command / Action                                                  |
+---+----------------------+-------------------------------------------------------------------------+
| 1 | Sync Codebase        | cd /opt/resilient-wireless-pid && git pull origin main                  |
+---+----------------------+-------------------------------------------------------------------------+
| 2 | Stop Service         | sudo systemctl stop dcs-plant.service                                   |
+---+----------------------+-------------------------------------------------------------------------+
| 3 | Run Diagnostics      | sudo .venv/bin/python3 src/resilient_pid/plant/plant_interface.py        |
|   |                      |   --op-test --ignore-tach                                               |
+---+----------------------+-------------------------------------------------------------------------+
| 4 | Restart Daemon       | sudo systemctl restart dcs-plant.service                                |
+---+----------------------+-------------------------------------------------------------------------+
| 5 | Verify InfluxDB      | curl -s -I http://10.10.10.1:8086/health | grep HTTP                     |
+---+----------------------+-------------------------------------------------------------------------+
| 6 | Execute Baseline     | sudo .venv/bin/python3 scripts/test/run_baselines.py                     |
|   |                      |   --algorithm all --tube-type acrylic --duration 300.0                  |
+---+----------------------+-------------------------------------------------------------------------+
| 7 | Run Network Sweeps   | sudo .venv/bin/python3 scripts/test/run_sweeps.py                        |
|   |                      |   --interface wlan0 --v1-duration 30.0 --v2-duration 35.0               |
+---+----------------------+-------------------------------------------------------------------------+
| 8 | Run KPI Aggregation  | .venv/bin/python3 scripts/analysis/aggregate_and_postprocess.py          |
|   |                      |   --environment hardware_rpi --tube-type acrylic                        |
+---+----------------------+-------------------------------------------------------------------------+
| 9 | Pull Datasets        | ./scripts/analysis/fetch_datasets.sh pi@10.10.10.1                      |
+---+----------------------+-------------------------------------------------------------------------+

"""

import glob
import os
import subprocess
import sys

import numpy as np
import pandas as pd

EXPORT_DIR = os.getenv("DCS_EXPORT_DIR", "./data_exports")
PROCESSED_DIR = os.path.join(EXPORT_DIR, "processed")

CANONICAL_SCHEMA = [
    "_time",
    "trial_id",
    "algorithm",
    "tube_type",
    "tube_length_cm",
    "tube_inner_dia_mm",
    "setpoint",
    "process_variable",
    "control_signal",
    "error",
    "raw_distance_cm",
    "height_cm",
    "fan_tach_rpm",
    "tach_valid",
    "rtt_ms",
    "dt_exec_ms",
    "is_loss",
]


def log_pass(msg: str) -> None:
    print(f"\033[92m[PASS]\033[0m {msg}")


def log_fail(msg: str) -> None:
    print(f"\033[91m[FAIL]\033[0m {msg}")


def log_info(msg: str) -> None:
    print(f"\033[94m[INFO]\033[0m {msg}")


def check_raw_exports() -> bool:
    """Validates raw trial exports for naming compliance and schema parity."""
    log_info(f"Auditing raw trial CSVs in {EXPORT_DIR}...")
    csv_files = glob.glob(os.path.join(EXPORT_DIR, "*.csv"))

    if not csv_files:
        log_fail("No raw CSV files found in export directory.")
        return False

    success = True
    for fpath in csv_files:
        fname = os.path.basename(fpath)

        # Skip non-trial files or aggregated summary tables
        if fname.startswith("thesis_") or fname.startswith("multi_"):
            continue

        try:
            df = pd.read_csv(fpath, nrows=5)
            missing = [c for c in CANONICAL_SCHEMA if c not in df.columns]
            if missing:
                log_fail(f"{fname}: Missing columns: {missing}")
                success = False
        except Exception as e:
            log_fail(f"{fname}: Failed to parse CSV: {e}")
            success = False

    if success:
        log_pass(
            f"All raw trial CSVs ({len(csv_files)} audited) adhere to canonical 17-column schema."
        )
    return success


def check_processed_datasets() -> bool:
    """Verifies aggregated datasets for geometric consistency and timing bounds."""
    log_info(f"Auditing processed vector datasets in {PROCESSED_DIR}...")
    vector_files = glob.glob(os.path.join(PROCESSED_DIR, "vector*.csv"))

    if not vector_files:
        log_fail("No processed vector CSVs found.")
        return False

    success = True
    for vpath in vector_files:
        vname = os.path.basename(vpath)
        try:
            df = pd.read_csv(vpath)

            # Geometry checks
            lengths = df["tube_length_cm"].unique()
            if len(lengths) != 1 or not np.isclose(lengths[0], 45.085):
                log_fail(
                    f"{vname}: Non-standard tube_length_cm values detected: {lengths}"
                )
                success = False

            # Timing checks
            dt_series = df["dt_exec_ms"].dropna()
            p50 = dt_series.median()
            if not (45.0 <= p50 <= 55.0):
                log_fail(
                    f"{vname}: Loop execution period p50 drifted out of spec: {p50:.2f} ms"
                )
                success = False

            # Contamination checks
            if "itae" in df.columns or "overshoot_pct" in df.columns:
                log_fail(
                    f"{vname}: KPI summary metrics leaked into raw time-series dataset!"
                )
                success = False

        except Exception as e:
            log_fail(f"{vname}: Post-processing verification failed: {e}")
            success = False

    if success:
        log_pass("All processed vector datasets satisfy geometric and timing bounds.")
    return success


def check_kpi_summary() -> bool:
    """Confirms calculation of thesis-ready control performance metrics."""
    kpi_path = os.path.join(PROCESSED_DIR, "thesis_control_performance_kpis.csv")
    if not os.path.exists(kpi_path):
        log_fail(f"KPI table not found at {kpi_path}")
        return False

    df = pd.read_csv(kpi_path)
    expected_kpis = [
        "itae",
        "rmse",
        "mae",
        "overshoot_pct",
        "settling_time_s",
        "loss_rate_pct",
    ]
    missing = [k for k in expected_kpis if k not in df.columns]

    if missing:
        log_fail(f"thesis_control_performance_kpis.csv missing KPI fields: {missing}")
        return False

    # Check for NaN/Inf in computed KPIs
    numeric_df = df[expected_kpis]
    if numeric_df.isna().any().any() or np.isinf(numeric_df.to_numpy()).any():
        log_fail("NaN or Infinite values present in KPI summary calculations.")
        return False

    log_pass(
        f"KPI table verified ({len(df)} trials summarized with complete performance metrics)."
    )
    return True


def check_qdisc_hygiene(interface: str = "wlan0") -> bool:
    """Verifies that Linux kernel tc/netem qdisc is clean and unshaped."""
    log_info(f"Checking kernel qdisc state on {interface}...")
    try:
        res = subprocess.run(
            ["tc", "qdisc", "show", "dev", interface],
            capture_output=True,
            text=True,
            check=False,
        )
        if "netem" in res.stdout:
            log_fail(
                f"Residual netem qdisc rules found active on {interface}: {res.stdout.strip()}"
            )
            return False
        log_pass(
            f"Interface {interface} root qdisc is clean (no residual netem rules)."
        )
        return True
    except FileNotFoundError:
        log_info(
            "tc binary unavailable in host environment; skipping kernel qdisc audit."
        )
        return True


def main() -> None:
    print("=================================================================")
    print("Resilient Wireless DCS - Pipeline End-to-End Verification Suite")
    print("=================================================================")

    r1 = check_raw_exports()
    r2 = check_processed_datasets()
    r3 = check_kpi_summary()
    r4 = check_qdisc_hygiene()

    print("=================================================================")
    if all([r1, r2, r3, r4]):
        print(
            "\033[92mALL PIPELINE INTEGRITY CHECKS PASSED: Environment Ready for Hardware Deployment.\033[0m"
        )
        sys.exit(0)
    else:
        print(
            "\033[91mPIPELINE AUDIT FAILED: Resolve reported defects before running hardware trials.\033[0m"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
