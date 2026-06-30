import asyncio
import json
import logging
import os
import threading
import webbrowser
from threading import Timer
import time
from collections import deque
from concurrent.futures import TimeoutError

from bleak import BleakScanner
from flask import Flask, render_template, redirect, url_for, jsonify, make_response, request
from ph4_walkingpad.pad import Controller, WalkingPad

# ── Logging Setup ────────────────────────────────────────────────────────
# All print() statements will be replaced with this logging configuration.
# It provides timed, leveled output. Set level=logging.DEBUG to see verbose messages.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)

# ── Conversion constants ─────────────────────────────────────────────────
KM_TO_MI = 0.621371
KMH_TO_MPH = 0.621371
KCAL_PER_MILE = 95  # rough kcal per mile

# Speed control constants
MAX_SPEED_KMH = 6.0  # Approx 3.7 mph, a common max for these pads
MIN_SPEED_KMH = 1.0
SPEED_STEP = 0.6  # Speed change per button press in km/h
SLOW_WALK_SPEED_KMH = 4.5 # Approx 2.8 MPH

UNIT_METRIC = "metric"
UNIT_IMPERIAL = "imperial"
VALID_UNITS = {UNIT_METRIC, UNIT_IMPERIAL}
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")


def load_measurement_units() -> str:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as settings_file:
            saved_units = json.load(settings_file).get("measurement_units")
    except (OSError, ValueError):
        return UNIT_METRIC
    return saved_units if saved_units in VALID_UNITS else UNIT_METRIC


def save_measurement_units(units: str) -> None:
    with open(SETTINGS_FILE, "w", encoding="utf-8") as settings_file:
        json.dump({"measurement_units": units}, settings_file, indent=2)
        settings_file.write("\n")


def kcal_estimate(miles: float) -> float:
    return KCAL_PER_MILE * miles


def speed_for_display(speed_kmh: float) -> float:
    return speed_kmh if measurement_units == UNIT_METRIC else speed_kmh * KMH_TO_MPH


def distance_for_display(distance_km: float) -> float:
    return distance_km if measurement_units == UNIT_METRIC else distance_km * KM_TO_MI


def speed_unit_label() -> str:
    return "km/h" if measurement_units == UNIT_METRIC else "mph"


def distance_unit_label() -> str:
    return "km" if measurement_units == UNIT_METRIC else "mi"

# In app.py
def format_seconds_to_hms(total_seconds):
    """Converts total seconds to H:MM:SS string format."""
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours}:{minutes:02}:{seconds:02}"


# ── Flask & global state ────────────────────────────────────────────────
app = Flask(__name__)

connected = connecting = connection_failed = False
ble_loop: asyncio.AbstractEventLoop | None = None
controller: Controller | None = None
_pad_address: str | None = None
_auto_pause_grace_until = 0
speed_history = deque(maxlen=15)
measurement_units = load_measurement_units()

session_active = belt_running = False
resume_speed_kmh = 2.0  # default if none yet

current_speed_kmh = current_distance_km = 0.0
current_steps = 0
current_calories = 0.0
current_session_active_seconds = 0 

_last_dev_dist = _last_dev_steps = _last_dev_time = 0


# ── Context processor so templates always know flags ────────────────────
@app.context_processor
def inject_flags():
    return dict(
        connected=connected,
        connecting=connecting,
        connection_failed=connection_failed,
        measurement_units=measurement_units,
        speed_unit=speed_unit_label(),
        distance_unit=distance_unit_label(),
    )


