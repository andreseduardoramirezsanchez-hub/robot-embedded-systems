#  Autonomous Robotic System — Embedded Systems Final Project

## System Description

This project implements an autonomous robotic system consisting of:
- A **differential drive robot** (car) with PWM motor control
- A **robotic arm** (MeArm) with 3 servo joints and inverse kinematics
- A **distributed PubSub communication architecture** via a central broker
- A **web-based control interface** (Digital Twin)

All components run concurrently using a cooperative task scheduler on a **Raspberry Pi Pico 2W** running MicroPython.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        BROKER (PC)                          │
│                    Simple_server.py                         │
│                                                             │
│   ┌──────────────────┐       ┌──────────────────────────┐   │
│   │  TCP Server      │       │   WebSocket Server       │   │
│   │  Port 5051       │       │   Port 5052              │   │
│   └────────┬─────────┘       └────────────┬─────────────┘   │
│            │         PubSub Bus           │                 │
└────────────┼─────────────────────────────┼─────────────────┘
             │ TCP                          │ WebSocket
             │                             │
┌────────────▼──────────┐      ┌───────────▼──────────────┐
│   ROBOT (Pico 2W)     │      │   FRONTEND (Browser)     │
│   main_robot.py       │      │   Robot.html             │
│                       │      │                          │
│  Scheduler            │      │  PubSub (JS class)       │
│  ├── SocketClient     │      │  ├── sendCar()           │
│  ├── Node (PubSub)    │      │  ├── sendArm()           │
│  ├── ArmTask          │      │  └── state display       │
│  ├── CarTask          │      │                          │
│  └── WatchdogTask     │      └──────────────────────────┘
└───────────────────────┘
```

---

## Topic Structure (PubSub)

All topics use the namespace prefix: `UDFJC/emb1/robot{id}/`

| Topic | Direction | Payload | Description |
|-------|-----------|---------|-------------|
| `UDFJC/emb1/robot0/car/cmd` | Frontend → Robot | `{action, col}` | Move car: recto, izquierda, derecha, stop |
| `UDFJC/emb1/robot0/car/state` | Robot → Frontend | `{moving, direction}` | Current car state |
| `UDFJC/emb1/robot0/arm/cmd` | Frontend → Robot | `{action, size?}` | Arm command: inicio, rombo |
| `UDFJC/emb1/robot0/arm/state` | Robot → Frontend | `{base, hombro, codo, busy}` | Current arm angles |
| `UDFJC/emb1/robot0/debug/watchdog` | Robot → All | `{msg: "alive"}` | Periodic heartbeat |

---

## Project Structure

```
/
├── main_robot.py         # MicroPython code for Pico 2W (robot)
├── Robot_control.py      # Alternative robot code with auto-reconnect
├── Simple_server.py      # Broker server (TCP + WebSocket)
├── Robot.html            # Web frontend / Digital Twin
└── README.md
```

---

## How to Run

### 1. Start the Broker (on your PC)

```bash
pip install websockets
python Simple_server.py
```

The broker will listen on:
- TCP port `5051` (for the robot)
- WebSocket port `5052` (for the browser)

### 2. Flash the Robot (Pico 2W)

1. Edit `main_robot.py` and set your WiFi credentials and broker IP:
```python
WIFI_SSID = "YOUR_SSID"
BROKER_HOST = "192.168.X.X"   # Your PC's IP address
BROKER_PORT = 5051
```
2. Upload `main_robot.py` to the Pico 2W using Thonny or `mpremote`.
3. The robot will connect to WiFi and then to the broker automatically.

### 3. Open the Frontend

1. Open `Robot.html` in a browser (Chrome recommended).
2. Enter the broker's IP and port `5052`.
3. Set the Robot ID (default: `0`).
4. Click **Conectar**.

You can now control the car and arm in real time.

---

## Key Design Decisions

- **Cooperative Scheduler**: The Pico 2W runs a priority-based `Scheduler` that executes `Task` objects in a non-blocking loop, avoiding the need for RTOS threads.
- **PubSub Decoupling**: The robot, broker, and frontend never communicate directly with each other — all communication goes through named topics, making the system modular and extensible.
- **Namespace isolation**: The prefix `UDFJC/emb1/robot{id}/` allows multiple robots to share the same broker without topic collision.
- **Auto-reconnect**: `Robot_control.py` includes automatic reconnection logic if the broker connection drops.
