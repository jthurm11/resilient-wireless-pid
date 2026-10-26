#!/usr/bin/env python3
"""
scripts/analysis/aggregate_and_postprocess.py
Post-processing and empirical data aggregation pipeline for Distributed Control Systems.
Consolidates raw trial CSV snapshots, guarantees mechanical plant geometry schemas,
derives loop execution deltas, and computes thesis-ready control performance indicators:
ITAE, RMSE, Settling Time (2% band), Max Overshoot %, and Packet Loss Rate.
"""

import argparse
import glob
import logging
import os
from typing import Any

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("PostProcessing")

# Canonical Plant Geometric Specifications
PLANT_GEOMETRY_REGISTRY = {
    "acrylic": {
        "tube_length_cm": 45.085,  # 17.75 inches measured reference
        "tube_inner_dia_mm": 51.0,
        "tube_outer_dia_mm": 55.0,
    },
    "cardboard": {
        "tube_length_cm": 78.0,
        "tube_inner_dia_mm": 50.8,
        "tube_outer_dia_mm": 54.0,
    },
}


def parse_distribution_type(trial_id: str) -> str:
    """Extract stochastic delay distribution profile from trial identifier."""
    lowered = str(trial_id).lower()
    if "pareto" in lowered:
        return "pareto"
    elif "gaussian" in lowered or "normal" in lowered or "gauss" in lowered:
        return "gaussian"
    return "none"


def resolve_tube_type(df: pd.DataFrame, default_tube: str) -> str:
    """Infer tube type from DataFrame content, trial identifiers, or fallback default."""
    if "tube_type" in df.columns and not df["tube_type"].dropna().empty:
        return str(df["tube_type"].dropna().iloc[0]).strip().lower()

    if "trial_id" in df.columns and not df["trial_id"].dropna().empty:
        sample_id = str(df["trial_id"].dropna().iloc[0]).lower()
        for tube_key in PLANT_GEOMETRY_REGISTRY.keys():
            if tube_key in sample_id:
                return tube_key

    return default_tube.strip().lower()


def calculate_control_kpis(trial_df: pd.DataFrame) -> dict[str, Any]:
    """Computes academic-standard control performance indicators over a trial time-series."""
    if trial_df.empty or "error" not in trial_df.columns:
        return {}

    t_s = trial_df["t_rel_s"].to_numpy()
    e = trial_df["error"].to_numpy()
    sp = trial_df["setpoint"].to_numpy()
    pv = trial_df["process_variable"].to_numpy()
    loss = (
        trial_df["is_loss"].to_numpy()
        if "is_loss" in trial_df.columns
        else np.zeros_like(pv)
    )

    # Trapezoidal ITAE: integral(t * |e(t)| dt)
    abs_e = np.abs(e)
    dt = np.diff(t_s, prepend=0.0)
    dt[0] = 0.05
    itae = float(np.sum(t_s * abs_e * dt))

    # Root Mean Square Error (RMSE)
    rmse = float(np.sqrt(np.mean(e**2)))

    # Mean Absolute Error (MAE)
    mae = float(np.mean(abs_e))

    # Packet Loss Percentage
    loss_rate_pct = float(np.mean(loss) * 100.0)

    # Maximum Percentage Overshoot (relative to target setpoint)
    target_sp = float(sp[-1]) if len(sp) > 0 else 1.0
    if target_sp > 0:
        max_pv = float(np.max(pv))
        overshoot_pct = float(max(0.0, (max_pv - target_sp) / target_sp * 100.0))
    else:
        overshoot_pct = 0.0

    # 2% Settling Time (time of last exit outside [0.98*SP, 1.02*SP])
    band = 0.02 * target_sp
    outside = np.where(np.abs(pv - target_sp) > band)[0]
    settling_time_s = float(t_s[outside[-1]]) if len(outside) > 0 else 0.0

    return {
        "itae": round(itae, 2),
        "rmse": round(rmse, 2),
        "mae": round(mae, 2),
        "overshoot_pct": round(overshoot_pct, 2),
        "settling_time_s": round(settling_time_s, 2),
        "loss_rate_pct": round(loss_rate_pct, 2),
    }


