#!/usr/bin/env python3
"""
scripts/test/telemetry_smoke_test.py
Synthetic smoke test to validate InfluxDB v2 line protocol ingestion.
Verifies asynchronous synchronous-write pipelines against active database buckets.
"""

import argparse
import logging
import math
import os
import sys
import time

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("TelemetrySmokeTest")


def run_smoke_test(
    url: str,
    token: str,
    org: str,
    bucket: str,
    steps: int = 500,
    dt: float = 0.02,
    setpoint: float = 100.0,
) -> bool:
    """
    Streams synthetic damped oscillation step telemetry to InfluxDB.

    :param url: InfluxDB instance target URL.
    :param token: Authentication Bearer token.
    :param org: Destination organization namespace.
    :param bucket: Target metrics bucket.
    :param steps: Total sample points to generate.
    :param dt: Sampling period in seconds (default: 20ms / 50Hz).
    :param setpoint: Target step reference.
    :return: True if ingestion succeeds without network exceptions.
    """
    logger.info("Connecting to InfluxDB at %s [Org: %s | Bucket: %s]", url, org, bucket)
    trial_id = f"smoke_test_{int(time.time())}"

    try:
        with InfluxDBClient(url=url, token=token, org=org, timeout=10_000) as client:
            write_api = client.write_api(write_options=SYNCHRONOUS)
            logger.info("Streaming %d synthetic steps for trial '%s'...", steps, trial_id)

            for k in range(steps):
                t_start = time.perf_counter()
                t = k * dt

                # Second-order underdamped synthetic trajectory: y(t) = SP * (1 - e^(-1.5t) * cos(3t))
                pv = setpoint * (1.0 - math.exp(-1.5 * t) * math.cos(3.0 * t))
                error = setpoint - pv
                u_t = 1.2 * error + 0.05 * math.sin(10.0 * t)
                rtt_ms = 5.0 + 2.0 * math.sin(5.0 * t)

                point = (
                    Point("control_telemetry")
                    .tag("trial_id", trial_id)
                    .tag("algorithm", "discrete")
                    .tag("environment", "smoke_test")
                    .field("setpoint", float(setpoint))
                    .field("process_variable", float(pv))
                    .field("control_signal", float(u_t))
                    .field("error", float(error))
                    .field("rtt_ms", float(rtt_ms))
                    .field("dt_exec_ms", float(dt * 1000.0))
                    .field("is_loss", 0.0)
                    .time(time.time_ns(), WritePrecision.NS)
                )

                write_api.write(bucket=bucket, org=org, record=point)

                elapsed = time.perf_counter() - t_start
                sleep_time = max(0.0, dt - elapsed)
                time.sleep(sleep_time)

        logger.info("Ingestion completed successfully for %s (%d records).", trial_id, steps)
        return True

    except Exception as err:
        logger.error("Telemetry ingestion pipeline failed: %s", err)
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synthetic InfluxDB Ingestion Smoke Test")
    parser.add_argument(
        "--url", default=os.getenv("INFLUXDB_URL", "http://localhost:8086"), help="InfluxDB endpoint"
    )
    parser.add_argument(
        "--token",
        default=os.getenv("INFLUXDB_TOKEN", "testbed_secret_token_123"),
        help="Access token",
    )
    parser.add_argument(
        "--org", default=os.getenv("INFLUXDB_ORG", "resilient_pid"), help="Target organization"
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("INFLUXDB_BUCKET", "wireless_pid_metrics"),
        help="Target bucket",
    )
    parser.add_argument("--steps", type=int, default=500, help="Number of telemetry steps")
    parser.add_argument("--dt", type=float, default=0.02, help="Sample delta in seconds")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    success = run_smoke_test(
        url=args.url,
        token=args.token,
        org=args.org,
        bucket=args.bucket,
        steps=args.steps,
        dt=args.dt,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()