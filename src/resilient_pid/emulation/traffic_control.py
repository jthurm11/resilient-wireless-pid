import subprocess
import logging
import re
import shutil

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TrafficControl")

class TrafficControlWrapper:
    """
    A robust Python wrapper around Linux Traffic Control (tc) and Network Emulation (netem)
    to automate latency, jitter, and packet loss injection for wireless control testing.
    """
    
    def __init__(self, interface: str = "wlan0"):
        self.interface = interface
        self._check_tc_availability()

    def _check_tc_availability(self):
        """Verify that the 'tc' command exists on the system."""
        if shutil.which("tc") is None:
            logger.warning("The 'tc' executable was not found in the system PATH. "
                           "Emulation commands will fail if executed on a non-Linux system.")

    def _run_cmd(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """Run a shell command using subprocess and handle errors cleanly."""
        try:
            logger.debug(f"Executing command: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result
        except subprocess.CalledProcessError as e:
            logger.error(f"Command failed: {' '.join(cmd)}")
            logger.error(f"Stderr: {e.stderr.strip()}")
            raise RuntimeError(f"tc command failed: {e.stderr.strip()}") from e

    def clear_rules(self) -> bool:
        """
        Deletes all root queuing disciplines (qdisc) on the interface.
        Equates to resetting the network to optimal/quiet conditions.
        """
        # We use a try-except here because deleting qdisc will error if no custom qdisc exists
        cmd = ["sudo", "tc", "qdisc", "del", "dev", self.interface, "root"]
        try:
            self._run_cmd(cmd)
            logger.info(f"Successfully cleared all traffic control rules on {self.interface}.")
            return True
        except Exception as e:
            # If there was no rule to delete, tc returns an error; we treat this as a success/no-op
            if "Cannot find qdisc" in str(e) or "No such file or directory" in str(e):
                logger.debug(f"No active traffic control rules to clear on {self.interface}.")
                return True
            logger.error(f"Failed to clear traffic control rules: {e}")
            return False

    def apply_rules(self, 
                    delay_ms: float = 0.0, 
                    jitter_ms: float = 0.0, 
                    delay_correlation_pct: float = 0.0,
                    loss_pct: float = 0.0, 
                    loss_correlation_pct: float = 0.0,
                    distribution: str = "normal") -> bool:
        """
        Apply network emulation rules (latency, jitter, packet loss) using tc/netem.
        
        Args:
            delay_ms (float): Mean network latency in milliseconds.
            jitter_ms (float): Delay variation (jitter) in milliseconds.
            delay_correlation_pct (float): Correlation value for jitter (0 to 100 %).
            loss_pct (float): Percentage of packets to drop (0 to 100 %).
            loss_correlation_pct (float): Correlation value for loss (0 to 100 %).
            distribution (str): Jitter distribution (e.g., 'normal', 'pareto', 'paretonormal').
            
        Returns:
            bool: True if rules were applied successfully, False otherwise.
        """
        # Always clear existing rules first to avoid 'File exists' errors
        self.clear_rules()

        # Build tc netem command structure
        # Base command: sudo tc qdisc add dev <iface> root handle 1: netem
        cmd = ["sudo", "tc", "qdisc", "add", "dev", self.interface, "root", "handle", "1:", "netem"]

        # Latency & Jitter configuration
        if delay_ms > 0:
            cmd.extend(["delay", f"{delay_ms}ms"])
            if jitter_ms > 0:
                cmd.append(f"{jitter_ms}ms")
                if delay_correlation_pct > 0:
                    cmd.append(f"{delay_correlation_pct}%")
                if distribution and distribution.lower() != "uniform":
                    cmd.extend(["distribution", distribution.lower()])

        # Packet Loss configuration
        if loss_pct > 0:
            cmd.extend(["loss", f"{loss_pct}%"])
            if loss_correlation_pct > 0:
                cmd.append(f"{loss_correlation_pct}%")

        # If neither delay nor loss is configured, we do not need to apply anything
        if delay_ms <= 0 and loss_pct <= 0:
            logger.info("No active emulation parameters specified (delay/loss are zero). Interface remains clean.")
            return True

        try:
            self._run_cmd(cmd)
            logger.info(f"Applied emulation to {self.interface}: "
                        f"Delay={delay_ms}ms (jitter={jitter_ms}ms, corr={delay_correlation_pct}%, dist={distribution}), "
                        f"Loss={loss_pct}% (corr={loss_correlation_pct}%)")
            return True
        except Exception as e:
            logger.error(f"Failed to apply traffic control rules: {e}")
            # Attempt cleanup on failure
            self.clear_rules()
            return False

    def show_active_rules(self) -> str:
        """
        Query tc to show currently active rules on the interface.
        
        Returns:
            str: Raw tc output showing active queuing disciplines.
        """
        cmd = ["tc", "qdisc", "show", "dev", self.interface]
        try:
            result = self._run_cmd(cmd)
            return result.stdout.strip()
        except Exception as e:
            logger.error(f"Failed to fetch active rules: {e}")
            return f"Error retrieving rules: {e}"

if __name__ == "__main__":
    # Standard quick test block to demonstrate functionality
    print("Testing TrafficControlWrapper initialization...")
    tc = TrafficControlWrapper(interface="lo")  # Use loopback for local testing
    
    print("\n1. Showing active rules (baseline):")
    print(tc.show_active_rules())
    
    print("\n2. Applying latency and jitter simulation (50ms delay, 10ms jitter, normal distribution):")
    success = tc.apply_rules(delay_ms=50.0, jitter_ms=10.0, distribution="normal")
    if success:
        print("Success! Current rules:")
        print(tc.show_active_rules())
    else:
        print("Failed to apply (expected if not run as sudo or on non-Linux OS).")
        
    print("\n3. Clearing rules...")
    tc.clear_rules()
    print("Cleared! Current rules:")
    print(tc.show_active_rules())
