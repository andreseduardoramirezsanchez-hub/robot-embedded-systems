# =============================================================
# PICO W (CÁMARA) – Detección de color + UART + WiFi (telemetría)
# =============================================================
from ov7670 import OV7670_30x40_RGB565 as CAM
import time, json, gc
import wifi, socketpool, board, busio, analogio, binascii
from adafruit_ov7670 import OV7670, OV7670_SIZE_DIV16, OV7670_COLOR_RGB
import adafruit_ssd1306

# =============================================================
# CONFIGURACIÓN DE RED
# =============================================================
SSID = "LUIS ALBERTO HENAO"
PASSWORD = "RAZAPURA"
BROKER_HOST = "192.168.1.7"
BROKER_PORT = 5051
ROBOT_PREFIX = "UDFJC/emb1/robot0"

print("Conectando WiFi...")
wifi.radio.connect(SSID, PASSWORD)
print("IP:", wifi.radio.ipv4_address)

pool = socketpool.SocketPool(wifi.radio)

# =============================================================
# UART para enviar datos de visión (TX=GP0, RX=GP1)
# =============================================================
uart = busio.UART(board.GP0, board.GP1, baudrate=115200)

# =============================================================
# DETECCIÓN DE COLOR (optimizada, con umbrales actualizables)
# =============================================================
class ColorDetector:
    # Umbrales iniciales (se actualizarán desde la web)
    RED_R_MIN, RED_G_MAX, RED_B_MAX = 10, 16, 13
    GREEN_G_MIN, GREEN_R_MAX, GREEN_B_MAX = 20, 9, 7
    BLUE_B_MIN, BLUE_R_MAX, BLUE_G_MAX = 8, 15, 18
    MIN_PIXELS_PER_COL = 8
    MIN_REGION_WIDTH   = 2

    def __init__(self, w=40, h=30):
        self.w, self.h = w, h

    def analyze(self, buf):
        w, h = self.w, self.h
        col_red, col_green, col_blue = [0]*w, [0]*w, [0]*w
        total_red = total_green = total_blue = 0

        rmin, gmax_r, bmax_r = self.RED_R_MIN, self.RED_G_MAX, self.RED_B_MAX
        gmin, rmax_g, bmax_g = self.GREEN_G_MIN, self.GREEN_R_MAX, self.GREEN_B_MAX
        bmin, rmax_b, gmax_b = self.BLUE_B_MIN, self.BLUE_R_MAX, self.BLUE_G_MAX

        idx, buf_len = 0, len(buf)
        for y in range(h):
            for x in range(w):
                if idx+1 >= buf_len: break
                pixel = (buf[idx] << 8) | buf[idx+1]
                idx += 2
                r, g, b = (pixel >> 11) & 0x1F, (pixel >> 5) & 0x3F, pixel & 0x1F

                if r >= rmin and g <= gmax_r and b <= bmax_r:
                    col_red[x] += 1; total_red += 1
                elif g >= gmin and r <= rmax_g and b <= bmax_g:
                    col_green[x] += 1; total_green += 1
                elif b >= bmin and r <= rmax_b and g <= gmax_b:
                    col_blue[x] += 1; total_blue += 1

        def mejor_region(col_count):
            mejor = None; mejor_ancho = 0; in_reg = False; start = 0
            for x in range(w):
                if col_count[x] >= self.MIN_PIXELS_PER_COL:
                    if not in_reg: in_reg = True; start = x
                else:
                    if in_reg:
                        in_reg = False
                        ancho = x - start
                        if ancho >= self.MIN_REGION_WIDTH and ancho > mejor_ancho:
                            mejor_ancho = ancho; mejor = (start, x-1)
            if in_reg and (w - start) >= self.MIN_REGION_WIDTH and (w - start) > mejor_ancho:
                mejor = (start, w-1)
            return mejor

        rr, gg, bb = mejor_region(col_red), mejor_region(col_green), mejor_region(col_blue)
        rc = (rr[0] + rr[1])/2 if rr else None
        gc_ = (gg[0] + gg[1])/2 if gg else None
        bc = (bb[0] + bb[1])/2 if bb else None

        return {
            "rp": total_red, "gp": total_green, "bp": total_blue,
            "rc": rc, "gc": gc_, "bc": bc
        }

# =============================================================
# TAREA DE CÁMARA Y ENVÍO UART + WiFi
# =============================================================
class CameraTask:
    WIDTH, HEIGHT = 40, 30
    def __init__(self):
        self.cam = CAM(
            d0_d7pinslist=[board.GP4, board.GP5, board.GP6, board.GP7, board.GP8, board.GP9, board.GP10, board.GP11],
            plk=board.GP12, xlk=board.GP13, sda=board.GP20, scl=board.GP21,
            hs=board.GP16, vs=board.GP17, ret=board.GP18, pwdn=board.GP19
        )
        self.buf = bytearray(2 * self.WIDTH * self.HEIGHT)
        self.cam.size = OV7670_SIZE_DIV16
        self.cam.colorspace = OV7670_COLOR_RGB
        self.detector = ColorDetector(self.WIDTH, self.HEIGHT)
        self.camera_enabled = True
        self.last_frame = 0
        self.sock = None   # se asignará después

    def capture_and_process(self):
        self.cam.capture(self.buf)
        frame_b64 = binascii.b2a_base64(self.buf).decode().strip()
        # Enviar por WiFi (para el dashboard)
        if self.sock:
            self.sock.send_json({
                "action": "PUB",
                "topic": ROBOT_PREFIX + "/camera/frame",
                "data": {"w": self.WIDTH, "h": self.HEIGHT, "frame": frame_b64}
            })
        # Analizar colores y enviar por UART
        vision = self.detector.analyze(self.buf)
        uart.write((json.dumps(vision) + "\n").encode())

# =============================================================
# CLIENTE WiFi SIMPLE
# =============================================================
class SimpleClient:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sock = None
    def connect(self):
        try:
            self.sock = pool.socket()
            self.sock.connect((self.host, self.port))
            self.sock.setblocking(False)
            print("Conectado al broker")
            return True
        except Exception as e:
            print("Error conexión:", e)
            return False
    def send_json(self, obj):
        try:
            data = (json.dumps(obj) + "\n").encode()
            self.sock.send(data)
        except:
            self.sock = None

# =============================================================
# INICIALIZACIÓN
# =============================================================
cam_task = CameraTask()
client = SimpleClient(BROKER_HOST, BROKER_PORT)
cam_task.sock = client

# OLED y baterías (se pueden mantener como antes, no incluyo todo por brevedad)
# ...

print("Sistema listo")
last_frame = 0
while True:
    now = time.monotonic()
    # Reconexión WiFi
    if client.sock is None:
        client.connect()
    # Captura y envío cada 50 ms (20 fps)
    if now - last_frame > 0.05:
        cam_task.capture_and_process()
        last_frame = now
        gc.collect()
    time.sleep(0.01)
