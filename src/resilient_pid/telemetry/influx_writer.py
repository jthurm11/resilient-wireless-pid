import queue
import threading
import time
import logging
from typing import Optional
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("InfluxTelemetry")

class InfluxWriter:
    """
    A high-frequency, thread-safe, non-blocking telemetry writer for InfluxDB v2.
    Uses an internal queue and background worker thread to buffer and execute batch 
    writes, ensuring that the main real-time control loop (PID/Smith Predictor) 
    never suffers from network-induced blocking or CPU jitter caused by database I/O.
    """
    
    def __init__(self, 
                 url: str, 
                 token: str, 
                 org: str, 
                 bucket: str, 
                 batch_size: int = 100, 
                 flush_interval_sec: float = 0.5,
                 max_queue_size: int = 10000,
                 dry_run: bool = False):
        """
        Initialize the InfluxTelemetry writer.
        
        Args:
            url (str): InfluxDB instance URL.
            token (str): InfluxDB API authentication token.
            org (str): Organization name.
            bucket (str): Destination bucket for metrics.
            batch_size (int): Max number of points to write in a single batch.
            flush_interval_sec (float): Max time to wait before flushing buffered points.
            max_queue_size (int): Maximum size of the internal queue to prevent out-of-memory errors.
            dry_run (bool): If True, metrics are logged to standard output instead of written to InfluxDB.
        """
        self.url = url
        self.token = token
        self.org = org
        self.bucket = bucket
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec
        self.dry_run = dry_run
        
        self._queue = queue.Queue(maxsize=max_queue_size)
        self._active = False
        self._worker_thread: Optional[threading.Thread] = None
        self._client: Optional[InfluxDBClient] = None
        self._write_api = None

        if not self.dry_run:
            self._connect_client()
        else:
            logger.info("InfluxWriter initialized in DRY-RUN mode. Metrics will be printed to logger.")

    def _connect_client(self):
        """Establish connection with InfluxDB client."""
        try:
            self._client = InfluxDBClient(url=self.url, token=self.token, org=self.org)
            # We use SYNCHRONOUS here because the asynchronous batching is managed by our 
            # custom thread queue. This allows for tighter control over threads, exception propagation,
            # and timing characteristics on standard Raspberry Pi nodes.
            self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
            logger.info(f"InfluxWriter successfully connected to InfluxDB at {self.url} (Org: {self.org}, Bucket: {self.bucket})")
        except Exception as e:
            logger.error(f"Failed to connect to InfluxDB at {self.url}: {e}")
            logger.warning("Falling back to DRY-RUN mode due to connection failure.")
            self.dry_run = True

    def start(self):
        """Start the background consumer worker thread."""
        if self._active:
            logger.warning("InfluxWriter background worker is already running.")
            return
        
        self._active = True
        self._worker_thread = threading.Thread(target=self._worker_loop, name="InfluxWriterWorker", daemon=True)
        self._worker_thread.start()
        logger.info("InfluxWriter background worker thread started.")

    def stop(self):
        """Stop the background worker thread and flush remaining points."""
        if not self._active:
            return
        
        logger.info("Stopping InfluxWriter background worker thread... Flushing remaining points.")
        self._active = False
        if self._worker_thread:
            self._worker_thread.join(timeout=3.0)
            
        # Clean up client resources
        if self._client:
            try:
                self._client.close()
                logger.info("InfluxDB client connection closed safely.")
            except Exception as e:
                logger.error(f"Error closing InfluxDB client: {e}")

    def log_control_metrics(self, 
                            trial_id: str, 
                            algorithm: str, 
                            setpoint: float, 
                            process_variable: float, 
                            control_signal: float, 
                            error: float, 
                            rtt_ms: Optional[float] = None):
        """
        Push a control telemetry data point to the background queue.
        This call is highly efficient and non-blocking.
        """
        now_ns = time.time_ns()
        metric_data = {
            "measurement": "control_telemetry",
            "tags": {
                "trial_id": trial_id,
                "algorithm": algorithm
            },
            "fields": {
                "setpoint": float(setpoint),
                "process_variable": float(process_variable),
                "control_signal": float(control_signal),
                "error": float(error)
            },
            "timestamp": now_ns
        }
        
        if rtt_ms is not None:
            metric_data["fields"]["rtt_ms"] = float(rtt_ms)

        try:
            self._queue.put_nowait(metric_data)
        except queue.Full:
            # Overwrite-oldest strategy to prevent memory pressure and preserve controller runtime stability
            try:
                # Remove one item from the head of the queue to make space
                dropped_item = self._queue.get_nowait()
                self._queue.put_nowait(metric_data)
                logger.warning(f"Telemetry queue full. Dropped oldest metric from trial {dropped_item['tags']['trial_id']}.")
            except queue.Empty:
                # Race condition handled defensively
                pass

    def _worker_loop(self):
        """Background thread worker loop that processes and writes metrics."""
        buffered_points = []
        last_flush_time = time.time()
        
        while self._active or not self._queue.empty():
            try:
                # Retrieve from queue with a brief timeout to avoid infinite blocking
                metric_data = self._queue.get(timeout=0.1)
                
                # Convert our custom dictionary format to Influx Point format
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
            
            # Check flush triggers: buffer limit reached or time interval elapsed
            current_time = time.time()
            if buffered_points and (len(buffered_points) >= self.batch_size or 
                                    (current_time - last_flush_time) >= self.flush_interval_sec or 
                                    not self._active):
                self._flush_batch(buffered_points)
                buffered_points.clear()
                last_flush_time = current_time

    def _flush_batch(self, points: list[Point]):
        """Write a batch of points to InfluxDB or log if dry-run."""
        if self.dry_run:
            for p in points:
                logger.info(f"[DRY-RUN WRITE]: {p.to_line_protocol()}")
            return

        try:
            # Execute batch write synchronously (already run inside background thread, so safe from blocking main thread)
            self._write_api.write(bucket=self.bucket, org=self.org, record=points)
            logger.debug(f"Successfully flushed batch of {len(points)} metrics to InfluxDB.")
        except Exception as e:
            logger.error(f"Error flushing telemetry batch of size {len(points)} to InfluxDB: {e}")
            # In a robust production-grade system, you might implement a retry buffer,
            # but for this testing testbed we log the failure to prevent cascading heap depletion.
