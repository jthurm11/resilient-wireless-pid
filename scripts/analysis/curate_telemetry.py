#!/usr/bin/env python3
"""
scripts/analysis/curate_telemetry.py
Single-responsibility telemetry curation and schema conformance engine.
Audits raw DCS trial CSV logs, enforces canonical 28-column typing, trims
multi-run Flux query gaps, repairs collapsed batch-flush timestamps, and
compiles versioned + canonical master Apache Parquet and CSV datasets.
"""

import argparse
import glob
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("TelemetryCurator")

PLANT_GEOMETRY: dict[str, dict[str, float]] = {
    "acrylic": {
        "tube_length_cm": 45.085,
        "tube_inner_dia_mm": 51.0,
        "tube_outer_dia_mm": 55.0,
    },
    "cardboard": {
        "tube_length_cm": 78.0,
        "tube_inner_dia_mm": 50.8,
        "tube_outer_dia_mm": 54.0,
    },
}

ALGORITHM_MAP: dict[str, str] = {
    "baseline": "discrete",
    "pid": "discrete",
    "discrete_pid": "discrete",
    "smith_predictor": "smith",
    "resilient_observer": "resilient",
    "resilient_pid": "resilient",
}

COLUMN_ALIASES: dict[str, str] = {
    "_time": "raw_time",
    "time": "raw_time",
    "control_signal": "u_out",
    "u": "u_out",
    "process_variable": "pv",
    "dt_exec_ms": "dt_ms",
    "dt": "dt_ms",
    "rtt": "rtt_ms",
}

MASTER_SCHEMA_COLUMNS: list[str] = [
    "timestamp",
    "iso_time",
    "dt_ms",
    "trial_id",
    "algorithm",
    "environment",
    "stage",
    "impairment_category",
    "impairment_label",
    "distribution",
    "delay_ms",
    "jitter_ms",
    "loss_pct",
    "adversary_rate_kbps",
    "tube_type",
    "tube_length_cm",
    "tube_inner_dia_mm",
    "tube_outer_dia_mm",
    "setpoint",
    "pv",
    "error",
    "u_out",
    "raw_distance_cm",
    "height_cm",
    "fan_tach_rpm",
    "tach_valid",
    "rtt_ms",
    "is_loss",
]


def parse_distribution_type(identifier: str) -> str:
    """Infer stochastic delay distribution profile from trial identifier or filename."""
    lowered = identifier.lower()
    if "pareto" in lowered:
        return "pareto"
    if any(k in lowered for k in ["gaussian", "normal", "gauss"]):
        return "gaussian"
    return "none"


