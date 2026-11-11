# Hardware Setup & Physical Integration Guide

## 1. Overview & System Topology

This document details the physical assembly, electrical wiring, sensor calibration, and pre-flight verification procedures for the **Resilient Wireless Control Hardware-in-the-Loop (HIL) testbed**. The physical system operates as a decoupled, dual-node distributed control setup over a dedicated, isolated Layer-2 Wi-Fi subnet (`10.10.10.0/24`).

### 1.1 Node Roles & Network Abstraction

The testbed distributes control execution and physical actuation across two distinct Raspberry Pi compute units:

*   **Plant Node (`dcs-plant-node` @ `10.10.10.2`)**: A Raspberry Pi 3B+ board running bare-metal Linux. It physically interfaces with the HC-SR04 ultrasonic distance sensor via GPIO and the Nidec UltraFlo blower via the EMC2101 I2C fan controller (`0x4C`). The plant daemon listens on UDP port `5005` for control actuation commands ($u(t) \in [0.0\%, 100.0\%]$) and transmits high-frequency process variable telemetry datagrams back to the controller.
*   **Controller Node (`dcs-ctrl-node` @ `10.10.10.1`)**: A Raspberry Pi 3B+ board operating in complete physical isolation from the plant sensors and actuators. It executes the C2 supervisory state engine (port `5050`) and real-time control algorithms (Discrete PID, Smith Predictor, and Resilient Observer PID) at $T_s = 50\text{ ms}$ ($20\text{ Hz}$). All interaction between the controller and the plant node occurs wirelessly over UDP sockets on `wlan0`.

```
                  +--------------------------------------------------+
                  |  Controller Node (dcs-ctrl-node @ 10.10.10.1)    |
                  |  - Real-Time Control Loop (Ts = 50ms)           |
                  |  - C2 REST State Engine (:5050)                  |
                  |  - No Physical Plant Wiring Connected           |
                  +------------------------+-------------------------+
                                           |
                                Wireless UDP Socket
                                (wlan0 @ 10.10.10.0/24)
                                           |
                  +------------------------v-------------------------+
                  |    Plant Node (dcs-plant-node @ 10.10.10.2)     |
                  |  - Plant Daemon Listener (UDP :5005)             |
                  |  - HC-SR04 Ultrasonic Worker Thread (GPIO 23/24)  |
                  |  - EMC2101 SMBus/I2C Driver (Bus 1, 0x4C)        |
                  +-----+--------------------------------------+-----+
                        |                                      |
                 GPIO / Voltage Divider                    I2C / PWM / TACH
                        |                                      |
                  +-----v----------------+              +------v--------------+
                  |  HC-SR04 Sensor      |              |  EMC2101 Controller |
                  |  (Top Standoff)      |              |  (Red Breakout PCB) |
                  +----------------------+              +------+--------------+
                                                               |
                                                          12V PWM Drive
                                                               |
                                                        +------v--------------+
                                                        |  Nidec UltraFlo Fan |
                                                        |  (12V Dual Blower)  |
                                                        +---------------------+
```

---

## 2. Mechanical Duct & Pneumatic Assembly

The physical plant consists of a vertical, transparent acrylic levitation cylinder mounted flush to a forced-draft blower plenum assembly.

### 2.1 Mechanical & Geometric Specifications

| Assembly Parameter | Physical Specification | Engineering Significance |
| :--- | :--- | :--- |
| **Column Material** | Clear Rigid Acrylic Duct | Transparent optical inspection, rigid wall acoustic reflection |
| **Tube Length ($L_{\text{tube}}$)** | $18.0\text{ in}$ ($45.085\text{ cm}$) | Total vertical travel flight corridor |
| **Inner Diameter ($D_i$)** | $51.0\text{ mm}$ ($0.051\text{ m}$) | Cross-sectional area $A_{\text{tube}} = \frac{\pi}{4}(0.051)^2 \approx 0.002043\text{ m}^2$ |
| **Outer Diameter ($D_o$)** | $55.0\text{ mm}$ ($0.055\text{ m}$) | Wall thickness $2.0\text{ mm}$ |
| **Levitating Mass** | ITTF Regulation Ping-Pong Ball | Diameter $d = 40.0\text{ mm}$, mass $m = 2.7\text{ g}$ ($0.0027\text{ kg}$) |
| **Annular Clearance** | $5.5\text{ mm}$ radial gap | $A_{\text{ball}}/A_{\text{tube}} \approx 61.5\%$ cross-sectional blockage |
| **Actuator Base** | Nidec UltraFlo R40W CA Series | 1U counter-rotating dual-impeller fan ($40 \times 40 \times 56\text{ mm}$) |
| **Plenum Transition** | Square-to-Round $40\times 40\text{ mm}$ Collar | Seals base transition to eliminate pneumatic blowby leaks |

