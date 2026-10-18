#!/usr/bin/env python3
"""
scripts/test/socket_smoke_test.py
Verifies two-way UDP datagram exchange, payload integrity, sequence tracking,
and sub-millisecond Round-Trip Time (RTT) timing between dcs-ctrl-node and dcs-plant-node.

Payload Contract:
  Controller -> Plant (UDP port 5005):
    {"seq": int, "u": float, "t_send": float}
  Plant -> Controller (Echo back):
    {"seq": int, "pv": float, "t_send": float, "t_echo": float}
"""

import argparse
import json
import logging
import socket
import statistics
import sys
import time
from typing import List

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("SocketSmokeTest")


def run_client(
    host: str, port: int, count: int, dt: float, timeout_factor: float = 0.8
) -> bool:
    """
    Executes a sequence of UDP probe datagrams against an active plant node server.
    Measures RTT, delivery consistency, and payload compliance.

    :param host: Target plant IPv4 address.
    :param port: Target plant UDP port.
    :param count: Total datagram probes to dispatch.
    :param dt: Inter-sample period in seconds.
    :param timeout_factor: Fractional multiplier of dt for socket read timeout.
    :return: True if zero drops and zero corruptions occur.
    """
    timeout_sec = dt * timeout_factor
    logger.info(
        "Starting UDP Socket Smoke Test | Target: %s:%d | Datagrams: %d | dt: %.1fms | Timeout: %.1fms",
        host,
        port,
        count,
        dt * 1000.0,
        timeout_sec * 1000.0,
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout_sec)

    rtts_ms: List[float] = []
    dropped: int = 0
    corrupted: int = 0
    received: int = 0

    u_test_values = [0.0, 25.0, 50.0, 75.0, 100.0]
    t_start_test = time.perf_counter()

    for seq in range(1, count + 1):
        t_cycle_start = time.perf_counter()
        u_val = u_test_values[(seq - 1) % len(u_test_values)]
        t_send_wall = time.time()

        payload = {"seq": seq, "u": float(u_val), "t_send": t_send_wall}
        raw_bytes = json.dumps(payload).encode("utf-8")

        try:
            t_send_mono = time.perf_counter()
            sock.sendto(raw_bytes, (host, port))
            resp_bytes, _ = sock.recvfrom(2048)
            t_recv_mono = time.perf_counter()

            rtt_ms = (t_recv_mono - t_send_mono) * 1000.0
            rtts_ms.append(rtt_ms)

            try:
                resp_data = json.loads(resp_bytes.decode("utf-8"))
                pv = resp_data.get("pv", float("nan"))
                echo_seq = resp_data.get("seq", -1)

                if echo_seq == seq:
                    received += 1
                    logger.debug(
                        "RX Seq %d | PV: %.2f%% | RTT: %.3f ms", seq, pv, rtt_ms
                    )
                else:
                    corrupted += 1
                    logger.warning(
                        "Sequence mismatch: Expected %d, received %d", seq, echo_seq
                    )

            except (json.JSONDecodeError, KeyError) as err:
                corrupted += 1
                logger.error("Malformed payload on seq %d: %s", seq, err)

        except TimeoutError:
            dropped += 1
            logger.warning("Timeout occurred waiting for datagram seq %d", seq)
        except OSError as err:
            dropped += 1
            logger.error("Socket error on seq %d: %s", seq, err)

        t_elapsed = time.perf_counter() - t_cycle_start
        if t_elapsed < dt:
            time.sleep(dt - t_elapsed)

    sock.close()
    total_test_dur = time.perf_counter() - t_start_test

    logger.info("=== Socket Probe Execution Summary ===")
    logger.info(
        "Dispatched: %d | Valid RX: %d | Dropped: %d (%.1f%%) | Corrupted: %d",
        count,
        received,
        dropped,
        (dropped / count) * 100.0,
        corrupted,
    )
    logger.info("Total Test Runtime: %.3f s", total_test_dur)

    if rtts_ms:
        logger.info(
            "RTT Metrics -> Min: %.3f ms | Mean: %.3f ms | Max: %.3f ms | StdDev: %.3f ms",
            min(rtts_ms),
            statistics.mean(rtts_ms),
            max(rtts_ms),
            statistics.stdev(rtts_ms) if len(rtts_ms) > 1 else 0.0,
        )

    return dropped == 0 and corrupted == 0


def run_mock_server(host: str, port: int) -> None:
    """
    Standalone mock plant echo server for isolated testing when plant_interface is inactive.

    :param host: Interface address to bind.
    :param port: UDP port to expose.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    logger.info("Mock Plant Echo Server listening on %s:%d", host, port)

    mock_pv = 10.0
    try:
        while True:
            data, addr = sock.recvfrom(2048)
            t_echo = time.time()
            try:
                req = json.loads(data.decode("utf-8"))
                u_in = float(req.get("u", 0.0))
                mock_pv += (u_in * 0.1) - (mock_pv * 0.05)

                resp = {
                    "seq": req.get("seq", 0),
                    "pv": float(mock_pv),
                    "t_send": req.get("t_send", 0.0),
                    "t_echo": t_echo,
                }
                sock.sendto(json.dumps(resp).encode("utf-8"), addr)
            except Exception as err:
                logger.error("Error processing mock datagram: %s", err)
    except KeyboardInterrupt:
        logger.info("Mock server received termination signal.")
    finally:
        sock.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Socket Smoke Test for Wireless PID Control Loop"
    )
    parser.add_argument(
        "--mode",
        choices=["client", "mock-server"],
        default="client",
        help="Probe client or mock server",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Plant node target IP")
    parser.add_argument("--port", type=int, default=5005, help="Target UDP port")
    parser.add_argument(
        "--count", type=int, default=50, help="Number of datagram probes"
    )
    parser.add_argument(
        "--dt", type=float, default=0.05, help="Sampling delta in seconds (20 Hz)"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "client":
        passed = run_client(
            host=args.host, port=args.port, count=args.count, dt=args.dt
        )
        sys.exit(0 if passed else 1)
    else:
        run_mock_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
