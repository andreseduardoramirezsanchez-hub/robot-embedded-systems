# =============================================================
# PICO 2W – BRAZO + CARRO + GUIADO AUTÓNOMO (UART + WiFi)
# =============================================================
import network, time, json, gc, math
import usocket as socket
from machine import Pin, PWM, UART

# =============================================================
# CONFIGURACIÓN
# =============================================================
WIFI_SSID = "LUIS ALBERTO HENAO"
WIFI_PASS = "RAZAPURA"
BROKER_HOST = "192.168.1.7"
BROKER_PORT = 5051
ROBOT_PREFIX = "UDFJC/emb1/robot0"

# UART para recibir visión de la cámara
uart = UART(0, baudrate=115200, tx=Pin(0), rx=Pin(1))

# =============================================================
# TASK BASE Y SCHEDULER
# =============================================================
class Task:
    def __init__(self, scheduler, period_ms, priority=1):
        self.period = period_ms
        self.priority = priority
        self.next_run = time.ticks_ms()
        scheduler.add(self)
    def update(self):
        pass

class Scheduler:
    def __init__(self):
        self.tasks = []
    def add(self, task):
        self.tasks.append(task)
        self.tasks.sort(key=lambda t: t.priority)
    def run(self):
        while True:
            now = time.ticks_ms()
            for task in self.tasks:
                if time.ticks_diff(now, task.next_run) >= 0:
                    try:
                        task.update()
                    except Exception as e:
                        print("ERROR en tarea:", e)
                    task.next_run = time.ticks_add(now, task.period)
            gc.collect()
            time.sleep_ms(1)

# =============================================================
# WIFI Y SOCKET
# =============================================================
class WiFiManager:
    def __init__(self, ssid, password):
        self.ssid, self.password = ssid, password
        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)
    def connect(self):
        print("Conectando WiFi...")
        if not self.wlan.isconnected():
            self.wlan.connect(self.ssid, self.password)
            timeout = 20
            while not self.wlan.isconnected() and timeout > 0:
                time.sleep(1)
                timeout -= 1
        if self.wlan.isconnected():
            print("WiFi OK. IP:", self.wlan.ifconfig()[0])
        else:
            raise RuntimeError("WiFi falló")

class SocketClient(Task):
    def __init__(self, host, port, scheduler, period_ms=50, on_connect=None):
        super().__init__(scheduler, period_ms, priority=0)
        self.host, self.port = host, port
        self.sock = None
        self.actions = {}
        self._rx_buffer = b""
        self.last_reconnect = 0
        self.on_connect = on_connect
    def connect(self):
        try:
            addr = socket.getaddrinfo(self.host, self.port)[0][-1]
            self.sock = socket.socket()
            self.sock.connect(addr)
            self.sock.setblocking(False)
            print("Broker OK")
            self.last_reconnect = time.ticks_ms()
            if self.on_connect:
                self.on_connect()
            return True
        except OSError as e:
            print("Error conexión:", e)
            self.sock = None
            return False
    def send(self, data): # ... (similar a código anterior) ...
    def send_json(self, obj): # ...
    def close(self): # ...
    def recv_json_nonblocking(self): # ...
    def update(self): # ...

class Node: # ... (PubSub, igual que antes, con resubscribe_all) ...

# =============================================================
# BRAZO (sin cambios)
# =============================================================
class ArmTask(Task):
    # ... (exactamente igual al código anterior, con RECOGER_SEQUENCE) ...

