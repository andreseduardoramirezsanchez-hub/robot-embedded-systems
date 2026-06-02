# =============================================================
# PICO 2W — BRAZO + CARRO DIFERENCIAL
# Control exclusivamente por WiFi/PubSub desde el frontend
# Topicos suscritos:
#   arm/cmd  <- {action:"rombo", size:N} | {action:"inicio"}
#   car/cmd  <- {action:"recto"|"izquierda"|"derecha", col:"col1"|"col2"|"col3"}
# Topicos publicados:
#   arm/state -> {base, hombro, codo, busy}
#   car/state -> {moving, direction}
#   debug/watchdog -> heartbeat
# =============================================================

import network, time, json, gc, math
import usocket as socket
from machine import Pin, PWM


# =============================================================
# SCHEDULER + TASK BASE
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
                    task.update()
                    task.next_run = time.ticks_add(now, task.period)
            gc.collect()
            time.sleep_ms(1)


# =============================================================
# WIFI
# =============================================================

class WiFiManager:
    def __init__(self, ssid, password_file=".env"):
        self.ssid = ssid
        with open(password_file) as f:
            self.password = f.read().strip()
        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)

    def connect(self):
        print("Conectando WiFi...")
        if not self.wlan.isconnected():
            self.wlan.connect(self.ssid, self.password)
            while not self.wlan.isconnected():
                time.sleep(1)
                print("status:", self.wlan.status())
        print("WiFi OK. IP:", self.wlan.ifconfig()[0])


# =============================================================
# SOCKET CLIENT
# =============================================================

class SocketClient(Task):
    def __init__(self, host, port, scheduler, period_ms=50):
        super().__init__(scheduler, period_ms, priority=0)
        self.host = host
        self.port = port
        self.sock = None
        self.actions = {}
        self._rx_buffer = b""

    def connect(self):
        print("Conectando al broker...")
        addr = socket.getaddrinfo(self.host, self.port)[0][-1]
        self.sock = socket.socket()
        self.sock.connect(addr)
        self.sock.setblocking(False)
        print("Broker OK")

    def ensure(self):
        if self.sock is None:
            self.connect()

    def send(self, data):
        total = 0
        while total < len(data):
            try:
                sent = self.sock.send(data[total:])
                if sent == 0:
                    return False
                total += sent
            except OSError:
                return False
        return True

    def send_json(self, obj):
        self.send((json.dumps(obj) + "\n").encode())

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except:
            pass
        self.sock = None

    def recv_json_nonblocking(self):
        messages = []
        try:
            data = self.sock.recv(1024)
            if data == b'':
                self.close()
                return []
            if data:
                self._rx_buffer += data
        except OSError:
            return []
        while b"\n" in self._rx_buffer:
            line, self._rx_buffer = self._rx_buffer.split(b"\n", 1)
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except:
                pass
        return messages

    def update(self):
        if self.sock is None:
            return
        msgs = self.recv_json_nonblocking()
        for msg in msgs:
            action = msg.get("action")
            if action in self.actions:
                self.actions[action](msg)

    def add_action(self, action, callback):
        self.actions[action] = callback


# =============================================================
# PUBSUB NODE
# =============================================================

class Node:
    def __init__(self, socket_client, prefix='UDFJC/emb1/robot0/'):
        self.sock = socket_client
        self.sock.add_action("PUB", self.handle_pub)
        self.sock.add_action("SUB", self.handle_sub)
        self.prefix = prefix
        self.subscriptions = {}

    def publish(self, topic, data):
        self.broker_publish(topic, data)
        self.local_publish(topic, data)

    def broker_publish(self, topic, data):
        self.sock.ensure()
        pkt = {"action": "PUB", "topic": self.prefix + topic, "data": data}
        self.sock.send_json(pkt)

    def local_publish(self, topic, data):
        for c in list(self.subscriptions.get(topic, set())):
            try:
                c(data)
            except:
                self.subscriptions[topic].discard(c)

    def subscribe(self, topic, callback):
        self.subscriptions.setdefault(topic, set()).add(callback)
        self.sock.ensure()
        self.sock.send_json({"action": "SUB", "topic": self.prefix + topic})
        print(f"[SUB] {topic}")

    def handle_pub(self, msg):
        topic = msg['topic']
        if not topic.startswith(self.prefix):
            return
        self.local_publish(topic[len(self.prefix):], msg['data'])

    def handle_sub(self, msg):
        pass


