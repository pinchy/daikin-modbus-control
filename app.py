"""
Aircon Local Control — Flask backend
Communicates with Daikin Zena units via Modbus TCP (port 502)
"""

import json
import time
import threading
from flask import Flask, jsonify, request, send_from_directory
from pymodbus.client.sync import ModbusTcpClient

app = Flask(__name__, static_folder="static")

# ─── Configuration ───────────────────────────────────────────────────────────

CONFIG_FILE = "config.json"

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

# ─── Modbus Register Map ────────────────────────────────────────────────────
#
# Holding Registers (read/write):
#   HR 2000 — Power & Mode
#             Bit 0 = power (0=off, 1=on)
#             Upper byte = mode:
#               0x00 = Auto (0 + power bit)
#               0x01 = Dry  (256 + power bit)
#               0x02 = Cool (512 + power bit)
#               0x03 = Heat (768 + power bit)
#               0x04 = Fan  (1024 + power bit)
#   HR 2001 — Setpoint (value ÷ 10 = °C, e.g. 220 = 22.0°C)
#   HR 2003 — Fan speed
#             0  = Auto
#             11 = Quiet
#             3  = Level 1
#             4  = Level 2
#             5  = Level 3
#             6  = Level 4
#             7  = Level 5
#
# Input Registers (read-only):
#   IR 2005 — Room temperature (value ÷ 10 = °C)
#   IR 2006 — Outdoor temperature (value ÷ 10 = °C)
#

MODE_MAP = {
    "auto": 0x0000,
    "dry":  0x0100,
    "cool": 0x0200,
    "heat": 0x0300,
    "fan":  0x0400,
}

MODE_REVERSE = {v >> 8: k for k, v in MODE_MAP.items()}

FAN_MAP = {
    "auto":  0,
    "quiet": 11,
    "1":     3,
    "2":     4,
    "3":     5,
    "4":     6,
    "5":     7,
}

FAN_REVERSE = {v: k for k, v in FAN_MAP.items()}

# ─── Modbus Helpers ──────────────────────────────────────────────────────────

def read_unit(ip):
    """Read all status from a unit. Returns dict or None on error."""
    try:
        client = ModbusTcpClient(ip, port=502, timeout=3)
        client.connect()

        # Read holding registers (controls)
        hr = {}
        for addr in [2000, 2001, 2003]:
            result = client.read_holding_registers(address=addr, count=1)
            if not result.isError():
                hr[addr] = result.registers[0]

        # Read input registers (sensors)
        ir = {}
        for addr in [2005, 2006]:
            result = client.read_input_registers(address=addr, count=1)
            if not result.isError():
                ir[addr] = result.registers[0]

        client.close()

        if 2000 not in hr:
            return None

        power_mode = hr.get(2000, 0)
        power = bool(power_mode & 0x01)
        mode_byte = (power_mode >> 8) & 0xFF
        mode = MODE_REVERSE.get(mode_byte, "unknown")
        setpoint = hr.get(2001, 220) / 10.0
        fan_val = hr.get(2003, 0)
        fan = FAN_REVERSE.get(fan_val, "auto")
        room_temp = ir.get(2005, 0) / 10.0
        outdoor_temp = ir.get(2006, 0) / 10.0

        return {
            "power": power,
            "mode": mode,
            "setpoint": setpoint,
            "fan": fan,
            "room_temp": room_temp,
            "outdoor_temp": outdoor_temp,
            "online": True,
        }
    except Exception as e:
        print(f"Error reading {ip}: {e}")
        return {"online": False, "error": str(e)}


def write_unit(ip, register, value):
    """Write a single register to a unit."""
    try:
        client = ModbusTcpClient(ip, port=502, timeout=3)
        client.connect()
        result = client.write_register(address=register, value=value)
        client.close()
        return not result.isError()
    except Exception as e:
        print(f"Error writing to {ip}: {e}")
        return False


# ─── Cache ───────────────────────────────────────────────────────────────────

status_cache = {}
cache_lock = threading.Lock()
CACHE_TTL = 5  # seconds

def get_cached_status(unit_id, ip):
    with cache_lock:
        cached = status_cache.get(unit_id)
        if cached and (time.time() - cached["_time"]) < CACHE_TTL:
            return cached
    status = read_unit(ip)
    if status:
        status["_time"] = time.time()
        with cache_lock:
            status_cache[unit_id] = status
    return status

