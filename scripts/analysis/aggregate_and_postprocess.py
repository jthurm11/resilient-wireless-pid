#!/usr/bin/env python3
"""
scripts/analysis/aggregate_and_postprocess.py
Post-processing and data aggregation pipeline for empirical sweeps.
Consolidates raw trial CSV snapshots, infers sweep start timestamps from telemetry,
tags execution environments and distribution profiles, derives relative trajectories,
and partitions output datasets by experimental impairment vector.
"""

import argparse
import glob
import logging
import os
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("PostProcessing")


def parse_distribution_type(trial_id: str) -> str:
    """Extract stochastic delay distribution profile from trial identifier."""
    lowered = str(trial_id).lower()
    if "pareto" in lowered:
        return "pareto"
    elif "gaussian" in lowered or "normal" in lowered:
        return "gaussian"
    return "none"


def postprocess_trial_dataframe(df: pd.DataFrame, default_env: str) -> pd.DataFrame:
    """Apply signal sanitization, relative timing, and loop execution deltas."""
    if df.empty:
        return df

    # Drop internal InfluxDB query boundaries and metadata fields
    drop_candidates = ["_start", "_stop", "_measurement", "result", "table"]
    cols_to_drop = [c for c in drop_candidates if c in df.columns]
    if cols_to_drop:
        df = df.drop(columns=cols_to_drop)

    # Convert RFC-3339 timestamps and enforce monotonic order per trial
    if "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
        df = df.sort_values(by=["trial_id", "_time"]).reset_index(drop=True)

        # Compute relative elapsed seconds t_rel (t - t0) per trial
        df["t_rel_s"] = df.groupby("trial_id")["_time"].transform(
            lambda s: (s - s.iloc[0]).dt.total_seconds()
        )

        # Compute empirical inter-sample loop execution delta dt_exec_ms (target: 50.0 ms)
        df["dt_exec_ms"] = (
            df.groupby("trial_id")["_time"].diff().dt.total_seconds() * 1000.0
        )
        df["dt_exec_ms"] = df["dt_exec_ms"].fillna(50.0)

    # Parse stochastic jitter distribution profile
    if "trial_id" in df.columns:
        df["distribution"] = df["trial_id"].apply(parse_distribution_type)

    # Apply execution environment tag
    if "environment" not in df.columns:
        df["environment"] = default_env

    # Enforce standard scientific column ordering
    core_order = [
        "_time",
        "t_rel_s",
        "trial_id",
        "algorithm",
        "environment",
        "distribution",
        "setpoint",
        "process_variable",
        "control_signal",
        "error",
        "rtt_ms",
        "dt_exec_ms",
        "is_loss",
    ]
    ordered_cols = [c for c in core_order if c in df.columns] + [
        c for c in df.columns if c not in core_order
    ]
    return df[ordered_cols]


def infer_execution_timestamp(dfs: List[pd.DataFrame], files: List[str]) -> str:
    """
    Infer run timestamp from the earliest recorded telemetry sample.
    Falls back to the oldest file modification time if _time is unavailable.
    """
    min_times = []
    for df in dfs:
        if "_time" in df.columns and not df.empty:
            converted = pd.to_datetime(df["_time"], utc=True, errors="coerce").dropna()
            if not converted.empty:
                min_times.append(converted.min())

    if min_times:
        earliest_dt = min(min_times)
        inferred = earliest_dt.strftime("%Y%m%d_%H%M%S")
        logger.info("Inferred sweep execution timestamp from telemetry: %s", inferred)
        return inferred

    # Fallback to earliest POSIX mtime of the input files
    oldest_mtime = min(os.path.getmtime(f) for f in files)
    fallback_dt = pd.to_datetime(oldest_mtime, unit="s", utc=True)
    fallback_str = fallback_dt.strftime("%Y%m%d_%H%M%S")
    logger.warning("Falling back to filesystem mtime timestamp: %s", fallback_str)
    return fallback_str


def process_exports(
    raw_dir: str,
    output_dir: str,
    environment: str,
    override_timestamp: Optional[str] = None,
) -> Dict[str, str]:
    """Aggregate raw trial CSV files into vector-partitioned research datasets."""
    os.makedirs(output_dir, exist_ok=True)

    vector_manifest = {
        "vector1_deadtime_sweeps": [
            os.path.join(raw_dir, "baseline_*ms_rtt.csv"),
            os.path.join(raw_dir, "control_law_comparison_*ms_rtt.csv"),
        ],
        "vector2_stochastic_jitter": [
            os.path.join(raw_dir, "trial_jitter_*.csv"),
        ],
        "vector3_burst_loss": [
            os.path.join(raw_dir, "burst_loss_*.csv"),
        ],
    }

    generated_files = {}

    for vector_name, patterns in vector_manifest.items():
        matched_files: List[str] = []
        for p in patterns:
            matched_files.extend(glob.glob(p))
        matched_files = sorted(list(set(matched_files)))

        if not matched_files:
            logger.debug("No files matched for vector: %s", vector_name)
            continue

        dfs = [pd.read_csv(f) for f in matched_files]
        if not dfs:
            continue

        # Infer timestamp from the batch telemetry or use manual override
        ts_tag = override_timestamp or infer_execution_timestamp(dfs, matched_files)

        combined = pd.concat(dfs, ignore_index=True)
        processed = postprocess_trial_dataframe(combined, default_env=environment)

        out_filename = f"{vector_name}_{environment}_{ts_tag}.csv"
        out_filepath = os.path.join(output_dir, out_filename)
        processed.to_csv(out_filepath, index=False)
        generated_files[vector_name] = out_filepath
        logger.info(
            "Persisted %s (%d records from %d trials)",
            out_filepath,
            len(processed),
            len(matched_files),
        )

    return generated_files


def main():
    parser = argparse.ArgumentParser(
        description="Phase 2 Empirical Data Post-Processing and Aggregation Pipeline"
    )
    parser.add_argument(
        "--raw-dir",
        type=str,
        default="data_exports",
        help="Source directory containing raw InfluxDB trial CSV exports",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data_exports/processed",
        help="Destination directory for processed vector datasets",
    )
    parser.add_argument(
        "--environment",
        type=str,
        default=os.getenv("DCS_ENV", "docker"),
        help="Target execution environment tag (docker, proxmox_lxc, hardware_rpi)",
    )
    parser.add_argument(
        "--timestamp",
        type=str,
        default=None,
        help="Manual timestamp override (YYYYMMDD_HHMMSS); default infers from data",
    )
    args = parser.parse_args()

    logger.info("Initializing post-processing pipeline for target: %s", args.environment)
    generated = process_exports(
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        environment=args.environment,
        override_timestamp=args.timestamp,
    )
    logger.info("Post-processing aggregation complete. %d vector files generated.", len(generated))


if __name__ == "__main__":
    main()