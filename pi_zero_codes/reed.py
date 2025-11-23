ami@catdoorpi:~ $ cat reed.py 
#!/usr/bin/env python3
import time
from gpiozero import Button
import os

# --- Pins (BCM) ---
REED_GPIO = 4  # Reed switch (door magnetic sensor)

reed = Button(REED_GPIO, pull_up=True, bounce_time=0.05)

open_count = 0
opened_at = None
longest_open = 0.0

LOG_PATH = "/home/rami/logs/reed_logs.txt"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


def log_event(msg: str):
    """Append a timestamped event to the log file."""
    with open(LOG_PATH, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def on_open():
    """Triggered when the reed switch opens (door opened)."""
    global open_count, opened_at
    open_count += 1
    opened_at = time.time()
    print(f"🧲 OPEN #{open_count} @ {time.strftime('%H:%M:%S')}")


def on_close():
    """Triggered when the reed switch closes (door closed)."""
    global opened_at, longest_open
    if opened_at is None:
        return

    dur = time.time() - opened_at
    longest_open = max(longest_open, dur)
    print(f"✅ CLOSED @ {time.strftime('%H:%M:%S')} (open {dur:.2f} s)")

    # Log only if the flap was open for more than 1s (ignore small movements)
    if dur >= 1.0:
        log_event(f"Flap open {dur:.2f}s")

    opened_at = None


# --- Attach event handlers ---
reed.when_released = on_open   # Magnetic contact broken → flap opened
reed.when_pressed = on_close   # Magnetic contact restored → flap closed

print("Reed logging running. Ctrl+C to quit.")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nBye.")

