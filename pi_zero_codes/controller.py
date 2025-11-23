rami@catdoorpi:~/catdoor-api $ cat go.mod 
module catdoor-api

go 1.24.4
rami@catdoorpi:~/catdoor-api $ cd ../
rami@catdoorpi:~ $ cat controller.py 
#!/usr/bin/env python3
import socket
import threading
import time
from gpiozero import LED, AngularServo, Device
from gpiozero.pins.pigpio import PiGPIOFactory
import pigpio


# ==== PINS ====
RED_PIN   = 17   # pin 11
GRN_PIN   = 27   # pin 13
YEL_PIN   = 22   # pin 15
SERVO_PIN = 18   # pin 12 (PWM)
IR_PIN    = 23   # pin 16 (IR module 'Y' pin)


# ==== Your NEC IR codes ====
IR_CODE_1   = 0xF30CFF00  # "1" -> GREEN
IR_CODE_2   = 0xE718FF00  # "2" -> YELLOW
IR_CODE_3   = 0xA15EFF00  # "3" -> RED
NEC_REPEAT  = 0xFFFFFFFF


# ==== Setup gpiozero via pigpio (stable PWM) ====
Device.pin_factory = PiGPIOFactory()

red = LED(RED_PIN)
grn = LED(GRN_PIN)
yel = LED(YEL_PIN)

servo = AngularServo(
    SERVO_PIN,
    min_angle=0,
    max_angle=90,
    min_pulse_width=0.0005,
    max_pulse_width=0.0025,
)


# ==== Shared state ====
modes = ["GREEN", "YELLOW", "RED"]
mode_lock = threading.Lock()
current_mode = "GREEN"


def stop_all_leds():
    for led in (red, grn, yel):
        led.source = None
        led.off()


def apply_mode(name: str):
    global current_mode
    with mode_lock:
        current_mode = name

    stop_all_leds()

    if name == "RED":
        servo.angle = 90
        red.on()
    elif name == "YELLOW":
        servo.angle = 0
        yel.on()
    elif name == "GREEN":
        servo.angle = 45
        grn.on()
    else:
        # unknown mode -> do nothing special
        pass

    print(f"[MODE] {current_mode}")


def mode_green():
    apply_mode("GREEN")


def mode_yellow():
    apply_mode("YELLOW")


def mode_red():
    apply_mode("RED")


# ---- Minimal NEC decoder (timing-based) ----
LEADER_MARK   = 9000
LEADER_SPACE  = 4500
REPEAT_SPACE  = 2250
BIT_MARK      = 560
ZERO_SPACE    = 560
ONE_SPACE     = 1690
TOL = 0.35


def in_range(v, tgt):
    return tgt * (1 - TOL) <= v <= tgt * (1 + TOL)


class NecReceiver:
    def __init__(self, pi, gpio, on_code):
        self.pi = pi
        self.gpio = gpio
        self.on_code = on_code
        self.last_tick = None
        self.durs = []

        pi.set_mode(gpio, pigpio.INPUT)
        pi.set_pull_up_down(gpio, pigpio.PUD_UP)
        pi.set_glitch_filter(gpio, 100)  # ignore pulses <100µs

        self.cb = pi.callback(gpio, pigpio.EITHER_EDGE, self._cb)

    def _cb(self, gpio, level, tick):
        if self.last_tick is None:
            self.last_tick = tick
            return

        dt = pigpio.tickDiff(self.last_tick, tick)
        self.last_tick = tick
        self.durs.append(dt)

        # ~30ms gap → frame ended
        if dt > 30000:
            code = self._decode(self.durs)
            self.durs.clear()
            if code is not None:
                self.on_code(code)

    def _decode(self, durs):
        if len(durs) < 2:
            return None

        start = 0

        # Leader detection (normal or repeat)
        leader_ok = (
            in_range(durs[0], LEADER_MARK)
            and (in_range(durs[1], LEADER_SPACE) or in_range(durs[1], REPEAT_SPACE))
        )

        # Sometimes the first duration can be noise; try shifted by 1
        if not leader_ok and len(durs) >= 3:
            leader_ok = (
                in_range(durs[1], LEADER_MARK)
                and (in_range(durs[2], LEADER_SPACE) or in_range(durs[2], REPEAT_SPACE))
            )
            if leader_ok:
                start = 1

        if not leader_ok:
            return None

        # Handle NEC repeat frame
        if in_range(durs[start + 1], REPEAT_SPACE):
            return NEC_REPEAT

        # Decode 32 data bits
        bits = []
        i = start + 2
        while i + 1 < len(durs) and len(bits) < 32:
            mark = durs[i]
            space = durs[i + 1]

            if not in_range(mark, BIT_MARK):
                return None

            if in_range(space, ZERO_SPACE):
                bits.append(0)
            elif in_range(space, ONE_SPACE):
                bits.append(1)
            else:
                return None

            i += 2

        if len(bits) != 32:
            return None

        # NEC transmits LSB first
        val = 0
        for idx, b in enumerate(bits):
            val |= (b << idx)

        return val

    def cancel(self):
        self.cb.cancel()


last_code = None


def on_ir_code(code):
    global last_code

    if code == NEC_REPEAT:
        return

    last_code = code
    # print(f"[IR] 0x{code:08X}")

    if code == IR_CODE_1:
        mode_green()
    elif code == IR_CODE_2:
        mode_yellow()
    elif code == IR_CODE_3:
        mode_red()
    else:
        print(f"[IR] Unknown 0x{code:08X}")


# ---- TCP control server (localhost:8765) ----
def handle_client(conn, addr):
    try:
        data = conn.recv(1024).decode("utf-8").strip().upper()

        if data == "GREEN":
            mode_green()
            conn.sendall(b"OK GREEN\n")
        elif data == "YELLOW":
            mode_yellow()
            conn.sendall(b"OK YELLOW\n")
        elif data == "RED":
            mode_red()
            conn.sendall(b"OK RED\n")
        elif data == "STATUS":
            with mode_lock:
                conn.sendall(f"MODE {current_mode}\n".encode("utf-8"))
        else:
            conn.sendall(b"ERR UNKNOWN\n")

    except Exception as e:
        try:
            conn.sendall(f"ERR {e}\n".encode("utf-8"))
        except Exception:
            pass
    finally:
        conn.close()


def tcp_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 8765))
    srv.listen(5)

    print("[TCP] Listening on 127.0.0.1:8765")

    while True:
        c, a = srv.accept()
        threading.Thread(target=handle_client, args=(c, a), daemon=True).start()


def main():
    # Ensure pigpio daemon is running
    pi = pigpio.pi()
    if not pi.connected:
        print(
            "ERROR: pigpio daemon not running. Start it: sudo systemctl start pigpiod"
        )
        return

    # Start in GREEN
    mode_green()

    # IR receiver
    rx = NecReceiver(pi, IR_PIN, on_ir_code)

    # TCP control thread
    threading.Thread(target=tcp_server, daemon=True).start()

    print("[CTRL] Ready: IR on GPIO23, TCP on 127.0.0.1:8765")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        rx.cancel()
        stop_all_leds()
        servo.angle = 0
        pi.stop()
        print("[CTRL] Exit.")


if __name__ == "__main__":
    main()