# =============================================================
# LED INDICADOR
# ON = standby | OFF = ejecutando
# =============================================================

class LedIndicator:
    def __init__(self):
        self.led = Pin("LED", Pin.OUT)
        self.led.off()

    def standby(self):
        self.led.on()

    def busy(self):
        self.led.off()


# =============================================================
# BRAZO — Task con maquina de estados (no bloqueante)
# =============================================================

class ArmTask(Task):
    L1     = 80.0
    L2     = 145.0
    H_BASE = 30.0
    L3     = 15.0
    LIMITS = {'base': (10, 170), 'hombro': (35, 165), 'codo': (90, 180)}
    INICIAL = {'base': 90.0, 'hombro': 90.0, 'codo': 180.0}
    X_TABLERO = 185
    X_SEGURO  = 155
    Z_CENTRO  = 90
    Y_CENTRO  = 0

    def __init__(self, scheduler, pubsub, led, period_ms=20):
        super().__init__(scheduler, period_ms, priority=2)
        self.pubsub = pubsub
        self.led    = led

        self.servos = {
            'base':   PWM(Pin(16), freq=50),
            'hombro': PWM(Pin(17), freq=50),
            'codo':   PWM(Pin(18), freq=50)
        }
        self.estado = self.INICIAL.copy()

        # Maquina de estados
        self._busy       = False
        self._queue      = []
        self._seg        = None
        self._seg_step   = 0
        self._seg_total  = 0
        self._seg_delay  = 20
        self._last_ms    = 0

        pubsub.subscribe("arm/cmd", self._handle_cmd)
        self._publish_state()

    # ---- IK ----
    def _ik(self, x, y, z):
        b_rad = math.atan2(y, x)
        b = 90 + math.degrees(b_rad)
        r = math.sqrt(x**2 + y**2)
        dx = r - self.L3
        dz = z - self.H_BASE
        D2 = dx**2 + dz**2
        D  = math.sqrt(D2)
        if D > (self.L1 + self.L2) or D < abs(self.L1 - self.L2):
            return None, None, None
        cos_th2 = max(-1.0, min(1.0, (D2 - self.L1**2 - self.L2**2) / (2*self.L1*self.L2)))
        th2 = math.acos(cos_th2)
        th1 = math.atan2(dx, dz) - math.atan2(self.L2*math.sin(th2), self.L1+self.L2*math.cos(th2))
        return 90 + math.degrees(b_rad), math.degrees(th1)+90, math.degrees(th2)+90

    def _valido(self, b, h, c):
        if b is None:
            return False
        return (self.LIMITS['base'][0]   <= b <= self.LIMITS['base'][1] and
                self.LIMITS['hombro'][0] <= h <= self.LIMITS['hombro'][1] and
                self.LIMITS['codo'][0]   <= c <= self.LIMITS['codo'][1])

    def _mover(self, nombre, angulo):
        lo, hi = self.LIMITS[nombre]
        ang  = max(lo, min(hi, angulo))
        duty = int((ang / 180 * 6554) + 1638)
        self.servos[nombre].duty_u16(duty)
        return ang

    # ---- Cola de segmentos ----
    def _seg_cartesiano(self, x0,y0,z0, x1,y1,z1, pasos=60, delay_ms=20):
        self._queue.append(('cart', x0,y0,z0, x1,y1,z1, pasos, delay_ms))

    def _seg_angular(self, b,h,c, pasos=80, delay_ms=20):
        self._queue.append(('ang', b,h,c, pasos, delay_ms))

    def _encolar_rombo(self, lado_mm):
        d  = lado_mm
        xT = self.X_TABLERO
        xS = self.X_SEGURO
        yC = self.Y_CENTRO
        zC = self.Z_CENTRO
        va = (yC,      zC + d)
        vd = (yC + d,  zC)
        vb = (yC,      zC - d)
        vi = (yC - d,  zC)
        pl = max(12, int(lado_mm * 1.2))

        self._seg_cartesiano(160,0,110,       xS,va[0],va[1], 80,20)
        self._seg_cartesiano(xS,va[0],va[1],  xT,va[0],va[1], 30,20)
        self._seg_cartesiano(xT,va[0],va[1],  xT,vd[0],vd[1], pl,20)
        self._seg_cartesiano(xT,vd[0],vd[1],  xT,vb[0],vb[1], pl,20)
        self._seg_cartesiano(xT,vb[0],vb[1],  xT,vi[0],vi[1], pl,20)
        self._seg_cartesiano(xT,vi[0],vi[1],  xT,va[0],va[1], pl,20)
        self._seg_cartesiano(xT,va[0],va[1],  xS,va[0],va[1], 30,20)
        self._seg_angular(self.INICIAL['base'], self.INICIAL['hombro'],
                          self.INICIAL['codo'], 120, 20)

    # ---- Maquina de estados ----
    def update(self):
        if not self._busy:
            if self._queue:
                self._busy = True
                self.led.busy()
                self._load_next()
            return

        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_ms) < self._seg_delay:
            return
        self._last_ms = now

        if self._seg_step >= self._seg_total:
            if self._queue:
                self._load_next()
            else:
                self._busy = False
                self.led.standby()
                self._publish_state()
            return

        self._step()
        self._seg_step += 1

    def _load_next(self):
        s = self._queue.pop(0)
        if s[0] == 'ang':
            _, b, h, c, pasos, delay = s
            self._seg = ('ang', b, h, c,
                         self.estado['base'],
                         self.estado['hombro'],
                         self.estado['codo'])
            self._seg_total = pasos
            self._seg_delay = delay
        else:
            _, x0,y0,z0, x1,y1,z1, pasos, delay = s
            self._seg = ('cart', x0,y0,z0, x1,y1,z1)
            self._seg_total = pasos
            self._seg_delay = delay
        self._seg_step = 0

    def _step(self):
        f = (self._seg_step + 1) / self._seg_total
        if self._seg[0] == 'ang':
            _, bd,hd,cd, b0,h0,c0 = self._seg
            self.estado['base']   = self._mover('base',   b0+(bd-b0)*f)
            self.estado['hombro'] = self._mover('hombro', h0+(hd-h0)*f)
            self.estado['codo']   = self._mover('codo',   c0+(cd-c0)*f)
        else:
            _, x0,y0,z0, x1,y1,z1 = self._seg
            b,h,c = self._ik(x0+(x1-x0)*f, y0+(y1-y0)*f, z0+(z1-z0)*f)
            if self._valido(b,h,c):
                self.estado['base']   = self._mover('base',   b)
                self.estado['hombro'] = self._mover('hombro', h)
                self.estado['codo']   = self._mover('codo',   c)

    def _publish_state(self):
        self.pubsub.publish("arm/state", {
            "base":   round(self.estado['base'],   1),
            "hombro": round(self.estado['hombro'], 1),
            "codo":   round(self.estado['codo'],   1),
            "busy":   self._busy
        })

    def _handle_cmd(self, msg):
        if self._busy:
            return
        action = msg.get("action")
        if action == "rombo":
            self._encolar_rombo(msg.get("size", 20))
        elif action == "inicio":
            self._seg_angular(self.INICIAL['base'],
                              self.INICIAL['hombro'],
                              self.INICIAL['codo'], 120, 20)
            self._busy = True
            self.led.busy()
            self._load_next()