def resolve_temporal_series(df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """
    Sort DataFrame chronologically and derive relative monotonic seconds (timestamp)
    starting at 0.0s alongside canonical UTC ISO-8601 strings (iso_time).
    - Uses unit-agnostic .dt.total_seconds() to prevent [us] vs [ns] scaling bugs.
    - Detects multi-second inter-batch gaps (> 5.0s) caused by static legacy trial_ids
      within a -30m Flux query window and isolates the final contiguous run.
    - Reconstructs collapsed batch-flush timestamps (mean step < 5ms).
    """
    n_rows = len(df)
    time_source = "synthetic_50ms"

    if "raw_time" in df.columns:
        num_time = pd.to_numeric(df["raw_time"], errors="coerce")
        if num_time.notna().all() and (num_time.abs() > 1e8).any():
            max_v = float(num_time.max())
            divisor = (
                1e9
                if max_v > 1e16
                else (1e6 if max_v > 1e13 else (1e3 if max_v > 1e10 else 1.0))
            )
            epoch_s = num_time / divisor
            df = (
                df.assign(_sort_t=epoch_s)
                .sort_values(by="_sort_t")
                .reset_index(drop=True)
            )
            sorted_epoch = df["_sort_t"]
            df["timestamp"] = (sorted_epoch - sorted_epoch.iloc[0]).astype("float64")
            df["iso_time"] = pd.to_datetime(
                sorted_epoch, unit="s", utc=True
            ).dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            df = df.drop(columns=["_sort_t"])
            time_source = "epoch_numeric"
        else:
            dt_parsed = pd.to_datetime(
                df["raw_time"], utc=True, format="ISO8601", errors="coerce"
            )
            if dt_parsed.isna().all():
                dt_parsed = pd.to_datetime(df["raw_time"], utc=True, errors="coerce")

            if dt_parsed.notna().any():
                df = (
                    df.assign(_sort_t=dt_parsed)
                    .sort_values(by="_sort_t")
                    .reset_index(drop=True)
                )
                sorted_dt = df["_sort_t"]
                df["timestamp"] = (
                    (sorted_dt - sorted_dt.iloc[0]).dt.total_seconds().astype("float64")
                )
                df["iso_time"] = sorted_dt.dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ").fillna(
                    ""
                )
                df = df.drop(columns=["_sort_t"])
                time_source = "iso8601"

    if time_source == "synthetic_50ms":
        for fallback_col in ["timestamp", "t_rel_s"]:
            if fallback_col in df.columns:
                rel_num = pd.to_numeric(df[fallback_col], errors="coerce")
                if rel_num.notna().any():
                    df = (
                        df.assign(_sort_t=rel_num)
                        .sort_values(by="_sort_t")
                        .reset_index(drop=True)
                    )
                    sorted_rel = df["_sort_t"].ffill().fillna(0.0)
                    df["timestamp"] = (sorted_rel - sorted_rel.iloc[0]).astype(
                        "float64"
                    )
                    df["iso_time"] = ""
                    df = df.drop(columns=["_sort_t"])
                    time_source = f"relative_{fallback_col}"
                    break

    if time_source == "synthetic_50ms":
        df["timestamp"] = np.arange(n_rows, dtype=np.float64) * 0.05
        df["iso_time"] = ""

    # Check for multi-second clock discontinuities (> 5.0s) within a single trial
    # (caused by legacy static trial_id reuse within InfluxDB's -30m export window)
    step_diffs = df["timestamp"].diff().fillna(0.0)
    if (step_diffs > 5.0).any():
        # Reconstruct continuous timeline by replacing >5.0s dead-air gaps with 0.05s
        clamped_diffs = step_diffs.where(step_diffs <= 5.0, 0.05)
        df["timestamp"] = clamped_diffs.cumsum().astype("float64")
        time_source = f"{time_source}_gap_bridged"

    # Guard against genuinely collapsed batch-flush timestamps (mean step < 5ms)
    total_span = (
        float(df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]) if n_rows > 1 else 0.0
    )
    mean_dt_s = total_span / max(1, n_rows - 1)
    if n_rows >= 20 and mean_dt_s < 0.005:
        if "dt_ms" in df.columns:
            dt_series = pd.to_numeric(df["dt_ms"], errors="coerce").fillna(50.0)
            dt_series = dt_series.where((dt_series >= 5.0) & (dt_series <= 500.0), 50.0)
            dt_s = dt_series / 1000.0
            reconstructed = dt_s.cumsum() - dt_s.iloc[0]
            df["timestamp"] = reconstructed.astype("float64")
        else:
            df["timestamp"] = np.arange(n_rows, dtype=np.float64) * 0.05
        time_source = f"{time_source}_reconstructed_50ms"

    return df, time_source