def invalidate_cache(unit_id):
    with cache_lock:
        status_cache.pop(unit_id, None)


# ─── API Routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")


@app.route("/api/units", methods=["GET"])
def get_units():
    """Get config + live status for all units."""
    config = load_config()
    units = []
    for unit in config["units"]:
        status = get_cached_status(unit["id"], unit["ip"])
        units.append({
            "id": unit["id"],
            "name": unit["name"],
            "ip": unit["ip"],
            **(status or {"online": False}),
        })
    return jsonify(units)


@app.route("/api/units/<unit_id>", methods=["GET"])
def get_unit(unit_id):
    """Get live status for a single unit."""
    config = load_config()
    unit = next((u for u in config["units"] if u["id"] == unit_id), None)
    if not unit:
        return jsonify({"error": "Unit not found"}), 404
    invalidate_cache(unit_id)
    status = get_cached_status(unit_id, unit["ip"])
    return jsonify({"id": unit["id"], "name": unit["name"], "ip": unit["ip"], **(status or {"online": False})})


@app.route("/api/units/<unit_id>/power", methods=["POST"])
def set_power(unit_id):
    """Turn unit on or off. Body: {"power": true/false}"""
    config = load_config()
    unit = next((u for u in config["units"] if u["id"] == unit_id), None)
    if not unit:
        return jsonify({"error": "Unit not found"}), 404

    data = request.json
    power = data.get("power", False)

    # Read current mode to preserve it
    status = read_unit(unit["ip"])
    if not status or not status.get("online"):
        return jsonify({"error": "Unit offline"}), 503

    mode_val = MODE_MAP.get(status["mode"], 0x0200)
    value = mode_val | (1 if power else 0)

    ok = write_unit(unit["ip"], 2000, value)
    invalidate_cache(unit_id)
    return jsonify({"success": ok})


@app.route("/api/units/<unit_id>/mode", methods=["POST"])
def set_mode(unit_id):
    """Set mode. Body: {"mode": "cool"|"heat"|"auto"|"dry"|"fan"}"""
    config = load_config()
    unit = next((u for u in config["units"] if u["id"] == unit_id), None)
    if not unit:
        return jsonify({"error": "Unit not found"}), 404

    data = request.json
    mode = data.get("mode", "cool")

    if mode not in MODE_MAP:
        return jsonify({"error": f"Invalid mode: {mode}"}), 400

    # Read current power state to preserve it
    status = read_unit(unit["ip"])
    if not status or not status.get("online"):
        return jsonify({"error": "Unit offline"}), 503

    power_bit = 1 if status["power"] else 0
    value = MODE_MAP[mode] | power_bit

    ok = write_unit(unit["ip"], 2000, value)
    invalidate_cache(unit_id)
    return jsonify({"success": ok})


@app.route("/api/units/<unit_id>/setpoint", methods=["POST"])
def set_setpoint(unit_id):
    """Set temperature. Body: {"setpoint": 22.0}"""
    config = load_config()
    unit = next((u for u in config["units"] if u["id"] == unit_id), None)
    if not unit:
        return jsonify({"error": "Unit not found"}), 404

    data = request.json
    setpoint = data.get("setpoint", 22.0)

    # Clamp to valid range
    setpoint = max(16.0, min(30.0, float(setpoint)))
    value = int(setpoint * 10)

    ok = write_unit(unit["ip"], 2001, value)
    invalidate_cache(unit_id)
    return jsonify({"success": ok})


@app.route("/api/units/<unit_id>/fan", methods=["POST"])
def set_fan(unit_id):
    """Set fan speed. Body: {"fan": "auto"|"quiet"|"1"-"5"}"""
    config = load_config()
    unit = next((u for u in config["units"] if u["id"] == unit_id), None)
    if not unit:
        return jsonify({"error": "Unit not found"}), 404

    data = request.json
    fan = str(data.get("fan", "auto"))

    if fan not in FAN_MAP:
        return jsonify({"error": f"Invalid fan speed: {fan}"}), 400

    ok = write_unit(unit["ip"], 2003, FAN_MAP[fan])
    invalidate_cache(unit_id)
    return jsonify({"success": ok})


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8082, debug=True)