# ── BLE helpers ─────────────────────────────────────────────────────────
async def _connect_to_pad() -> bool:
    global controller, _pad_address
    dev = None

    if _pad_address:
        logging.info(f"Attempting to connect to known address: {_pad_address}")
        try:
            dev = await BleakScanner.find_device_by_address(_pad_address, timeout=5)
        except Exception as exc:
            logging.warning(f"Failed to find device by address: {exc}")
            dev = None

    if not dev:
        logging.info("Scanning for device by name 'WalkingPad'...")
        try:
            dev = await BleakScanner.find_device_by_name("WalkingPad", timeout=10)
        except Exception as exc:
            logging.warning(f"Failed to find device by name: {exc}")
            dev = None

    if not dev:
        logging.error("Could not find WalkingPad. Ensure it is on and in range.")
        _pad_address = None
        return False

    _pad_address = dev.address
    logging.info(f"Device found! Address: {_pad_address}")

    controller = Controller()
    await controller.run(dev.address)

    if (
        hasattr(controller, "client")
        and controller.client
        and hasattr(controller.client, "set_disconnected_callback")
    ):
        controller.client.set_disconnected_callback(_handle_disconnect)

    await controller.switch_mode(WalkingPad.MODE_MANUAL)

    def _status_cb(_sender, st):
        try:
            if isinstance(st, dict):
                dist = st.get("dist", 0)
                steps = st.get("steps", 0)
                speed = st.get("speed", 0)
                dev_time = st.get("time", 0)
                belt_state = st.get("belt_state", 0)
            else:
                dist = getattr(st, "dist", 0)
                steps = getattr(st, "steps", 0)
                speed = getattr(st, "speed", 0)
                dev_time = getattr(st, "time", 0)
                belt_state = getattr(st, "belt_state", 0)
            process_status_packet(dist, steps, speed, dev_time, belt_state)
            logging.debug(f"Push d={dist} s={steps} v={speed} t={dev_time} state={belt_state}")
        except Exception as exc:
            logging.warning(f"status_cb error: {exc}")

    controller.on_cur_status_received = _status_cb

    if hasattr(controller, "enable_notifications"):
        try:
            await controller.enable_notifications()
        except Exception as exc:
            logging.warning(f"enable_notifications failed: {exc}")
    return True


def process_status_packet(dev_dist, dev_steps, dev_speed, dev_time=0, belt_state=0):
    """Update cumulative stats from raw values AND handle auto-pause."""
    global belt_running, resume_speed_kmh, _auto_pause_grace_until
    global current_speed_kmh, current_distance_km, current_steps, current_calories
    global current_session_active_seconds, session_active
    global _last_dev_dist, _last_dev_steps, _last_dev_time

    new_reported_speed_kmh = dev_speed / 10.0
    device_running = new_reported_speed_kmh > 0

    if device_running:
        session_active = True
        belt_running = True

    # Continuously populate the speed history with stable, non-zero speeds.
    if belt_running and new_reported_speed_kmh > MIN_SPEED_KMH:
        speed_history.append(new_reported_speed_kmh)

    # AUTO-PAUSE LOGIC
    if time.time() > _auto_pause_grace_until:
        if belt_running and new_reported_speed_kmh == 0 and current_speed_kmh > 0:
            logging.info("Belt has stopped unexpectedly. Auto-pausing session.")
            
            # Use the OLDEST speed from history to ignore the deceleration phase.
            if speed_history:
                resume_speed_kmh = speed_history[0] # Use the first (oldest) item
            else:
                # Fallback if pause happens too quickly after starting
                resume_speed_kmh = MIN_SPEED_KMH

            belt_running = False

    # CUMULATIVE STATS LOGIC (is unchanged)
    # ...
    if dev_dist < _last_dev_dist:
        _last_dev_dist = 0
    current_distance_km += (dev_dist - _last_dev_dist) / 100.0
    _last_dev_dist = dev_dist

    if dev_steps < _last_dev_steps:
        _last_dev_steps = 0
    current_steps += dev_steps - _last_dev_steps
    _last_dev_steps = dev_steps

    if dev_time < _last_dev_time:
        _last_dev_time = 0
    current_session_active_seconds += dev_time - _last_dev_time
    _last_dev_time = dev_time

    current_speed_kmh = new_reported_speed_kmh
    current_calories = kcal_estimate(current_distance_km * KM_TO_MI)


async def _telemetry_monitor():
    """Continuously request status packets while connected."""
    logging.info("Telemetry monitor started")
    while connected and controller:
        try:
            await controller.ask_stats()
        except Exception as exc:
            logging.warning(f"ask_stats error: {exc}")
        await asyncio.sleep(1)


def _ble_thread():
    global connected, connecting, connection_failed, ble_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ble_loop = loop

    if not loop.run_until_complete(_connect_to_pad()):
        connecting = False
        connection_failed = True
        return

    connected = True
    connecting = False
    loop.create_task(_telemetry_monitor())
    try:
        loop.run_forever()
    finally:
        connected = False
        loop.close()


