# Virtual Testbed Setup Guide (Docker & Proxmox LXC)

This procedure configures an isolated dual-node Distributed Control System (DCS) testbed for pre-hardware simulation and validation. It ensures complete runtime parity with the bare-metal Raspberry Pi environment: deterministic socket addressing, isolated subnets (`10.10.10.0/24`), Linux kernel queue disciplines (`tc/netem`), and time-series telemetry logging.

The only functional deviation from bare metal is the plant runtime: virtual testbeds execute a continuous software physics twin (`run-plant --mode simulate`) instead of interacting with physical sensors and PWM fan drivers (`run-plant --mode hardware`).

Both Docker and Proxmox LXC tracks establish identical socket endpoints and network boundaries: 
* **`dcs-ctrl-node`:** `10.10.10.1` (`127.0.0.1` for intra-node IPC)
  * `10.10.10.1:5050` (TCP/HTTP) $\to$ Command & Control (C2) Orchestrator API / Web Console
  * `10.10.10.1:8086` (TCP/HTTP) $\to$ InfluxDB v2 Line Protocol Engine
  * `10.10.10.1:3000` (TCP/HTTP) $\to$ Grafana Dashboards
* **`dcs-plant-node`:** `10.10.10.2`
  * `10.10.10.2:5005` (UDP) $\to$ Dynamic state-space listener (`step(u) -> pv`)


## Unified Testbed Management (`virtual_manager.sh`)

All provisioning, health verification, and teardown workflows are managed through the centralized orchestrator `scripts/infra/virtual_manager.sh`.

* **Automatic Backend Detection:** When executed on a development workstation with an active Docker daemon, it defaults to the **Docker** backend. When run on a Proxmox VE hypervisor host shell, it defaults to the **LXC** backend.
* **Explicit Targeting:** You can override auto-detection by passing `docker` or `lxc` explicitly as the first parameter.

```text
Usage:
  ./scripts/infra/virtual_manager.sh [docker|lxc] {create|destroy|status} [options]

```

## Deployment Architectures

Two isolated execution targets are supported:

1. **Docker Virtual Sim (Local Docker Host):** Runs the dual-node DCS environment locally via Docker Compose.

* Control plane services share the network namespace of `dcs-ctrl-node` (`10.10.10.1`), while `dcs-plant-node` executes in a dedicated container (`10.10.10.2`).
* The orchestrator automatically creates the `10.10.10.0/24` network bridge, initializes InfluxDB v2, mounts Grafana dashboards, and starts the real-time controller and plant runtimes automatically as container entrypoints.
* *Prerequisites:* Docker Engine 24.0+ and Docker Compose v2 plugin.

2. **LXC Virtual Twin (Proxmox VE Host):** Deploys two Debian 13 containers on a Proxmox VE host connected via an isolated Linux software bridge (i.e., `vmbr1`).

* The orchestrator persists hypervisor kernel modules, provisions an isolated network, creates CT 201 (`dcs-ctrl-node`) and CT 202 (`dcs-plant-node`), configures virtual environments, and starts the telemetry stack inside CT 201 via `telemetry_stack.sh`.
* Control loops run automatically via systemd services or can be invoked interactively inside container sessions.

## Workspace Provisioning & Access

### 1. Prepare the Host

Clone the source repository onto your preferred target host and enter the repository root:

```bash
git clone https://github.com/jthurm11/resilient-wireless-pid.git
cd resilient-wireless-pid

```

> [!NOTE]  
> The Proxmox VE Host can alternatively download the `virtual_manager.sh` script directly, instead of cloning the whole repository: 
> ```bash
> curl -fsSL https://raw.githubusercontent.com/jthurm11/resilient-wireless-pid/main/scripts/infra/virtual_manager.sh -o virtual_manager.sh
> ``` 
> Adjust file path accordingly in follow-on steps, i.e.: `bash ./virtual_manager.sh lxc {create|destroy|status}`  
>  

### 2. Provision the Testbed

Deploy the virtual testbed:

```bash
./scripts/infra/virtual_manager.sh create
# Explicit overrides:
#   ./scripts/infra/virtual_manager.sh docker create
#   ./scripts/infra/virtual_manager.sh lxc create

```

Check running container status:

```bash
./scripts/infra/virtual_manager.sh status

```

### 3. Inject Kernel Network Emulation (`tc/netem`)

Open a terminal session inside `dcs-ctrl-node`:

```bash
docker exec -it dcs-ctrl-node bash || pct enter 201 

```

Once the terminal session is open, apply network degradation to the outbound DCS interface (`wlan0`):