def conform_single_csv(
    filepath: str,
    curated_at_utc: str,
    default_env: str | None = None,
    min_samples: int = 50,
) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    """
    Read, validate, and conform a single raw CSV into the 28-column master schema.
    Returns (conformed_df, manifest_record).
    """
    fname = os.path.basename(filepath)
    manifest: dict[str, Any] = {
        "filename": fname,
        "trial_id": "",
        "status": "valid",
        "rows": 0,
        "duration_sec": 0.0,
        "initial_pv": 0.0,
        "is_cold_start": False,
        "time_source": "",
        "environment": "",
        "stage": "",
        "algorithm": "",
        "curated_at_utc": curated_at_utc,
        "notes": "",
    }

    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        manifest["status"] = "rejected_empty_file"
        return None, manifest

    try:
        df = pd.read_csv(filepath)
    except Exception as err:
        logger.error("Failed reading %s: %s", filepath, err)
        manifest["status"] = "rejected_parse_error"
        manifest["notes"] = str(err)
        return None, manifest

    if df.empty:
        manifest["status"] = "rejected_zero_rows"
        return None, manifest

    # 1. Strip InfluxDB query artifacts
    influx_cols = ["_start", "_stop", "_measurement", "result", "table", "vector"]
    df = df.drop(columns=[c for c in influx_cols if c in df.columns], errors="ignore")

    # 2. Normalize column headers
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns=COLUMN_ALIASES)

    # 3. Temporal alignment, monotonic sorting, and gap/collapse repair
    df, time_src = resolve_temporal_series(df)
    manifest["time_source"] = time_src

    if "dt_ms" not in df.columns or df["dt_ms"].isna().all():
        dt_calc = df["timestamp"].diff().fillna(0.05) * 1000.0
        df["dt_ms"] = dt_calc.where((dt_calc > 0.0) & (dt_calc <= 500.0), 50.0)
    else:
        dt_num = pd.to_numeric(df["dt_ms"], errors="coerce").fillna(50.0)
        df["dt_ms"] = dt_num.where((dt_num > 0.0) & (dt_num <= 500.0), 50.0)

    # 4. Trial Identifier & Algorithm
    if "trial_id" not in df.columns or df["trial_id"].isna().all():
        df["trial_id"] = os.path.splitext(fname)[0]
    trial_id_str = str(df["trial_id"].dropna().iloc[0]).strip()
    df["trial_id"] = trial_id_str
    manifest["trial_id"] = trial_id_str

    search_token = f"{fname.lower()}_{trial_id_str.lower()}"

    if "algorithm" in df.columns and df["algorithm"].notna().any():
        raw_alg = str(df["algorithm"].dropna().iloc[0]).strip().lower()
        alg_resolved = ALGORITHM_MAP.get(raw_alg, raw_alg)
    else:
        alg_resolved = "discrete"
        for k, v in ALGORITHM_MAP.items():
            if k in search_token:
                alg_resolved = v
                break
    df["algorithm"] = alg_resolved
    manifest["algorithm"] = alg_resolved

    # 5. Environment normalization (bridges bare_metal vs hardware_rpi)
    env_raw = default_env or ""
    if not env_raw and "environment" in df.columns and df["environment"].notna().any():
        env_raw = str(df["environment"].dropna().iloc[0]).strip().lower()
    if not env_raw:
        if "docker" in search_token:
            env_raw = "docker"
        elif any(k in search_token for k in ["proxmox", "lxc"]):
            env_raw = "proxmox_lxc"
        else:
            env_raw = "hardware_rpi"

    if env_raw in ["bare_metal", "hardware_rpi", "rpi", "hardware"]:
        env_norm = "hardware_rpi"
    elif "proxmox" in env_raw or "lxc" in env_raw:
        env_norm = "proxmox_lxc"
    elif "docker" in env_raw:
        env_norm = "docker"
    else:
        env_norm = env_raw
    df["environment"] = env_norm
    manifest["environment"] = env_norm

    # 6. Physical Tube Geometry
    tube_val = "acrylic"
    if "tube_type" in df.columns and df["tube_type"].notna().any():
        tube_val = str(df["tube_type"].dropna().iloc[0]).strip().lower()
    elif "cardboard" in search_token:
        tube_val = "cardboard"
    geom = PLANT_GEOMETRY.get(tube_val, PLANT_GEOMETRY["acrylic"])
    df["tube_type"] = tube_val
    df["tube_length_cm"] = float(geom["tube_length_cm"])
    df["tube_inner_dia_mm"] = float(geom["tube_inner_dia_mm"])
    df["tube_outer_dia_mm"] = float(geom["tube_outer_dia_mm"])

    # 7. Experimental Stage & Treatment Factors
    if (
        "adversary_rate_kbps" not in df.columns
        or df["adversary_rate_kbps"].isna().all()
    ):
        m_rate = re.search(r"crosstraffic_(\d+)k", search_token)
        adv_rate = int(m_rate.group(1)) if m_rate else 0
        df["adversary_rate_kbps"] = adv_rate
    else:
        df["adversary_rate_kbps"] = (
            pd.to_numeric(df["adversary_rate_kbps"], errors="coerce")
            .fillna(0)
            .astype("int64")
        )
    adv_val = int(df["adversary_rate_kbps"].iloc[0])

    if (
        any(k in search_token for k in ["vector4", "stage4", "crosstraffic", "iperf"])
        or adv_val > 0
    ):
        stage, category = "stage4_cross_traffic", "crosstraffic"
    elif any(k in search_token for k in ["vector3", "stage3", "loss"]):
        stage, category = "stage3_burst_loss", "loss"
    elif any(k in search_token for k in ["vector2", "stage2", "jitter"]):
        stage, category = "stage2_jitter", "jitter"
    elif any(
        k in search_token
        for k in ["vector1", "stage1", "deadtime", "delay", "_rtt", "rtt"]
    ):
        stage, category = "stage1_deadtime", "delay"
    else:
        stage, category = "stage0_quiet_baseline", "quiet_baseline"

    df["stage"] = stage
    df["impairment_category"] = category
    df["distribution"] = parse_distribution_type(search_token)
    manifest["stage"] = stage

    if "delay_ms" not in df.columns or df["delay_ms"].isna().all():
        m_del = re.search(r"(?:delay_|rtt|_)?(\d+)ms(?:_rtt|-j|$|_)", search_token)
        if m_del and stage == "stage1_deadtime":
            df["delay_ms"] = float(m_del.group(1))
        elif stage in ["stage2_jitter", "stage3_burst_loss"]:
            df["delay_ms"] = 40.0
        else:
            df["delay_ms"] = 0.0
    else:
        df["delay_ms"] = pd.to_numeric(df["delay_ms"], errors="coerce").fillna(0.0)

    if "jitter_ms" not in df.columns or df["jitter_ms"].isna().all():
        # Match explicit '-j15ms' first before falling back to 'jitter_..._10ms'
        m_jit = re.search(r"-j(\d+)ms", search_token) or re.search(
            r"jitter_(?:pareto_|gauss_|gaussian_|normal_)?(\d+)ms", search_token
        )
        if m_jit and stage == "stage2_jitter":
            df["jitter_ms"] = float(m_jit.group(1))
        elif stage == "stage3_burst_loss":
            df["jitter_ms"] = 10.0
        else:
            df["jitter_ms"] = 0.0
    else:
        df["jitter_ms"] = pd.to_numeric(df["jitter_ms"], errors="coerce").fillna(0.0)

    if "loss_pct" not in df.columns or df["loss_pct"].isna().all():
        m_loss = re.search(r"(?:loss_|burst)(\d+)pct", search_token)
        df["loss_pct"] = float(m_loss.group(1)) if m_loss else 0.0
    else:
        df["loss_pct"] = pd.to_numeric(df["loss_pct"], errors="coerce").fillna(0.0)

    if "impairment_label" not in df.columns or df["impairment_label"].isna().all():
        if category == "crosstraffic":
            df["impairment_label"] = f"crosstraffic_{adv_val}k"
        elif category == "delay":
            df["impairment_label"] = f"delay_{int(df['delay_ms'].iloc[0])}ms"
        elif category == "jitter":
            df["impairment_label"] = f"jitter_{int(df['jitter_ms'].iloc[0])}ms"
        elif category == "loss":
            df["impairment_label"] = f"loss_{int(df['loss_pct'].iloc[0])}pct"
        else:
            df["impairment_label"] = "baseline_nominal"

    # 8. Core Signals & Sensor Sentinels
    for col in ["setpoint", "pv", "u_out", "rtt_ms"]:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype("float64")

    if "error" not in df.columns or df["error"].isna().all():
        df["error"] = (df["setpoint"] - df["pv"]).astype("float64")
    else:
        df["error"] = (
            pd.to_numeric(df["error"], errors="coerce")
            .fillna(df["setpoint"] - df["pv"])
            .astype("float64")
        )

    for phys_col in ["raw_distance_cm", "height_cm"]:
        if phys_col not in df.columns:
            df[phys_col] = np.nan
        else:
            df[phys_col] = pd.to_numeric(df[phys_col], errors="coerce").astype(
                "float64"
            )

    if "fan_tach_rpm" not in df.columns:
        df["fan_tach_rpm"] = -1.0
    else:
        df["fan_tach_rpm"] = (
            pd.to_numeric(df["fan_tach_rpm"], errors="coerce")
            .fillna(-1.0)
            .astype("float64")
        )

    for bool_col in ["tach_valid", "is_loss"]:
        if bool_col not in df.columns:
            df[bool_col] = False
        else:
            df[bool_col] = df[bool_col].fillna(False).astype(bool)

    initial_pv = float(df["pv"].iloc[0])
    manifest["rows"] = len(df)
    manifest["duration_sec"] = round(float(df["timestamp"].iloc[-1]), 3)
    manifest["initial_pv"] = round(initial_pv, 2)
    manifest["is_cold_start"] = bool(initial_pv <= 15.0)

    if len(df) < min_samples:
        manifest["status"] = "rejected_truncated"
        manifest["notes"] = f"Row count {len(df)} < min_samples ({min_samples})"
        return None, manifest

    return df[MASTER_SCHEMA_COLUMNS], manifest


