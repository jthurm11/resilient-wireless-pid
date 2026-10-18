#!/usr/bin/env python3
"""
src/resilient_pid/emulation/traffic_control.py
Linux Traffic Control (tc) and Network Emulation (netem) programmatic wrapper.
Coordinates kernel-level egress queue manipulations for deterministic packet degradation.
"""

import argparse
import logging
import shutil
import subprocess
import sys
from typing import List

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("TrafficControl")


class TrafficControlWrapper:
    """
    Interface for Linux Traffic Control (tc) and Network Emulation (netem).
    Automates latency, stochastic jitter, and burst loss injection on egress interfaces.
    """

    def __init__(self, interface: str = "wlan0"):
        self.interface = interface
        self._check_tc_availability()

    def _check_tc_availability(self) -> None:
        """Verify presence of iproute2 'tc' binary in execution PATH."""
        if shutil.which("tc") is None:
            logger.warning(
                "Binary 'tc' not found in PATH. Network emulation requires Linux with iproute2."
            )

    def _run_cmd(self, cmd: List[str]) -> subprocess.CompletedProcess:
        """Execute a shell command with captured standard streams and error handling."""
        try:
            logger.debug("Executing tc command: %s", " ".join(cmd))
            return subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            logger.error("Command '%s' failed: %s", " ".join(cmd), e.stderr.strip())
            raise RuntimeError(f"tc invocation failure: {e.stderr.strip()}") from e

    def clear_rules(self) -> bool:
        """
        Deletes root queuing discipline (qdisc) rules from interface.
        Restores default kernel pfifo_fast line-rate operation.
        """
        cmd = ["sudo", "tc", "qdisc", "del", "dev", self.interface, "root"]
        try:
            self._run_cmd(cmd)
            logger.info("Flushed root traffic control disciplines on %s.", self.interface)
            return True
        except Exception as e:
            if "Cannot find qdisc" in str(e) or "No such file or directory" in str(e):
                logger.debug("Interface %s already unshaped (no root qdisc found).", self.interface)
                return True
            logger.error("Failed to clear rules on %s: %s", self.interface, e)
            return False

    def apply_rules(
        self,
        delay_ms: float = 0.0,
        jitter_ms: float = 0.0,
        delay_correlation_pct: float = 0.0,
        loss_pct: float = 0.0,
        loss_correlation_pct: float = 0.0,
        distribution: str = "normal",
    ) -> bool:
        """
        Configures egress netem queue discipline rules.

        :param delay_ms: Mean latency offset in milliseconds.
        :param jitter_ms: Stochastic dispersion envelope in milliseconds.
        :param delay_correlation_pct: Correlation factor between successive delays (0-100%).
        :param loss_pct: Drop probability percentage (0-100%).
        :param loss_correlation_pct: Burst loss correlation percentage (0-100%).
        :param distribution: Jitter distribution table ('normal', 'pareto', 'paretonormal').
        :return: True if kernel discipline successfully bound.
        """
        self.clear_rules()

        cmd = [
            "sudo",
            "tc",
            "qdisc",
            "add",
            "dev",
            self.interface,
            "root",
            "handle",
            "1:",
            "netem",
        ]

        if delay_ms > 0.0:
            cmd.extend(["delay", f"{delay_ms}ms"])
            if jitter_ms > 0.0:
                cmd.append(f"{jitter_ms}ms")
                if delay_correlation_pct > 0.0:
                    cmd.append(f"{delay_correlation_pct}%")
                if distribution and distribution.lower() != "uniform":
                    cmd.extend(["distribution", distribution.lower()])

        if loss_pct > 0.0:
            cmd.extend(["loss", f"{loss_pct}%"])
            if loss_correlation_pct > 0.0:
                cmd.append(f"{loss_correlation_pct}%")

        if delay_ms <= 0.0 and loss_pct <= 0.0:
            logger.info("Zero impairment specified; %s remains unshaped.", self.interface)
            return True

        try:
            self._run_cmd(cmd)
            logger.info(
                "Emulation active on %s [Delay: %.1fms ± %.1fms (%s) | Loss: %.1f%%]",
                self.interface,
                delay_ms,
                jitter_ms,
                distribution,
                loss_pct,
            )
            return True
        except Exception as e:
            logger.error("Failed applying netem discipline: %s", e)
            self.clear_rules()
            return False

    def show_active_rules(self) -> str:
        """Inspects and returns active kernel queuing disciplines on the interface."""
        cmd = ["tc", "qdisc", "show", "dev", self.interface]
        try:
            result = self._run_cmd(cmd)
            return str(result.stdout.strip())
        except Exception as e:
            logger.error("Failed retrieving qdisc status: %s", e)
            return f"Error: {e}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Linux Traffic Control Utility")
    parser.add_argument("--interface", default="lo", help="Target network interface (default: lo)")
    parser.add_argument("--delay", type=float, default=0.0, help="Latency in ms")
    parser.add_argument("--jitter", type=float, default=0.0, help="Jitter in ms")
    parser.add_argument("--loss", type=float, default=0.0, help="Loss percentage (0-100)")
    parser.add_argument("--dist", default="normal", choices=["normal", "pareto", "uniform"])
    parser.add_argument("--clear", action="store_true", help="Flush all active qdisc rules")
    args = parser.parse_args()

    tc = TrafficControlWrapper(interface=args.interface)
    if args.clear:
        tc.clear_rules()
        sys.exit(0)

    if args.delay > 0 or args.loss > 0:
        success = tc.apply_rules(
            delay_ms=args.delay,
            jitter_ms=args.jitter,
            loss_pct=args.loss,
            distribution=args.dist,
        )
        sys.exit(0 if success else 1)

    logger.info("Active Rules:\n%s", tc.show_active_rules())


if __name__ == "__main__":
    main()