# =============================================================
# CARRO — Task con maquina de estados (no bloqueante)
# =============================================================

class CarTask(Task):
    DUTY_MAX       = 65535
    TIEMPOS_RECTA  = {'col1': 0.60, 'col2': 1.20, 'col3': 1.80}
    SEMICIRCULOS   = {
        'col1': (1.206, 36863),
        'col2': (2.149, 49438),
        'col3': (3.091, 54346),
    }

    def __init__(self, scheduler, pubsub, led, period_ms=20):
        super().__init__(scheduler, period_ms, priority=2)
        self.pubsub = pubsub
        self.led    = led

        self.in1_l = Pin(8,  Pin.OUT)
        self.in2_l = Pin(9,  Pin.OUT)
        self.in1_r = Pin(4,  Pin.OUT)
        self.in2_r = Pin(5,  Pin.OUT)
        self.ena   = PWM(Pin(10), freq=1000)
        self.enb   = PWM(Pin(11), freq=1000)
        self._stop()

        self._moving   = False
        self._end_ms   = 0
        self._direction = "stop"

        pubsub.subscribe("car/cmd", self._handle_cmd)

    def _stop(self):
        self.in1_l.value(0); self.in2_l.value(0)
        self.in1_r.value(0); self.in2_r.value(0)
        self.ena.duty_u16(0); self.enb.duty_u16(0)

    def _set(self, duty_l, duty_r):
        self.in1_l.value(1); self.in2_l.value(0)
        self.in1_r.value(1); self.in2_r.value(0)
        self.ena.duty_u16(duty_l)
        self.enb.duty_u16(duty_r)

    def _start(self, direccion, col):
        if self._moving:
            return
        self._moving    = True
        self._direction = direccion
        self.led.busy()

        if direccion == 'recto':
            t = self.TIEMPOS_RECTA[col]
            self._set(self.DUTY_MAX, self.DUTY_MAX)
        elif direccion == 'izquierda':
            t, di = self.SEMICIRCULOS[col]
            self._set(di, self.DUTY_MAX)
        elif direccion == 'derecha':
            t, di = self.SEMICIRCULOS[col]
            self._set(self.DUTY_MAX, di)
        else:
            t = 0

        self._end_ms = time.ticks_add(time.ticks_ms(), int(t * 1000))
        print(f"[CAR] {direccion} {col} {t:.2f}s")

    def update(self):
        if not self._moving:
            return
        if time.ticks_diff(time.ticks_ms(), self._end_ms) >= 0:
            self._stop()
            self._moving    = False
            self._direction = "stop"
            self.led.standby()
            self.pubsub.publish("car/state", {"moving": False, "direction": "stop"})

    def _handle_cmd(self, msg):
        if not self._moving:
            self._start(msg.get("action", ""), msg.get("col", "col1"))