### 2.2 Pneumatic Equilibrium & Force Derivation

The gravitational downward force exerted by the $2.7\text{ g}$ sphere is:

$$F_g = m \cdot g = 0.0027\text{ kg} \times 9.80665\text{ m/s}^2 \approx 0.0265\text{ N}$$

To hover the sphere within the acrylic column, the required differential pressure across the duct cross-section is:

$$\Delta P_{\text{hover}} = \frac{F_g}{A_{\text{tube}}} = \frac{0.0265\text{ N}}{0.002043\text{ m}^2} \approx 12.97\text{ Pa} \quad (0.052\text{ inwg})$$

The Nidec UltraFlo R40W dual counter-rotating fan delivers up to $2.5\text{ inwg}$ ($\approx 622\text{ Pa}$) static pressure and $28\text{ CFM}$ ($\approx 0.0132\text{ m}^3\text{/s}$) free airflow, providing over $40\times$ the static pressure head required for terminal float. When air travels through the annular gap surrounding the ball, flow velocity accelerates to:

$$v_{\text{annular}} = \frac{v_{\text{duct}}}{1 - 0.615} \approx 15.0\text{ m/s}$$

This comfortably exceeds the terminal float velocity ($\sim 8.5\text{--}9.5\text{ m/s}$), providing full dynamic control authority across the linear flight corridor.

---

## 3. Electrical Schematic & Wiring Matrix

All hardware sensors and motor drivers are wired exclusively to the **Plant Node (`dcs-plant-node`)**.

### 3.1 Power Infrastructure & Star-Ground Topology

The system uses a dual-voltage power architecture:
1.  **Logic Power ($5\text{V} / 3.3\text{V}$ DC)**: Supplied directly to the Raspberry Pi 3B+ via Micro-USB or GPIO 5V pins.
2.  **Actuator Power ($12\text{V}$ DC)**: Supplied by an external bench power supply to power the Nidec UltraFlo dual fan.

```
       +-------------------------------------------------------------------+
       |                    12V DC External Power Supply                   |
       +-----------------+---------------------------------+---------------+
                         | +12V DC                         | GND
                         |                                 |
                         +------------+                    |
                                      |                    |
                               +------v------+             |
                               | 1000uF Cap  |             |
                               | (Decoupling)|             |
                               +------+------+             |
                                      |                    |
                         +------------+                    |
                         | +12V DC                         |
                         |                                 |
                 +-------v-------+                         |
                 | Nidec Fan 12V |                         |
                 | (Red Wires)   |                         |
                 +---------------+                         |
                                                           |
   =================== COMMON STAR-GROUND BUS =====================
   |                                                              |
   +--> PSU 12V (-) Return                                        |
   +--> 1000uF Decoupling Capacitor (-) Terminal                  |
   +--> Raspberry Pi 3B+ GND (Pin 6 / Pin 9 / Pin 14)             |
   +--> EMC2101 GND (Pin 5)                                       |
   +--> HC-SR04 GND (Pin 4)                                       |
   +--> Voltage Divider Low-Side Resistor (2k Ohm)                |
   ================================================================
```

### 3.2 Pinout Interconnect Table