def mirror_canonical_artifact(versioned_path: str, canonical_path: str) -> None:
    """Update the un-timestamped canonical pointer via atomic hardlink or copy."""
    if os.path.abspath(versioned_path) == os.path.abspath(canonical_path):
        return
    tmp_target = f"{canonical_path}.tmp"
    if os.path.exists(tmp_target):
        os.remove(tmp_target)
    try:
        os.link(versioned_path, tmp_target)
    except OSError:
        shutil.copy2(versioned_path, tmp_target)
    os.replace(tmp_target, canonical_path)


def run_reconciliation_audit(out_dir: str) -> None:
    """Execute 5-point conservation and integrity audit on curated outputs."""
    manifest_path = os.path.join(out_dir, "curation_manifest.csv")
    parquet_path = os.path.join(out_dir, "master_telemetry.parquet")
    manifest = pd.read_csv(manifest_path)
    master = pd.read_parquet(parquet_path)

    trial_durations = master.groupby("trial_id")["timestamp"].max()
    trial_meta = master.drop_duplicates("trial_id")

    logger.info("=== CURATION RECONCILIATION SUMMARY ===")
    logger.info(
        "Cataloged Trials: %d | Master Rows: %d | Unique Trial IDs: %d",
        len(manifest),
        len(master),
        master["trial_id"].nunique(),
    )
    logger.info(
        "Pre-Settled Trials: %d (%.1f%%) | Legacy Cold-Start Trials: %d (%.1f%%)",
        int((~manifest["is_cold_start"]).sum()),
        float((~manifest["is_cold_start"]).mean() * 100.0),
        int(manifest["is_cold_start"].sum()),
        float(manifest["is_cold_start"].mean() * 100.0),
    )
    logger.info(
        "Time Sources: %s",
        manifest["time_source"].value_counts().to_dict(),
    )
    logger.info(
        "Duration (s) -> Min: %.2f | 25%%: %.2f | Median: %.2f | 75%%: %.2f | Max: %.2f",
        float(trial_durations.min()),
        float(trial_durations.quantile(0.25)),
        float(trial_durations.median()),
        float(trial_durations.quantile(0.75)),
        float(trial_durations.max()),
    )
    logger.info(
        "Treatment Levels -> delay_ms: %s | jitter_ms: %s | loss_pct: %s | adversary_rate_kbps: %s",
        sorted(trial_meta["delay_ms"].unique().tolist()),
        sorted(trial_meta["jitter_ms"].unique().tolist()),
        sorted(trial_meta["loss_pct"].unique().tolist()),
        sorted(trial_meta["adversary_rate_kbps"].unique().tolist()),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate, conform, and compile raw DCS CSV logs into versioned and canonical Master Parquet/CSV datasets."
    )
    parser.add_argument("--raw-dir", type=str, default="./data_exports")
    parser.add_argument(
        "--out-dir", type=str, default="./data_exports/standardized_master"
    )
    parser.add_argument("--environment", type=str, default=None)
    parser.add_argument("--min-samples", type=int, default=50)
    parser.add_argument("--tag", type=str, default=None)
    parser.add_argument("--no-timestamp", action="store_true")
    parser.add_argument("--no-csv", action="store_true")
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Run post-compilation reconciliation summary.",
    )
    args = parser.parse_args()

    now_utc = datetime.now(timezone.utc)
    version_tag = args.tag or now_utc.strftime("%Y%m%d_%H%M%S")
    curated_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    os.makedirs(args.out_dir, exist_ok=True)
    out_abs = os.path.abspath(args.out_dir)

    candidate_files = sorted(glob.glob(os.path.join(args.raw_dir, "*.csv")))
    excluded_prefixes = (
        "master_telemetry",
        "curation_manifest",
        "aggregated_metrics_summary",
        "thesis_control_performance_kpis",
    )
    raw_files = [
        f
        for f in candidate_files
        if not os.path.basename(f).startswith(excluded_prefixes)
        and not os.path.abspath(f).startswith(out_abs)
    ]

    if not raw_files:
        logger.warning("No raw trial CSV files discovered in %s", args.raw_dir)
        return

    logger.info(
        "Auditing and conforming %d raw CSV files from %s [Build Tag: %s]...",
        len(raw_files),
        args.raw_dir,
        version_tag,
    )
    conformed_frames: list[pd.DataFrame] = []
    manifest_records: list[dict[str, Any]] = []

    for filepath in raw_files:
        df_clean, meta = conform_single_csv(
            filepath,
            curated_at_utc=curated_iso,
            default_env=args.environment,
            min_samples=args.min_samples,
        )
        manifest_records.append(meta)
        if df_clean is not None:
            conformed_frames.append(df_clean)

    manifest_df = pd.DataFrame(manifest_records)
    canonical_manifest = os.path.join(args.out_dir, "curation_manifest.csv")
    if args.no_timestamp:
        manifest_df.to_csv(canonical_manifest, index=False)
    else:
        versioned_manifest = os.path.join(
            args.out_dir, f"curation_manifest_{version_tag}.csv"
        )
        manifest_df.to_csv(versioned_manifest, index=False)
        mirror_canonical_artifact(versioned_manifest, canonical_manifest)

    if not conformed_frames:
        logger.error("No valid telemetry trials passed curation gates.")
        return

    master_df = pd.concat(conformed_frames, ignore_index=True)
    master_df = (
        master_df.drop_duplicates(subset=["trial_id", "timestamp"])
        .sort_values(by=["trial_id", "timestamp"])
        .reset_index(drop=True)
    )

    canonical_parquet = os.path.join(args.out_dir, "master_telemetry.parquet")
    if args.no_timestamp:
        master_df.to_parquet(
            canonical_parquet, engine="pyarrow", compression="snappy", index=False
        )
    else:
        versioned_parquet = os.path.join(
            args.out_dir, f"master_telemetry_{version_tag}.parquet"
        )
        master_df.to_parquet(
            versioned_parquet, engine="pyarrow", compression="snappy", index=False
        )
        mirror_canonical_artifact(versioned_parquet, canonical_parquet)

    if not args.no_csv:
        canonical_csv = os.path.join(args.out_dir, "master_telemetry.csv")
        if args.no_timestamp:
            master_df.to_csv(canonical_csv, index=False)
        else:
            versioned_csv = os.path.join(
                args.out_dir, f"master_telemetry_{version_tag}.csv"
            )
            master_df.to_csv(versioned_csv, index=False)
            mirror_canonical_artifact(versioned_csv, canonical_csv)

    logger.info(
        "Curation complete -> %s (%d rows across %d trials)",
        canonical_parquet,
        len(master_df),
        master_df["trial_id"].nunique(),
    )

    if args.audit:
        run_reconciliation_audit(args.out_dir)


if __name__ == "__main__":
    main()