def _start_ble_thread():
    global connecting, connection_failed
    if connected or connecting:
        return
    connecting = True
    connection_failed = False
    threading.Thread(target=_ble_thread, daemon=True).start()

def _handle_disconnect(client):
    """Callback function to handle unexpected disconnections."""
    global connected, belt_running, connecting, connection_failed
    if connected: # Only log if we thought we were connected
        logging.warning("Device has disconnected unexpectedly.")
    connected = False
    belt_running = False
    connecting = False
    connection_failed = True

# ── Flask routes ────────────────────────────────────────────────────────
@app.route("/")
def root():
    if not connected:
        return render_template("connecting.html") #

    time_active_display = "0:00:00" # Default for start/paused if not running
    if session_active: # Only calculate if a session is or was active
        time_active_display = format_seconds_to_hms(current_session_active_seconds)

    if not session_active:
        # For start_session, always show 0 time initially
        return render_template("start_session.html", time_active="0:00:00")

    template = "active_session.html" if belt_running else "paused_session.html"

    return render_template(
        template,
        speed=speed_for_display(current_speed_kmh),
        distance=distance_for_display(current_distance_km),
        steps=current_steps,
        calories=current_calories,
        time_active=time_active_display 
    )


@app.route("/reconnect", endpoint="reconnect")
@app.route("/manual_reconnect", endpoint="manual_reconnect")
def reconnect():
    if not connected and not connecting:
        _start_ble_thread()
    return redirect(url_for("root"))


@app.route("/settings")
def settings():
    return render_template(
        "settings.html",
        message=request.args.get("message"),
        error=request.args.get("error"),
    )


@app.route("/settings/units", methods=["POST"])
def set_units():
    global measurement_units

    requested_units = request.form.get("units", "")
    if requested_units not in VALID_UNITS:
        return redirect(url_for("settings", error="Unsupported unit selection."))

    if not connected or not controller or not ble_loop:
        return redirect(url_for("settings", error="Connect to the WalkingPad before changing device units."))

    async def apply_units():
        use_miles = requested_units == UNIT_IMPERIAL
        await controller.set_pref_units_miles(use_miles)
        await asyncio.sleep(0.5)
        await controller.ask_stats()

    try:
        asyncio.run_coroutine_threadsafe(apply_units(), ble_loop).result(timeout=8)
    except TimeoutError:
        return redirect(url_for("settings", error="Timed out while sending the unit setting to the WalkingPad."))
    except Exception as exc:
        logging.warning(f"Failed to set units: {exc}")
        return redirect(url_for("settings", error="Failed to send the unit setting to the WalkingPad."))

    measurement_units = requested_units
    save_measurement_units(measurement_units)
    message = "Units changed to metric." if measurement_units == UNIT_METRIC else "Units changed to imperial."
    return redirect(url_for("settings", message=message))


@app.route("/start")
def start_session():
    """Begin a new session: reset counters, start belt, launch stats monitor."""
    global session_active, belt_running, current_distance_km, current_steps, current_calories, resume_speed_kmh
    global current_session_active_seconds, _last_dev_dist, _last_dev_steps, _last_dev_time

    if not connected:
        return redirect(url_for("root"))

    current_distance_km = current_steps = current_calories = 0.0
    current_session_active_seconds = 0
    _last_dev_dist = _last_dev_steps = _last_dev_time = 0
    resume_speed_kmh = 2.0
    speed_history.clear() 

    session_active = True
    belt_running = True

    async def seq():
        try:
            await controller.start_belt()
            await asyncio.sleep(0.5)
        except Exception as exc:
            logging.error(f"Start sequence error: {exc}")

    asyncio.run_coroutine_threadsafe(seq(), ble_loop)
    return redirect(url_for("root"))


# ── Pause / Resume ───────────────────────────────────────────────────────

@app.route("/pause", endpoint="pause")
@app.route("/pause_session", endpoint="pause_session")
def pause_session():
    global belt_running, resume_speed_kmh
    if not belt_running:
        return redirect(url_for("root"))
    
    # Use the most recent speed from our history for manual pause
    if speed_history:
        resume_speed_kmh = speed_history[-1]

    belt_running = False
    asyncio.run_coroutine_threadsafe(controller.stop_belt(), ble_loop)
    return redirect(url_for("root"))