| Subsystem Component | Component Terminal | Raspberry Pi 3B+ Pin / Signal | Electrical Parameters |
| :--- | :--- | :--- | :--- |
| **EMC2101 Breakout** | VDD (Pin 1) | Pin 1 (3.3V Power) | $3.3\text{V}$ Logic Supply |
| **EMC2101 Breakout** | GND (Pin 5) | Pin 9 (GND) | Common Star Ground |
| **EMC2101 Breakout** | SMDATA (Pin 7) | Pin 3 (GPIO 2 / I2C1_SDA) | I2C Data Line (Address `0x4C`) |
| **EMC2101 Breakout** | SMCLK (Pin 8) | Pin 5 (GPIO 3 / I2C1_SCL) | I2C Clock Line ($100\text{ kHz}$) |
| **EMC2101 Breakout** | FAN (Pin 4) | Nidec Fan PWM (Blue/Green) | $1.4\text{ kHz}$ Open-Drain PWM Drive |
| **EMC2101 Breakout** | ALERT/TACH (Pin 6) | Nidec Fan TACH (Yellow/White)| Open-Collector Pulse (2 pulses/rev) |
| **Nidec UltraFlo Fan** | $12\text{V}$ Power (Red/Orange)| $12\text{V}$ DC PSU (+) Terminal | $12\text{V}$ DC, up to $1.32\text{ A}$ |
| **Nidec UltraFlo Fan** | Ground (Black/Gray) | Star-Ground Bus | Common Ground Return |
| **HC-SR04 Sensor** | VCC (Pin 1) | Pin 2 / Pin 4 (5V Power) | $5.0\text{V}$ Ultrasonic Transducer Power |
| **HC-SR04 Sensor** | GND (Pin 4) | Pin 6 / Pin 14 (GND) | Common Star Ground |
| **HC-SR04 Sensor** | TRIG (Pin 2) | Pin 16 (GPIO 23) | $3.3\text{V}$ Pulse Output ($10\,\mu\text{s}$) |
| **HC-SR04 Sensor** | ECHO (Pin 3) | Pin 18 (GPIO 24 via Divider) | $5\text{V} \to 3.3\text{V}$ Voltage Divider Input |

### 3.3 HC-SR04 Echo Voltage Divider Circuit

The HC-SR04 Echo pin outputs $5\text{V}$ TTL pulses, which will damage $3.3\text{V}$ tolerant Raspberry Pi GPIO pins if connected directly. A two-resistor voltage divider reduces the signal amplitude:

$$V_{\text{out}} = V_{\text{in}} \times \frac{R_2}{R_1 + R_2} = 5.0\text{V} \times \frac{2000\,\Omega}{1000\,\Omega + 2000\,\Omega} = 3.33\text{V}$$

```
   HC-SR04 ECHO (5V Output) ───[ R1: 1k Ohm ]───┬───> Raspberry Pi GPIO 24 (Pin 18)
                                                │
                                          [ R2: 2k Ohm ]
                                                │
                                                ▼
                                         Star Ground (GND)
```

---

## 4. Sensor Calibration & Acoustic Standoff

The HC-SR04 ultrasonic transducer is mounted at the top aperture of the acrylic tube, oriented downward along the column centerline.

### 4.1 Acoustic Standoff & Blind Zone Compensation

Ultrasonic sensors possess an inherent physical blind zone ($d < 2.0\text{ cm}$) where emitted $40\text{ kHz}$ burst pulses interfere with returning echoes, causing false readings or timeouts.

1.  **Physical Standoff Mounting**: Mount the HC-SR04 transducer face **$1.2\text{--}1.5\text{ cm}$ above the top physical retaining pins**. This ensures that when the ball is pinned against the ceiling ($PV = 100.0\%$), the distance measured by the sensor remains at $d \ge 2.7\text{ cm}$, remaining outside the transducer's blind zone.
2.  **Resting Datum Calibration ($d_{\max}$)**: With the ball resting on the bottom wire mesh, measure the distance from the sensor face to the top of the ball. The measured baseline distance must read:

$$d_{\max} \approx 42.0\text{--}42.6\text{ cm} \quad (\sigma < 0.5\text{ cm})$$

The software worker converts raw distance $d_{\text{cm}}$ to Process Variable $PV$ percentage using:

$$PV = \frac{d_{\max} - d_{\text{cm}}}{d_{\max} - d_{\min}} \times 100.0\%$$

