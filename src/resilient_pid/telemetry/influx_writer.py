import logging
import queue
import threading
import time
from typing import Any

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("InfluxTelemetry")


class InfluxWriter:
    """A high-frequency, thread-safe, non-blocking telemetry writer for InfluxDB v2.

    Uses an internal queue and background worker thread to buffer and execute batch
    writes, ensuring that the main real-time control loop (PID/Smith Predictor)
    never suffers from network-induced blocking or CPU jitter caused by database I/O.
    """

    def __init__(
        self,
        url: str,
        token: str,
        org: str,
        bucket: str,
        batch_size: int = 100,
        flush_interval_sec: float = 0.5,
        max_queue_size: int = 10000,
        dry_run: bool = False,
    ):
        self.url = url
        self.token = token
        self.org = org
        self.bucket = bucket
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec
        self.dry_run = dry_run

        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue_size)
        self._active: bool = False
        self._worker_thread: threading.Thread | None = None
        self._client: InfluxDBClient | None = None
        self._write_api: Any | None = None

        if not self.dry_run:
            self._connect_client()
        else:
            logger.info(
                "InfluxWriter initialized in DRY-RUN mode. Metrics will be printed to logger."
            )

    def _connect_client(self) -> None:
        """Establish connection with InfluxDB client."""
        try:
            self._client = InfluxDBClient(url=self.url, token=self.token, org=self.org)
            self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
            logger.info(
                f"InfluxWriter successfully connected to InfluxDB at {self.url} (Org: {self.org}, Bucket: {self.bucket})"
            )
        except Exception as e:
            logger.error(f"Failed to connect to InfluxDB at {self.url}: {e}")
            logger.warning("Falling back to DRY-RUN mode due to connection failure.")
            self.dry_run = True

    def start(self) -> None:
        """Start the background consumer worker thread."""
        if self._active:
            logger.warning("InfluxWriter background worker is already running.")
            return

        self._active = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="InfluxWriterWorker", daemon=True
        )
        self._worker_thread.start()
        logger.info("InfluxWriter background worker thread started.")

    def stop(self) -> None:
        """Stop the background worker thread and flush remaining points."""
        if not self._active:
            return

        logger.info(
            "Stopping InfluxWriter background worker thread... Flushing remaining points."
        )
        self._active = False
        if self._worker_thread:
            self._worker_thread.join(timeout=3.0)

        if self._client:
            try:
                self._client.close()
                logger.info("InfluxDB client connection closed safely.")
            except Exception as e:
                logger.error(f"Error closing InfluxDB client: {e}")

    def log_control_metrics(
        self,
        trial_id: str,
        algorithm: str,
        setpoint: float,
        process_variable: float,
        control_signal: float,
        error: float,
        rtt_ms: float | None = None,
    ) -> None:
        """Push a control telemetry data point to the background queue."""
        now_ns = time.time_ns()
        metric_data: dict[str, Any] = {
            "measurement": "control_telemetry",
            "tags": {"trial_id": trial_id, "algorithm": algorithm},
            "fields": {
                "setpoint": float(setpoint),
                "process_variable": float(process_variable),
                "control_signal": float(control_signal),
                "error": float(error),
            },
            "timestamp": now_ns,
        }

        if rtt_ms is not None:
            metric_data["fields"]["rtt_ms"] = float(rtt_ms)

        try:
            self._queue.put_nowait(metric_data)
        except queue.Full:
            try:
                dropped_item = self._queue.get_nowait()
                self._queue.put_nowait(metric_data)
                logger.warning(
                    f"Telemetry queue full. Dropped oldest metric from trial {dropped_item['tags']['trial_id']}."
                )
            except queue.Empty:
                pass

    def _worker_loop(self) -> None:
        """Background thread worker loop that processes and writes metrics."""
        buffered_points: list[Point] = []
        last_flush_time = time.time()

        while self._active or not self._queue.empty():
            try:
                metric_data = self._queue.get(timeout=0.1)

                point = Point(metric_data["measurement"])
                for k, v in metric_data["tags"].items():
                    point.tag(k, v)
                for k, v in metric_data["fields"].items():
                    point.field(k, v)
                point.time(metric_data["timestamp"], WritePrecision.NS)

                buffered_points.append(point)
                self._queue.task_done()

            except queue.Empty:
                pass

            current_time = time.time()
            if buffered_points and (
                len(buffered_points) >= self.batch_size
                or (current_time - last_flush_time) >= self.flush_interval_sec
                or not self._active
            ):
                self._flush_batch(buffered_points)
                buffered_points.clear()
                last_flush_time = current_time

    def _flush_batch(self, points: list[Point]) -> None:
        """Write a batch of points to InfluxDB or log if dry-run."""
        if self.dry_run:
            for p in points:
                logger.info(f"[DRY-RUN WRITE]: {p.to_line_protocol()}")
            return

        if self._write_api is None:
            logger.error("Attempted to flush points but write_api is not initialized.")
            return

        try:
            self._write_api.write(bucket=self.bucket, org=self.org, record=points)
            logger.debug(
                f"Successfully flushed batch of {len(points)} metrics to InfluxDB."
            )
        except Exception as e:
            logger.error(
                f"Error flushing telemetry batch of size {len(points)} to InfluxDB: {e}"
            )