# =============================================================
# CARRO DIFERENCIAL + GUIADO
# =============================================================
class CarTask(Task):
    WHEEL_BASE = 0.12
    MAX_DUTY   = 65535

    def __init__(self, scheduler, pubsub, led, period_ms=20):
        super().__init__(scheduler, period_ms, priority=2)
        self.pubsub, self.led = pubsub, led
        # Pines motores
        self.in1_l, self.in2_l = Pin(9, Pin.OUT), Pin(8, Pin.OUT)
        self.in1_r, self.in2_r = Pin(1, Pin.OUT), Pin(0, Pin.OUT)
        self.ena, self.enb = PWM(Pin(10), freq=1000), PWM(Pin(11), freq=1000)
        self._stop()
        self.target_v = 0.0
        self.target_w = 0.0
        # Odometría
        self.x, self.y, self.theta = 0.0, 0.0, 0.0
        self.last_time_us = time.ticks_us()
        self.last_pose_pub = 0
        # Parámetros de guiado
        self.forward_speed = 0.12
        self.turn_speed = 0.8
        self.rodeo_amp = 1.0
        self.recog_thresh = 300
        self.motor_balance = 1.0   # >1 favorece rueda derecha, <1 izquierda
        self.auto_mode = False
        # Visión
        self.vision_buffer = b""
        self.latest_vision = None
        # Memoria de guiado
        self.last_seen_side = {"red": None, "green": None, "blue": None}
        self.searching = False
        self.is_waiting = False
        self.last_turn_action = 0
        self.TURN_DURATION = 400   # ms
        self.WAIT_DURATION = 300   # ms
        pubsub.subscribe("guidance/cmd", self._handle_guidance_cmd)
        pubsub.subscribe("car/cmd", self._handle_car_cmd)

    def _stop(self):
        self.in1_l.value(0); self.in2_l.value(0)
        self.in1_r.value(0); self.in2_r.value(0)
        self.ena.duty_u16(0); self.enb.duty_u16(0)

    def _set_motors(self, duty_l, duty_r):
        # Aplicar balance
        if duty_l != 0:
            duty_l = int(duty_l * self.motor_balance)
        if duty_r != 0:
            duty_r = int(duty_r / self.motor_balance)
        # Saturaciones
        duty_l = max(-self.MAX_DUTY, min(self.MAX_DUTY, duty_l))
        duty_r = max(-self.MAX_DUTY, min(self.MAX_DUTY, duty_r))
        if duty_l >= 0:
            self.in1_l.value(1); self.in2_l.value(0)
        else:
            self.in1_l.value(0); self.in2_l.value(1)
        if duty_r >= 0:
            self.in1_r.value(1); self.in2_r.value(0)
        else:
            self.in1_r.value(0); self.in2_r.value(1)
        self.ena.duty_u16(abs(duty_l))
        self.enb.duty_u16(abs(duty_r))

    def _velocity_to_duty(self, v):
        return int(v * self.MAX_DUTY)

    def update(self):
        now_us = time.ticks_us()
        dt = time.ticks_diff(now_us, self.last_time_us) / 1_000_000.0
        self.last_time_us = now_us

        # Leer UART
        while uart.any():
            self.vision_buffer += uart.read(uart.any())
        while b"\n" in self.vision_buffer:
            line, self.vision_buffer = self.vision_buffer.split(b"\n", 1)
            try:
                self.latest_vision = json.loads(line)
            except:
                pass

        # Ejecutar guiado si está en modo automático
        if self.auto_mode and self.latest_vision:
            self._guidance_step()

        # Calcular velocidades y mover motores
        if dt > 0:
            v = self.target_v
            w = self.target_w
            # Odometría (simplificada)
            if abs(w) < 1e-6:
                self.x += v * dt * math.cos(self.theta)
                self.y += v * dt * math.sin(self.theta)
            else:
                radio = v / w
                theta_delta = w * dt
                self.theta += theta_delta
                self.x += radio * (math.sin(self.theta) - math.sin(self.theta - theta_delta))
                self.y -= radio * (math.cos(self.theta) - math.cos(self.theta - theta_delta))
            v_left  = v - w * self.WHEEL_BASE
            v_right = v + w * self.WHEEL_BASE
            duty_l = self._velocity_to_duty(v_left)
            duty_r = self._velocity_to_duty(v_right)
            self._set_motors(duty_l, duty_r)

        # Publicar pose cada 100 ms
        now_ms = time.ticks_ms()
        if time.ticks_diff(now_ms, self.last_pose_pub) > 100:
            self.last_pose_pub = now_ms
            self.pubsub.publish("car/pose", {"x": self.x, "y": self.y, "theta": self.theta})

    def _guidance_step(self):
        """Lógica de guiado idéntica a la del HTML, pero ejecutada aquí."""
        data = self.latest_vision
        rc, gc, bc = data.get("rc"), data.get("gc"), data.get("bc")
        bp = data.get("bp", 0)

        # Actualizar memoria de lados vistos
        def update_memory(color, center, w=40):
            if center is not None:
                if center < w/3: self.last_seen_side[color] = "left"
                elif center > 2*w/3: self.last_seen_side[color] = "right"
                else: self.last_seen_side[color] = "center"
        update_memory("red", rc)
        update_memory("green", gc)
        update_memory("blue", bc)

        # Control intermitente
        now = time.ticks_ms()
        if self.is_waiting:
            if time.ticks_diff(now, self.last_turn_action) < self.WAIT_DURATION:
                return
            self.is_waiting = False
            self.last_turn_action = now

        all_visible = rc is not None and gc is not None and bc is not None
        any_visible = rc is not None or gc is not None or bc is not None

        if all_visible:
            # Ordenar centros
            sorted_colors = sorted([("red", rc), ("green", gc), ("blue", bc)], key=lambda x: x[1])
            order = "-".join([c[0] for c in sorted_colors])
            if order == "red-green-blue":
                self.target_v = 0; self.target_w = 0
                self.auto_mode = False
                print("¡Frente a la estiba!")
                return
            elif order == "blue-green-red":
                self.target_v = self.forward_speed * 0.8
                self.target_w = self.turn_speed * self.rodeo_amp
            else:
                if gc > rc and gc > bc:
                    self.target_v = self.forward_speed * 0.8
                    self.target_w = -self.turn_speed * self.rodeo_amp
                elif gc < rc and gc < bc:
                    self.target_v = self.forward_speed * 0.8
                    self.target_w = self.turn_speed * self.rodeo_amp
                else:
                    error = gc - 20
                    self.target_v = self.forward_speed * 0.5
                    self.target_w = (self.turn_speed * self.rodeo_amp) if error > 0 else (-self.turn_speed * self.rodeo_amp)
        elif any_visible:
            missing = "red" if rc is None else ("green" if gc is None else "blue")
            side = self.last_seen_side.get(missing)
            if side == "left":
                self.target_v = 0; self.target_w = self.turn_speed
            elif side == "right":
                self.target_v = 0; self.target_w = -self.turn_speed
            else:
                self.target_v = 0; self.target_w = self.turn_speed
        else:
            self.target_v = 0; self.target_w = self.turn_speed * 0.6

        # Programar pausa
        self.is_waiting = True
        self.last_turn_action = now
        # Breve parada para imagen clara
        time.sleep_ms(int(self.TURN_DURATION * 0.8))
        self.target_v = 0; self.target_w = 0

    def _handle_guidance_cmd(self, msg):
        action = msg.get("action")
        if action == "set_params":
            self.forward_speed = float(msg.get("forward", self.forward_speed))
            self.turn_speed = float(msg.get("turn", self.turn_speed))
            self.rodeo_amp = float(msg.get("amp", self.rodeo_amp))
            self.recog_thresh = int(msg.get("recog", self.recog_thresh))
            self.motor_balance = float(msg.get("balance", self.motor_balance))
        elif action == "auto":
            self.auto_mode = bool(msg.get("enable", False))

    def _handle_car_cmd(self, msg):
        # Comandos manuales (desde el dashboard, solo si no está en auto)
        if self.auto_mode:
            return
        action = msg.get("action")
        if action == "set_velocity":
            self.target_v = float(msg.get("v", 0))
            self.target_w = float(msg.get("w", 0))
        elif action == "stop":
            self.target_v = 0; self.target_w = 0
            self._stop()

# =============================================================
# MAIN APP
# =============================================================
class MainApp:
    def __init__(self):
        self.scheduler = Scheduler()
        self.wifi = WiFiManager(WIFI_SSID, WIFI_PASS)
        self.socket = SocketClient(BROKER_HOST, BROKER_PORT, self.scheduler,
                                   on_connect=lambda: self.pubsub.resubscribe_all())
        self.pubsub = Node(self.socket, prefix=ROBOT_PREFIX+"/")
        self.led = LedIndicator()
        self.arm = ArmTask(self.scheduler, self.pubsub, self.led)
        self.car = CarTask(self.scheduler, self.pubsub, self.led)
        # Watchdog y heartbeat (opcional)
    def run(self):
        self.wifi.connect()
        self.socket.connect()
        print("Carro listo")
        self.scheduler.run()

app = MainApp()
app.run()