When $d_{\text{cm}} = d_{\max}$, $PV \le 1.0\%$.

3.  **Ceiling Occlusion Handling**: When the ball reaches the top pins ($d_{\text{cm}} \le 3.5\text{ cm}$ or echo timeout occurs), the asynchronous `_sensor_worker` overrides the state to report **$PV = 100.0\%$**, preventing echo failure from registering as a false floor timeout ($PV = 0.0\%$).

---

## 5. Pre-Flight Hardware Verification

Perform these sequential verification steps on the Plant Node (`dcs-plant-node`) prior to initiating trial sweeps.

### 5.1 Step 1: I2C Bus Detection
Scan I2C Bus 1 to confirm communication with the EMC2101 fan controller:
```bash
sudo i2cdetect -y 1
```
* **Success Criterion**: Address `0x4C` appears in the matrix grid.

```
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:          -- -- -- -- -- -- -- -- -- -- -- -- -- 
10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 
20: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 
30: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 
40: -- -- -- -- -- -- -- -- -- -- 4C -- -- -- -- -- 
50: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 
60: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 
70: -- -- -- -- -- -- -- --                         
```

### 5.2 Step 2: Automated Diagnostic Test
Run the built-in hardware interface diagnostic routine:
```bash
sudo python3 src/resilient_pid/plant/plant_interface.py --mode hardware --tube-type acrylic --op-test --ignore-tach
```
* **Success Criterion**:
  * **PHASE 0**: Reports static baseline distance $d_{\max} \in [42.0, 42.6]\text{ cm}$ with $PV \le 1.0\%$.
  * **PHASE 1**: Pulses fan duty cycle from $0\% \to 40\% \to 0\%$, verifying ball lift and motor spooling.

### 5.3 Step 3: SMBus Register Verification
Verify that the EMC2101 driver is correctly configured in Direct PWM Setting Mode with spin-up override disabled:
```bash
# Read Fan Configuration Register (0x4A) - Bit 5 (0x20) must be SET
sudo i2cget -y 1 0x4c 0x4a

# Read Fan Spinup Register (0x4B) - Must read 0x00 (3.2s spinup disabled)
sudo i2cget -y 1 0x4c 0x4b

# Read Fan Setting Register (0x4C) - Must read 0x00 when actuator is stopped
sudo i2cget -y 1 0x4c 0x4c
```
* **Success Criterion**: Register `0x4A` returns `0x20` (or `0x21`), `0x4B` returns `0x00`, and `0x4C` returns `0x00` when idle.

---

## 6. Safety, Electrical Protection & Troubleshooting

### 6.1 Inductive Transient & Voltage Dip Protection
When the Nidec UltraFlo fan accelerates rapidly from $0\% \to 100\%$ PWM duty cycle, motor inrush currents can cause a momentary voltage dip on the $12\text{V}$ rail and introduce high-frequency inductive switching noise onto the common ground.
* **Mitigation**: A $1000\,\mu\text{F}$ electrolytic capacitor must be placed directly across the $12\text{V}$ and GND terminals at the power distribution block. All logic ground connections must tie back to a single star-ground point to prevent ground loops from resetting the EMC2101 or causing I2C bus lockups (`NACK` errors).

### 6.2 Preventing Ceiling Impeller Collision
At $19,200\text{ RPM}$, direct ball contact with the fan impellers will shatter the ping-pong ball and destroy the fan blades.
* **Safety Rules**:
  1.  **Top Retaining Pins**: Ensure crosshair wire retaining pins are installed $3.0\text{ cm}$ below the fan intake and sensor face.
  2.  **Bottom Mesh Stop**: Ensure a rigid wire mesh is secured above the fan discharge plenum to prevent the ball from dropping into the bottom impeller when power is removed.
  3.  **Software Spin-Up Override**: Always set EMC2101 Register `0x4B` (`REG_FAN_SPINUP`) to `0x00`. Leaving the default factory spin-up enabled causes the driver to execute a 3.2-second 100% duty cycle blast on power-up, launching the ball into the ceiling before the controller script initializes.