```bash
IFACE="wlan0"

# Inject 40ms nominal delay, ±10ms jitter, and 2% packet loss
tc qdisc add dev $IFACE root netem delay 40ms 10ms distribution normal loss 2%

# Inspect before/after active queue rules and test transit delay to dcs-plant-node
tc -s qdisc show dev $IFACE && echo
ping -c 10 10.10.10.2 && echo
tc -s qdisc show dev $IFACE && echo

# Restore default line-rate transmission
tc qdisc del dev $IFACE root 

exit

```

Example output from the inspection and ping commands: 
```text
qdisc netem 80e2: root refcnt 5 limit 1000 delay 40ms  10ms loss 2% seed 7257834873562157834
 Sent 0 bytes 0 pkt (dropped 0, overlimits 0 requeues 0) 
 backlog 0b 0p requeues 0

PING 10.10.10.2 (10.10.10.2) 56(84) bytes of data.
64 bytes from 10.10.10.2: icmp_seq=1 ttl=64 time=37.6 ms
64 bytes from 10.10.10.2: icmp_seq=2 ttl=64 time=37.7 ms
64 bytes from 10.10.10.2: icmp_seq=3 ttl=64 time=75.5 ms
64 bytes from 10.10.10.2: icmp_seq=4 ttl=64 time=33.4 ms
64 bytes from 10.10.10.2: icmp_seq=5 ttl=64 time=30.4 ms
64 bytes from 10.10.10.2: icmp_seq=6 ttl=64 time=36.3 ms
64 bytes from 10.10.10.2: icmp_seq=7 ttl=64 time=31.4 ms
64 bytes from 10.10.10.2: icmp_seq=8 ttl=64 time=28.4 ms
64 bytes from 10.10.10.2: icmp_seq=9 ttl=64 time=31.3 ms
64 bytes from 10.10.10.2: icmp_seq=10 ttl=64 time=49.7 ms

--- 10.10.10.2 ping statistics ---
10 packets transmitted, 10 received, 0% packet loss, time 9014ms
rtt min/avg/max/mdev = 28.449/39.188/75.539/13.396 ms

qdisc netem 80e2: root refcnt 5 limit 1000 delay 40ms  10ms loss 2% seed 7257834873562157834
 Sent 1064 bytes 12 pkt (dropped 0, overlimits 0 requeues 0) 
 backlog 0b 0p requeues 0

```

### 4. Decommissioning

> [!WARNING]  
> Danger zone  
>  

To stop containers, release network namespaces, and **purge** virtual volumes:

```bash
./scripts/infra/virtual_manager.sh destroy
# Explicit overrides:
#   ./scripts/infra/virtual_manager.sh docker destroy
#   ./scripts/infra/virtual_manager.sh lxc destroy

```

## Optional Steps: Container Access and Debugging

### Inspecting Docker Logs

In Docker, the controller and simulated plant loops start automatically on boot. To inspect logs:

```bash
# Follow real-time controller execution
docker compose -f deploy/docker/docker-compose.yml logs -f controller

# Follow simulated plant physics
docker compose -f deploy/docker/docker-compose.yml logs -f dcs-plant-node

```

### Inspecting Proxmox LXC Logs

To inspect logs from the Proxmox VE Host: 

```bash
# dcs-ctrl-node
pct exec 201 -- journalctl -u dcs-controller -f --no-pager 

# dcs-plant-node
pct exec 202 -- journalctl -u dcs-plant -f --no-pager

```

### Proxmox LXC Interactive Debugging

> [!CAUTION]  
> Interactive loop commands (e.g., `run-plant` or `run-controller`) are intended for debugging. Docker containers and Proxmox LXC containers execute these loops automatically on boot via entrypoints or systemd. Before launching interactive debugging sessions on Proxmox LXC, run `systemctl stop dcs-controller.service` (on CT 201) or `systemctl stop dcs-plant.service` (on CT 202) to prevent UDP socket and HTTP port collisions.  
> Services must be restarted (or containers reset) once complete: `systemctl restart <service-name>.service` or `docker restart <container-name>`.    
>  

To run the loop interactively on Proxmox, stop the background services and invoke the entry points directly:

* Plant `dcs-plant-node`: 

```bash
pct enter 202
```
```bash
systemctl stop dcs-plant.service
cd /opt/resilient-wireless-pid && source .venv/bin/activate
run-plant --mode simulate --host 0.0.0.0 --port 5005

systemctl restart dcs-plant.service
```

* Ctrl `dcs-ctrl-node`:

```bash
pct enter 201
```
```bash
systemctl stop dcs-controller.service
cd /opt/resilient-wireless-pid && source .venv/bin/activate
run-controller --enable-ui

systemctl restart dcs-controller.service
```