# =============================================================
# WATCHDOG
# =============================================================

class WatchdogTask(Task):
    def __init__(self, scheduler, pubsub, period_ms=60000):
        super().__init__(scheduler, period_ms, priority=5)
        self.pubsub = pubsub

    def update(self):
        self.pubsub.publish("debug/watchdog", {"msg": "alive"})
        print("Watchdog alive")


# =============================================================
# MAIN APP — PICO 2W
# =============================================================

class MainApp:
    def __init__(self):
        self.scheduler = Scheduler()
        self.led       = LedIndicator()
        self.wifi      = WiFiManager("TU_SSID")   # <- cambiar
        self.socket    = SocketClient(
            host="192.168.1.17",                   # <- IP del broker
            port=5051,
            scheduler=self.scheduler
        )
        self.pubsub  = Node(self.socket, prefix='UDFJC/emb1/robot0/')
        self.arm     = ArmTask(self.scheduler, self.pubsub, self.led)
        self.car     = CarTask(self.scheduler, self.pubsub, self.led)
        self.watchdog= WatchdogTask(self.scheduler, self.pubsub)

    def run(self):
        self.wifi.connect()
        self.socket.connect()
        self.led.standby()
        print("Pico 2W listo. Scheduler corriendo...")
        self.scheduler.run()


app = MainApp()
app.run()