def postprocess_trial_dataframe(
    df: pd.DataFrame, default_env: str, default_tube: str = "acrylic"
) -> pd.DataFrame:
    """
    Sanitize signals, harmonize naming conventions, compute relative timing and
    execution deltas, and attach standardized mechanical plant geometry.
    """
    if df.empty:
        return df

    # Drop internal InfluxDB metadata
    drop_candidates = ["_start", "_stop", "_measurement", "result", "table"]
    cols_to_drop = [c for c in drop_candidates if c in df.columns]
    if cols_to_drop:
        df = df.drop(columns=cols_to_drop)

    # Harmonize algorithm naming convention: replace baseline -> discrete
    if "algorithm" in df.columns:
        df["algorithm"] = df["algorithm"].replace({"baseline": "discrete"})

    # Convert RFC-3339 timestamps and enforce monotonic order
    if "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True, format="ISO8601")
        sort_keys = [k for k in ["trial_id", "_time"] if k in df.columns]
        df = df.sort_values(by=sort_keys).reset_index(drop=True)

        if "trial_id" in df.columns:
            t_first = df.groupby("trial_id")["_time"].transform("first")
            df["t_rel_s"] = (df["_time"] - t_first).dt.total_seconds()
            df["dt_exec_ms"] = (
                df.groupby("trial_id")["_time"].diff().dt.total_seconds() * 1000.0
            ).fillna(50.0)
        else:
            df["t_rel_s"] = (df["_time"] - df["_time"].iloc[0]).dt.total_seconds()
            df["dt_exec_ms"] = (
                df["_time"].diff().dt.total_seconds() * 1000.0
            ).fillna(50.0)

    # Enforce canonical mechanical plant geometry
    resolved_tube = resolve_tube_type(df, default_tube)
    geometry_specs = PLANT_GEOMETRY_REGISTRY.get(
        resolved_tube, PLANT_GEOMETRY_REGISTRY["acrylic"]
    )

    df["tube_type"] = resolved_tube
    df["tube_length_cm"] = geometry_specs["tube_length_cm"]
    df["tube_inner_dia_mm"] = geometry_specs["tube_inner_dia_mm"]
    df["tube_outer_dia_mm"] = geometry_specs["tube_outer_dia_mm"]

    # Clean sentinel types
    if "fan_tach_rpm" in df.columns:
        df["fan_tach_rpm"] = pd.to_numeric(df["fan_tach_rpm"], errors="coerce")
    if "tach_valid" in df.columns:
        df["tach_valid"] = (
            df["tach_valid"].astype(bool)
            if df["tach_valid"].dtype != bool
            else df["tach_valid"]
        )

    # Stochastic distribution profile
    if "trial_id" in df.columns:
        df["distribution"] = df["trial_id"].apply(parse_distribution_type)
    else:
        df["distribution"] = "none"

    if "environment" not in df.columns:
        df["environment"] = default_env

    core_order = [
        "_time",
        "t_rel_s",
        "trial_id",
        "tube_type",
        "tube_length_cm",
        "tube_inner_dia_mm",
        "tube_outer_dia_mm",
        "algorithm",
        "environment",
        "distribution",
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
    ordered_cols = [c for c in core_order if c in df.columns] + [
        c for c in df.columns if c not in core_order
    ]
    return df[ordered_cols]


def infer_execution_timestamp(dfs: list[pd.DataFrame], files: list[str]) -> str:
    """Infer batch timestamp from earliest telemetry sample; fallback to filesystem mtime."""
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

    oldest_mtime = min(os.path.getmtime(f) for f in files)
    fallback_dt = pd.to_datetime(oldest_mtime, unit="s", utc=True)
    fallback_str = fallback_dt.strftime("%Y%m%d_%H%M%S")
    logger.warning("Falling back to filesystem mtime timestamp: %s", fallback_str)
    return fallback_str


def process_exports(
    raw_dir: str,
    output_dir: str,
    environment: str,
    tube_type: str = "acrylic",
    override_timestamp: str | None = None,
) -> dict[str, str]:
    """Aggregate raw trial CSV exports into partitioned experimental datasets and compute summary KPIs."""
    os.makedirs(output_dir, exist_ok=True)

    # Strictly partition patterns so legacy sweep files do not leak into vector 0
    vector_manifest = {
        "vector0_dynamic_baselines": [
            os.path.join(raw_dir, "baseline_acrylic_*.csv"),
            os.path.join(raw_dir, "baseline_cardboard_*.csv"),
            os.path.join(raw_dir, "dynamic_baseline_*.csv"),
        ],
        "vector1_deadtime_sweeps": [
            os.path.join(raw_dir, "sweep_deadtime_*.csv"),
            os.path.join(raw_dir, "deadtime_*.csv"),
            os.path.join(raw_dir, "baseline_*ms_rtt*.csv"),
            os.path.join(raw_dir, "control_law_comparison_*ms_rtt*.csv"),
        ],
        "vector2_stochastic_jitter": [
            os.path.join(raw_dir, "sweep_jitter_*.csv"),
            os.path.join(raw_dir, "trial_jitter_*.csv"),
            os.path.join(raw_dir, "jitter_*.csv"),
        ],
        "vector3_burst_loss": [
            os.path.join(raw_dir, "sweep_loss_*.csv"),
            os.path.join(raw_dir, "burst_loss_*.csv"),
        ],
    }

    generated_files: dict[str, str] = {}
    all_kpis = []

    for vector_name, patterns in vector_manifest.items():
        matched_files: list[str] = []
        for p in patterns:
            matched_files.extend(glob.glob(p))
        matched_files = sorted(list(set(matched_files)))

        if not matched_files:
            logger.debug("No CSV artifacts matched for vector: %s", vector_name)
            continue

        valid_dfs = []
        valid_files = []
        for f in matched_files:
            try:
                temp_df = pd.read_csv(f)
                if not temp_df.empty:
                    valid_dfs.append(temp_df)
                    valid_files.append(f)
            except Exception as err:
                logger.error("Failed to read raw CSV snapshot %s: %s", f, err)

        if not valid_dfs:
            logger.warning(
                "All matched files for %s were empty or unreadable.", vector_name
            )
            continue

        ts_tag = override_timestamp or infer_execution_timestamp(valid_dfs, valid_files)

        combined = pd.concat(valid_dfs, ignore_index=True)
        processed = postprocess_trial_dataframe(
            combined, default_env=environment, default_tube=tube_type
        )

        out_filename = f"{vector_name}_{tube_type}_{environment}_{ts_tag}.csv"
        out_filepath = os.path.join(output_dir, out_filename)
        processed.to_csv(out_filepath, index=False)
        generated_files[vector_name] = out_filepath
        logger.info(
            "Persisted %s (%d records from %d trials)",
            out_filepath,
            len(processed),
            len(valid_files),
        )

        # Compute thesis-ready KPIs per trial within vector
        for t_id, grp in processed.groupby("trial_id"):
            kpi = calculate_control_kpis(grp)
            if kpi:
                kpi["trial_id"] = t_id
                kpi["vector"] = vector_name
                kpi["algorithm"] = grp["algorithm"].iloc[0]
                kpi["tube_type"] = grp["tube_type"].iloc[0]
                all_kpis.append(kpi)

    if all_kpis:
        summary_df = pd.DataFrame(all_kpis)
        summary_path = os.path.join(output_dir, "thesis_control_performance_kpis.csv")
        summary_df.to_csv(summary_path, index=False)
        logger.info("Persisted executive KPI summary table -> %s", summary_path)

    return generated_files


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 2 Empirical Data Post-Processing and Aggregation Pipeline"
    )
    parser.add_argument(
        "--raw-dir",
        type=str,
        default="/opt/resilient-wireless-pid/data_exports",
        help="Source directory containing raw InfluxDB trial CSV exports",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="/opt/resilient-wireless-pid/data_exports/processed",
        help="Destination directory for processed vector datasets",
    )
    parser.add_argument(
        "--environment",
        type=str,
        default=os.getenv("DCS_ENV", "docker"),
        help="Execution target environment (hardware_rpi, docker, proxmox_lxc)",
    )
    parser.add_argument(
        "--tube-type",
        type=str,
        choices=["acrylic", "cardboard"],
        default=os.getenv("DCS_TUBE", "acrylic"),
        help="Default mechanical column type (default: acrylic)",
    )
    parser.add_argument(
        "--timestamp",
        type=str,
        default=None,
        help="Manual timestamp override (YYYYMMDD_HHMMSS); default infers from telemetry",
    )
    args = parser.parse_args()

    logger.info(
        "Initializing post-processing pipeline [Env: %s | Tube: %s]",
        args.environment,
        args.tube_type,
    )

    generated = process_exports(
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        environment=args.environment,
        tube_type=args.tube_type,
        override_timestamp=args.timestamp,
    )
    logger.info(
        "Post-processing pipeline complete. %d vector dataset(s) persisted to %s",
        len(generated),
        args.output_dir,
    )


if __name__ == "__main__":
    main()
