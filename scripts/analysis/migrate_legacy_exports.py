#!/usr/bin/env python3
"""
scripts/analysis/migrate_legacy_exports.py
Normalizes legacy trial CSVs to adhere to the canonical 17-column DCS schema.
Populates missing physical telemetry with sentinel values and enforces
uniform geometric and timing properties.
"""

import argparse
import glob
import logging
import os
import shutil

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("LegacyMigration")

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


def migrate_file(filepath: str, backup: bool = True) -> bool:
    try:
        df = pd.read_csv(filepath)
        if df.empty:
            logger.warning("Skipping empty file: %s", filepath)
            return False

        # Drop internal InfluxDB query metadata
        drop_cols = [
            c
            for c in ["_start", "_stop", "_measurement", "result", "table"]
            if c in df.columns
        ]
        if drop_cols:
            df = df.drop(columns=drop_cols)

        # 1. Standardize timestamp and compute dt_exec_ms if missing
        if "_time" in df.columns:
            df["_time"] = pd.to_datetime(df["_time"], utc=True, format="ISO8601")
            df = df.sort_values(by="_time").reset_index(drop=True)
            if "dt_exec_ms" not in df.columns or df["dt_exec_ms"].isna().all():
                df["dt_exec_ms"] = (
                    df["_time"].diff().dt.total_seconds() * 1000.0
                ).fillna(50.0)

        # 2. Harmonize algorithm terminology
        if "algorithm" in df.columns:
            df["algorithm"] = df["algorithm"].replace({"baseline": "discrete"})
        else:
            # Infer algorithm from filename
            fname = os.path.basename(filepath).lower()
            if "smith" in fname:
                df["algorithm"] = "smith"
            elif "resilient" in fname:
                df["algorithm"] = "resilient"
            else:
                df["algorithm"] = "discrete"

        # 3. Inject standard column geometry
        if "tube_type" not in df.columns or df["tube_type"].isna().all():
            df["tube_type"] = "acrylic"
        df["tube_length_cm"] = 45.085
        df["tube_inner_dia_mm"] = 51.0

        # 4. Standardize sentinel states for missing physical metrics
        if "raw_distance_cm" not in df.columns:
            df["raw_distance_cm"] = np.nan
        if "height_cm" not in df.columns:
            df["height_cm"] = np.nan
        if "fan_tach_rpm" not in df.columns:
            df["fan_tach_rpm"] = -1.0
        if "tach_valid" not in df.columns:
            df["tach_valid"] = 0.0

        if "is_loss" not in df.columns:
            df["is_loss"] = 0.0

        # Ensure all columns in CANONICAL_SCHEMA are present
        for col in CANONICAL_SCHEMA:
            if col not in df.columns:
                df[col] = np.nan

        # Preserve any extra columns at the end
        extra_cols = [c for c in df.columns if c not in CANONICAL_SCHEMA]
        ordered_df = df[CANONICAL_SCHEMA + extra_cols]

        if backup:
            backup_path = filepath + ".bak"
            if not os.path.exists(backup_path):
                shutil.copy2(filepath, backup_path)

        ordered_df.to_csv(filepath, index=False)
        logger.info("Migrated: %s (%d records)", os.path.basename(filepath), len(df))
        return True

    except Exception as e:
        logger.error("Failed to migrate %s: %s", filepath, e)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate legacy DCS trial CSVs to canonical schema."
    )
    parser.add_argument(
        "--data-dir",
        default="./data_exports",
        help="Target directory containing trial CSV files.",
    )
    parser.add_argument(
        "--no-backup", action="store_true", help="Disable creation of .bak files."
    )
    args = parser.parse_args()

    files = glob.glob(os.path.join(args.data_dir, "*.csv"))
    migrated_count = 0
    for f in files:
        fname = os.path.basename(f)
        if fname.startswith("thesis_") or fname.startswith("multi_"):
            continue
        if migrate_file(f, backup=not args.no_backup):
            migrated_count += 1

    logger.info(
        "Migration complete: %d files successfully updated to canonical schema.",
        migrated_count,
    )


if __name__ == "__main__":
    main()
