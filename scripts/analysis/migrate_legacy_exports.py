#!/usr/bin/env python3
"""
scripts/analysis/migrate_legacy_exports.py
Normalizes legacy trial CSVs to adhere to the canonical DCS schema.
Injects adversary_rate_kbps, maps legacy vector fields to stages,
and populates missing physical telemetry with sentinel values.
"""

import argparse
import glob
import logging
import os
import re
import shutil

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("LegacyMigration")

# Harmonized 18-column raw schema
CANONICAL_SCHEMA = [
    "_time",
    "trial_id",
    "algorithm",
    "adversary_rate_kbps",
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

VECTOR_TO_STAGE_MAP = {
    "vector0_dynamic_baselines": "stage0_quiet_baseline",
    "vector1_deadtime_sweeps": "stage1_deadtime",
    "vector2_stochastic_jitter": "stage2_jitter",
    "vector3_burst_loss": "stage3_burst_loss",
    "vector4_cross_traffic": "stage4_cross_traffic",
}


def infer_adversary_rate(filename: str, df: pd.DataFrame) -> int:
    if (
        "adversary_rate_kbps" in df.columns
        and not df["adversary_rate_kbps"].isna().all()
    ):
        val = df["adversary_rate_kbps"].dropna().iloc[0]
        try:
            return int(val)
        except (ValueError, TypeError):
            pass

    match = re.search(r"crosstraffic_(\d+)k", filename.lower())
    if match:
        return int(match.group(1))
    return 0


def migrate_file(filepath: str, backup: bool = True) -> bool:
    try:
        df = pd.read_csv(filepath)
        if df.empty:
            logger.warning("Skipping empty file: %s", filepath)
            return False

        fname = os.path.basename(filepath)

        # Handle processed summary files separately
        if fname == "thesis_control_performance_kpis.csv":
            modified = False
            if "vector" in df.columns:
                df["stage"] = df["vector"].map(VECTOR_TO_STAGE_MAP).fillna(df["vector"])
                df = df.drop(columns=["vector"])
                modified = True
            if "adversary_rate_kbps" not in df.columns:
                df["adversary_rate_kbps"] = df["trial_id"].apply(
                    lambda tid: infer_adversary_rate(str(tid), pd.DataFrame())
                )
                modified = True
            if modified:
                if backup:
                    shutil.copy2(filepath, filepath + ".bak")
                df.to_csv(filepath, index=False)
                logger.info("Migrated executive KPI file: %s", fname)
            return True

        # Drop InfluxDB metadata
        drop_cols = [
            c
            for c in ["_start", "_stop", "_measurement", "result", "table"]
            if c in df.columns
        ]
        if drop_cols:
            df = df.drop(columns=drop_cols)

        # 1. Monotonic timestamp & execution delta
        if "_time" in df.columns:
            df["_time"] = pd.to_datetime(df["_time"], utc=True, format="ISO8601")
            df = df.sort_values(by="_time").reset_index(drop=True)
            if "dt_exec_ms" not in df.columns or df["dt_exec_ms"].isna().all():
                df["dt_exec_ms"] = (
                    df["_time"].diff().dt.total_seconds() * 1000.0
                ).fillna(50.0)

        # 2. Harmonize algorithm name
        if "algorithm" in df.columns:
            df["algorithm"] = df["algorithm"].replace({"baseline": "discrete"})
        else:
            lower_name = fname.lower()
            if "smith" in lower_name:
                df["algorithm"] = "smith"
            elif "resilient" in lower_name:
                df["algorithm"] = "resilient"
            else:
                df["algorithm"] = "discrete"

        # 3. Geometry specifications
        if "tube_type" not in df.columns or df["tube_type"].isna().all():
            df["tube_type"] = "acrylic"
        df["tube_length_cm"] = 45.085
        df["tube_inner_dia_mm"] = 51.0

        # 4. Resolve Stage and Adversary Rate
        df["adversary_rate_kbps"] = infer_adversary_rate(fname, df)

        if "vector" in df.columns:
            df["stage"] = df["vector"].map(VECTOR_TO_STAGE_MAP).fillna(df["vector"])
            df = df.drop(columns=["vector"])

        # 5. Populate sentinels
        if "raw_distance_cm" not in df.columns:
            df["raw_distance_cm"] = np.nan
        if "height_cm" not in df.columns:
            df["height_cm"] = np.nan
        if "fan_tach_rpm" not in df.columns:
            df["fan_tach_rpm"] = -1.0
        if "tach_valid" not in df.columns:
            df["tach_valid"] = False
        if "is_loss" not in df.columns:
            df["is_loss"] = 0.0

        for col in CANONICAL_SCHEMA:
            if col not in df.columns:
                df[col] = np.nan

        extra_cols = [c for c in df.columns if c not in CANONICAL_SCHEMA]
        ordered_df = df[CANONICAL_SCHEMA + extra_cols]

        if backup:
            backup_path = filepath + ".bak"
            if not os.path.exists(backup_path):
                shutil.copy2(filepath, backup_path)

        ordered_df.to_csv(filepath, index=False)
        logger.info("Migrated: %s (%d records)", fname, len(df))
        return True

    except Exception as e:
        logger.error("Failed to migrate %s: %s", filepath, e)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate legacy DCS trial CSVs to canonical Stage schema."
    )
    parser.add_argument("--data-dir", default="./data_exports")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    files = glob.glob(os.path.join(args.data_dir, "**/*.csv"), recursive=True)
    migrated_count = 0
    for f in sorted(files):
        if f.endswith(".bak"):
            continue
        if migrate_file(f, backup=not args.no_backup):
            migrated_count += 1

    logger.info("Migration complete: %d file(s) updated.", migrated_count)


if __name__ == "__main__":
    main()