@app.route("/resume", endpoint="resume")
@app.route("/resume_session", endpoint="resume_session")
def resume_session():
    global belt_running, _auto_pause_grace_until, session_active # session_active ensures we only resume active sessions
    
    if not session_active: # Can't resume if no session was active
        logging.warning("Resume called but no active session.")
        return redirect(url_for("root"))

    if belt_running: # Already running, do nothing
        logging.info("Resume called but belt is already running.")
        return redirect(url_for("root"))

    # --- CRITICAL FIX: Optimistically set state for UI and grace period ---
    logging.info("Resume button clicked. Setting app state to active.")
    belt_running = True
    _auto_pause_grace_until = time.time() + 7 # Generous 7-second grace period for commands to take effect

    async def seq():
        try:
            logging.info("Attempting resume: Sending wake-up and start sequence to device...")
            
            # Standard wake-up and start sequence
            await controller.switch_mode(WalkingPad.MODE_STANDBY)
            await asyncio.sleep(0.5) 
            await controller.switch_mode(WalkingPad.MODE_MANUAL)
            await asyncio.sleep(0.5) 
            
            await controller.start_belt()
            await asyncio.sleep(0.5) 
            
            logging.info(f"Setting speed to {resume_speed_kmh:.1f} km/h.")
            await controller.change_speed(int(resume_speed_kmh * 10))
            await asyncio.sleep(0.5) # Allow speed change to propagate
            
            logging.info("Resume sequence commands sent.")

        except Exception as exc:
            logging.error(f"Error during resume sequence, device may have disconnected: {exc}")
            _handle_disconnect(None) # This will set belt_running = False and connected = False
                                     # The frontend polling will then reload to the correct disconnected/connecting page.

    asyncio.run_coroutine_threadsafe(seq(), ble_loop)
    # The redirect will now happen after belt_running is True in the main thread.
    return redirect(url_for("root"))


# ── Speed Controls ───────────────────────────────────────────────────────
@app.route("/decrease_speed")
def decrease_speed():
    """Decrease the belt speed by one step."""
    if not belt_running:
        return redirect(url_for("root"))

    new_speed_kmh = max(MIN_SPEED_KMH, current_speed_kmh - SPEED_STEP)
    dev_speed = int(new_speed_kmh * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))

@app.route("/slow_speed")
def slow_speed():
    """Set the belt speed to a predefined slow walk speed."""
    if not belt_running:
        return redirect(url_for("root"))
    
    dev_speed = int(SLOW_WALK_SPEED_KMH * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))

@app.route("/increase_speed")
def increase_speed():
    """Increase the belt speed by one step."""
    if not belt_running:
        return redirect(url_for("root"))

    new_speed_kmh = min(MAX_SPEED_KMH, current_speed_kmh + SPEED_STEP)
    dev_speed = int(new_speed_kmh * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


@app.route("/max_speed")
def max_speed():
    """Set the belt speed to maximum."""
    if not belt_running:
        return redirect(url_for("root"))
    
    dev_speed = int(MAX_SPEED_KMH * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


# ── Live JSON endpoint ───────────────────────────────────────────────────
@app.route("/stats", endpoint="get_stats")
def stats_json():
    # Calculate formatted_time_active within the function scope
    formatted_time_active = format_seconds_to_hms(current_session_active_seconds)

    data = dict(
        is_connected=connected,      
        is_running=belt_running,     
        speed=round(speed_for_display(current_speed_kmh), 1),
        speed_unit=speed_unit_label(),
        distance=round(distance_for_display(current_distance_km), 2),
        distance_unit=distance_unit_label(),
        steps=current_steps,
        calories=round(current_calories),
        time_active=formatted_time_active 
    )

    resp = make_response(jsonify(data))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Shutdown endpoint ──────────────────────────────────────────────────
@app.route("/shutdown", methods=['POST'])
def shutdown():
    """Forcefully shut down the Flask application process."""
    logging.info("Server shutting down via forceful exit...")
    os._exit(0)
