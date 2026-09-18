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

import socket
import json
import time
import argparse
import sys
import statistics
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("SocketSmokeTest")

def run_client(host: str, port: int, count: int, dt: float, timeout_factor: float = 0.8):
    """
    Executes a sequence of UDP probe datagrams against a running plant node server.
    Measures RTT, loss rate, and payload compliance.
    """
    timeout_sec = dt * timeout_factor
    logger.info(f"Starting UDP Socket Smoke Test target={host}:{port} count={count} dt={dt*1000:.1f}ms (timeout={timeout_sec*1000:.1f}ms)")
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout_sec)
    
    rtts_ms = []
    dropped = 0
    corrupted = 0
    received = 0
    
    # Test u profile (step variation)
    u_test_values = [0.0, 25.0, 50.0, 75.0, 100.0]

    print("\n" + "="*70)
    print(f"{'SEQ':<6} | {'STATUS':<10} | {'U (%)':<8} | {'PV (%)':<8} | {'RTT (ms)':<10} | {'DRIFT (ms)':<10}")
    print("="*70)

    t_start_test = time.perf_counter()

    for seq in range(1, count + 1):
        t_cycle_start = time.perf_counter()
        
        # Vary input duty cycle u across test sequence
        u_val = u_test_values[(seq - 1) % len(u_test_values)]
        
        t_send_mono = time.perf_counter()
        t_send_wall = time.time()
        
        payload = {
            "seq": seq,
            "u": float(u_val),
            "t_send": t_send_wall
        }
        
        raw_bytes = json.dumps(payload).encode("utf-8")
        
        try:
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
                    status = "OK"
                    received += 1
                else:
                    status = "SEQ_MISMATCH"
                    corrupted += 1
                    
                print(f"{seq:<6} | {status:<10} | {u_val:<8.1f} | {pv:<8.2f} | {rtt_ms:<10.3f} | {(t_recv_mono - t_cycle_start)*1000.0:<10.3f}")
                
            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Malformed JSON on seq {seq}: {e}")
                corrupted += 1
                print(f"{seq:<6} | {'CORRUPT':<10} | {u_val:<8.1f} | {'N/A':<8} | {rtt_ms:<10.3f} | N/A")
                
        except socket.timeout:
            dropped += 1
            print(f"{seq:<6} | {'TIMEOUT':<10} | {u_val:<8.1f} | {'N/A':<8} | {'TIMEOUT':<10} | N/A")
        except Exception as e:
            logger.error(f"Socket error on seq {seq}: {e}")
            dropped += 1

        # Precise drift-compensated cycle pacing
        t_elapsed = time.perf_counter() - t_cycle_start
        if t_elapsed < dt:
            time.sleep(dt - t_elapsed)

    sock.close()
    
    total_test_dur = time.perf_counter() - t_start_test
    
    print("="*70)
    print("\n--- TEST SUMMARY REPORT ---")
    print(f"Total Sent         : {count}")
    print(f"Received (Valid)   : {received}")
    print(f"Dropped (Timeout)  : {dropped} ({ (dropped/count)*100.0:.1f}% )")
    print(f"Corrupted          : {corrupted}")
    print(f"Total Test Duration: {total_test_dur:.3f} s")
    
    if rtts_ms:
        mean_rtt = statistics.mean(rtts_ms)
        std_rtt = statistics.stdev(rtts_ms) if len(rtts_ms) > 1 else 0.0
        min_rtt = min(rtts_ms)
        max_rtt = max(rtts_ms)
        
        print(f"\n--- RTT PERFORMANCE METRICS ---")
        print(f"Min RTT            : {min_rtt:.3f} ms")
        print(f"Max RTT            : {max_rtt:.3f} ms")
        print(f"Mean RTT           : {mean_rtt:.3f} ms")
        print(f"Jitter (StdDev)    : {std_rtt:.3f} ms")
    else:
        print("\n[!] Error: No valid RTT measurements collected.")
        sys.exit(1)
        
    if dropped > 0 or corrupted > 0:
        logger.warning(f"Smoke test completed with defects (Dropped: {dropped}, Corrupted: {corrupted}).")
        sys.exit(1)
    else:
        logger.info("Smoke test PASSED with 100% packet delivery and valid payload contract.")
        sys.exit(0)

def run_mock_server(host: str, port: int):
    """
    Standalone mock plant echo server for isolated testing if plant_interface.py is not active.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    logger.info(f"Mock Plant Echo Server listening on {host}:{port}")
    
    mock_pv = 10.0
    
    try:
        while True:
            data, addr = sock.recvfrom(2048)
            t_echo = time.time()
            try:
                req = json.loads(data.decode("utf-8"))
                u_in = req.get("u", 0.0)
                # Simple mock first-order response
                mock_pv += (u_in * 0.1) - (mock_pv * 0.05)
                
                resp = {
                    "seq": req.get("seq", 0),
                    "pv": float(mock_pv),
                    "t_send": req.get("t_send", 0.0),
                    "t_echo": t_echo
                }
                sock.sendto(json.dumps(resp).encode("utf-8"), addr)
            except Exception as e:
                logger.error(f"Error handling mock datagram: {e}")
    except KeyboardInterrupt:
        logger.info("Mock server shutting down.")
    finally:
        sock.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Socket Smoke Test for Wireless PID Control Loop")
    parser.add_argument("--mode", choices=["client", "mock-server"], default="client", help="Run mode: client probe or mock server")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Target plant node IP (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5005, help="UDP port (default: 5005)")
    parser.add_argument("--count", type=int, default=50, help="Number of probe datagrams (default: 50)")
    parser.add_argument("--dt", type=float, default=0.05, help="Sampling interval in seconds (default: 0.05s / 20Hz)")
    
    args = parser.parse_args()
    
    if args.mode == "client":
        run_client(host=args.host, port=args.port, count=args.count, dt=args.dt)
    else:
        run_mock_server(host=args.host, port=args.port)
