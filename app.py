from __future__ import annotations

from flask import Flask, render_template, jsonify, request
from datetime import datetime, timezone
from dotenv import load_dotenv
import math
import os
import random
import threading
import time
from typing import Any

import requests

load_dotenv()

app = Flask(__name__)

# ==============================================================================
# Hardware (ADS1115 turbidity + I2C LCD) — kept in this file for easier debugging
# ==============================================================================

# ── Turbidity (ADS1115) ───────────────────────────────────────────────────────
try:
    import board
    import busio
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.analog_in import AnalogIn

    _TURB_HW_AVAILABLE = True
except Exception:
    _TURB_HW_AVAILABLE = False

# Adjust these after calibrating with your sensor
V_CLEAN = float(os.environ.get("V_CLEAN", "4.2"))  # volts → ~0 NTU
V_DIRTY = float(os.environ.get("V_DIRTY", "0.8"))  # volts → ~1000 NTU
NTU_MAX = 1000.0


def _voltage_to_ntu(voltage: float) -> float:
    if V_CLEAN == V_DIRTY:
        return 0.0
    ntu = (V_CLEAN - voltage) / (V_CLEAN - V_DIRTY) * NTU_MAX
    return max(0.0, min(NTU_MAX, ntu))


class TurbiditySensor:
    def __init__(self) -> None:
        self._channel: AnalogIn | None = None
        if not _TURB_HW_AVAILABLE:
            print("[Sensor] Hardware libs unavailable — simulation mode active.")
            return
        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            ads = ADS.ADS1115(i2c, address=int(os.environ.get("ADS1115_ADDR", "0x48"), 16))
            ads.gain = int(os.environ.get("ADS1115_GAIN", "1"))
            self._channel = AnalogIn(ads, ADS.P0)
            print("[Sensor] ADS1115 ready.")
        except Exception as e:
            print(f"[Sensor] ADS1115 init failed — simulation mode active: {e}")

    def read(self) -> tuple[float, float]:
        """Returns (voltage, ntu)."""
        if self._channel is not None:
            voltage = float(self._channel.voltage)
        else:
            voltage = V_DIRTY + (V_CLEAN - V_DIRTY) * (0.5 + 0.45 * math.sin(time.time() / 20))
            voltage += random.uniform(-0.05, 0.05)
            voltage = max(V_DIRTY, min(V_CLEAN, voltage))
        ntu = _voltage_to_ntu(voltage)
        return round(voltage, 4), round(ntu, 2)


# ── LCD (RPLCD) ───────────────────────────────────────────────────────────────
try:
    from RPLCD.i2c import CharLCD

    _LCD_HW_AVAILABLE = True
except Exception:
    _LCD_HW_AVAILABLE = False


class LCDDisplay:
    def __init__(self, address: int = 0x27, cols: int = 16, rows: int = 2) -> None:
        self._lcd: CharLCD | None = None
        if not _LCD_HW_AVAILABLE:
            print("[LCD] RPLCD not installed — display disabled.")
            return
        try:
            self._lcd = CharLCD(
                i2c_expander="PCF8574",
                address=address,
                port=int(os.environ.get("I2C_PORT", "1")),
                cols=cols,
                rows=rows,
                dotsize=8,
            )
            self._lcd.clear()
            self._lcd.write_string("AquaTrack Ready")
            print(f"[LCD] Initialised at 0x{address:02X}.")
        except Exception as e:
            print(f"[LCD] Init failed — display disabled: {e}")

    def display(self, ntu: float, status: str) -> None:
        if self._lcd is None:
            return
        try:
            self._lcd.clear()
            self._lcd.write_string(f"NTU: {ntu:.1f}")
            self._lcd.cursor_pos = (1, 0)
            self._lcd.write_string(f"Status: {status:<8}")
        except Exception as e:
            print(f"[LCD] Write error: {e}")

    def clear(self) -> None:
        if self._lcd is None:
            return
        try:
            self._lcd.clear()
        except Exception:
            pass


sensor = TurbiditySensor()
lcd = LCDDisplay(address=int(os.environ.get("LCD_I2C_ADDR", "0x27"), 16))

# ==============================================================================
# Runtime state
# ==============================================================================

latest_reading: dict[str, Any] = {
    "ntu": 0.0,
    "voltage": 0.0,
    "status": "Unknown",
    "timestamp": datetime.now(timezone.utc).isoformat(),
}

# Sensor range: 0–1000 NTU
settings: dict[str, Any] = {
    "clean_threshold": 300,  # NTU <= this  → Clean
    "warning_threshold": 700,  # NTU <= this → Turbid, else → Dirty
    "sampling_interval": 1,  # seconds between readings (default 1s)
    "lcd_enabled": True,
    "logging_enabled": True,
}

_state_lock = threading.Lock()
_wake_event = threading.Event()
_started = False
_last_hw_read_monotonic = 0.0
_min_read_spacing_s = float(os.environ.get("MIN_READ_SPACING_S", "0.4"))


def classify(ntu: float) -> str:
    if ntu <= float(settings["clean_threshold"]):
        return "Clean"
    if ntu <= float(settings["warning_threshold"]):
        return "Turbid"
    return "Dirty"


# ==============================================================================
# Supabase (REST) — lighter than supabase-py (better on Pi Zero / ARMv6)
# ==============================================================================

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
# Accept either:
#   - https://<project>.supabase.co
#   - https://<project>.supabase.co/rest/v1
if _SUPABASE_URL.endswith("/rest/v1"):
    _SUPABASE_URL = _SUPABASE_URL[: -len("/rest/v1")]
_SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


def _sb_headers(prefer: str | None = None) -> dict[str, str]:
    h = {
        "apikey": _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _sb_enabled() -> bool:
    return bool(_SUPABASE_URL and _SUPABASE_KEY)


def _coerce_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("1", "true", "t", "yes", "y", "on"):
            return True
        if v in ("0", "false", "f", "no", "n", "off", ""):
            return False
    return bool(val)


def _normalize_threshold(v: Any) -> int:
    """
    Thresholds are expected in NTU [0..1000].
    If the DB defaults are mis-scaled (e.g. 3000/4000), scale down by 10s.
    """
    try:
        x = int(float(v))
    except Exception:
        return 0
    while x > 1000 and x % 10 == 0:
        x //= 10
    return max(0, min(1000, x))


def _normalize_settings_in_place() -> None:
    settings["clean_threshold"] = _normalize_threshold(settings.get("clean_threshold", 300))
    settings["warning_threshold"] = _normalize_threshold(settings.get("warning_threshold", 700))
    # enforce clean < warning
    if settings["clean_threshold"] >= settings["warning_threshold"]:
        settings["warning_threshold"] = min(1000, settings["clean_threshold"] + 10)
    try:
        settings["sampling_interval"] = max(1, int(float(settings.get("sampling_interval", 1))))
    except Exception:
        settings["sampling_interval"] = 1
    settings["lcd_enabled"] = _coerce_bool(settings.get("lcd_enabled", True))
    settings["logging_enabled"] = _coerce_bool(settings.get("logging_enabled", True))


def _db_log(reading: dict[str, Any]) -> None:
    if not settings.get("logging_enabled"):
        return
    if not _sb_enabled():
        print("[DB] SUPABASE_URL/SUPABASE_KEY not set — skipping log.")
        return
    try:
        payload = {
            "ntu": reading["ntu"],
            "voltage": reading["voltage"],
            "status": reading["status"],
            "created_at": reading["timestamp"],
        }
        r = requests.post(
            f"{_SUPABASE_URL}/rest/v1/readings",
            headers=_sb_headers(),
            params={"select": "id"},
            json=payload,
            timeout=5,
        )
        if r.status_code >= 300:
            print(f"[DB] Insert failed ({r.status_code}): {r.text}")
    except Exception as e:
        print(f"[DB] Insert error: {e}")


def _db_save_settings() -> None:
    if not _sb_enabled():
        return
    try:
        # Your RLS allows UPDATE on settings (not INSERT), so use PATCH id=eq.1.
        _normalize_settings_in_place()
        payload = {**settings, "updated_at": datetime.now(timezone.utc).isoformat()}
        r = requests.patch(
            f"{_SUPABASE_URL}/rest/v1/settings",
            headers=_sb_headers("return=minimal"),
            params={"id": "eq.1"},
            json=payload,
            timeout=5,
        )
        if r.status_code >= 300:
            print(f"[DB] Settings save failed ({r.status_code}): {r.text}")
    except Exception as e:
        print(f"[DB] Settings save error: {e}")


def _db_load_settings() -> None:
    if not _sb_enabled():
        return
    try:
        r = requests.get(
            f"{_SUPABASE_URL}/rest/v1/settings",
            headers=_sb_headers(),
            params={"select": "*", "id": "eq.1"},
            timeout=5,
        )
        if r.status_code >= 300:
            print(f"[DB] Settings load failed ({r.status_code}): {r.text}")
            return
        rows = r.json()
        if not rows:
            return
        row = rows[0]
        for k in list(settings.keys()):
            if k in row and row[k] is not None:
                settings[k] = row[k]
        _normalize_settings_in_place()
    except Exception as e:
        print(f"[DB] Settings load error: {e}")


def _db_get_logs(page: int, per_page: int, status: str) -> tuple[list[dict[str, Any]], int]:
    if not _sb_enabled():
        return ([], 0)
    params: dict[str, str] = {
        "select": "*",
        "order": "created_at.desc",
        "limit": str(per_page),
        "offset": str((page - 1) * per_page),
    }
    if status:
        params["status"] = f"eq.{status}"

    r = requests.get(
        f"{_SUPABASE_URL}/rest/v1/readings",
        headers=_sb_headers("count=exact"),
        params=params,
        timeout=8,
    )
    if r.status_code >= 300:
        raise RuntimeError(r.text)

    count = 0
    cr = r.headers.get("Content-Range")  # e.g. "0-49/1234"
    if cr and "/" in cr:
        try:
            count = int(cr.split("/")[-1])
        except Exception:
            count = 0
    return (r.json(), count)


# ==============================================================================
# Sensor loop + instant refresh
# ==============================================================================


def _take_reading(force: bool = False) -> dict[str, Any]:
    """
    Takes a single reading and updates:
      - `latest_reading` (always)
      - LCD (if enabled)
      - Supabase logs (if enabled)
    """
    global _last_hw_read_monotonic

    with _state_lock:
        now_m = time.monotonic()
        if not force and (now_m - _last_hw_read_monotonic) < _min_read_spacing_s:
            return latest_reading.copy()

        voltage, ntu = sensor.read()
        status = classify(ntu)
        ts = datetime.now(timezone.utc).isoformat()

        latest_reading.update(
            ntu=float(ntu),
            voltage=float(voltage),
            status=status,
            timestamp=ts,
        )
        _last_hw_read_monotonic = now_m
        out = latest_reading.copy()

    # Do slow/IO work outside the lock
    if settings.get("lcd_enabled"):
        lcd.display(float(out["ntu"]), str(out["status"]))
    _db_log(out)
    return out


def sensor_loop() -> None:
    while True:
        try:
            _take_reading(force=False)
        except Exception as e:
            print(f"[Sensor] Error: {e}")

        # Wait until next interval OR wake immediately on settings change/refresh
        interval = max(0.2, float(settings.get("sampling_interval", 5)))
        _wake_event.wait(timeout=interval)
        _wake_event.clear()

# ── Page routes ───────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return render_template("dashboard.html")

@app.route("/logs")
def logs():
    return render_template("logs.html")

@app.route("/settings")
def settings_page():
    return render_template("settings.html")

# ── API ───────────────────────────────────────────────────────────────────────
@app.route("/api/reading")
def api_reading():
    fresh = request.args.get("fresh", "").lower() in ("1", "true", "yes", "y")
    try:
        if fresh:
            r = _take_reading(force=True)
            return jsonify(r)
        with _state_lock:
            return jsonify(latest_reading.copy())
    except Exception as e:
        return jsonify({"error": str(e), **latest_reading}), 500


@app.route("/api/reading/refresh", methods=["POST"])
def api_reading_refresh():
    try:
        _wake_event.set()
        r = _take_reading(force=True)
        return jsonify({"ok": True, "reading": r})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/logs")
def api_logs():
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    status   = request.args.get("status", "")
    try:
        data, count = _db_get_logs(page=page, per_page=per_page, status=status)
        return jsonify({"data": data, "count": count})
    except Exception as e:
        return jsonify({"error": str(e), "data": [], "count": 0}), 500

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(settings)

@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.get_json(force=True)
    for key in settings:
        if key not in data:
            continue
        val = data[key]
        if isinstance(settings[key], bool):
            val = _coerce_bool(val)
        elif isinstance(settings[key], int):
            if key in ("clean_threshold", "warning_threshold"):
                val = _normalize_threshold(val)
            elif key == "sampling_interval":
                val = max(1, int(float(val)))
            else:
                val = int(val)
        elif isinstance(settings[key], float):
            val = float(val)
        settings[key] = val
    _normalize_settings_in_place()
    _db_save_settings()
    # Wake loop so changes apply immediately (and refresh LCD/UI quickly)
    _wake_event.set()
    return jsonify({"ok": True, "settings": settings})


@app.route("/health")
def health():
    """
    Simple diagnostics endpoint for Raspberry Pi debugging.
    Does not expose any secrets.
    """
    # Sensor status
    sensor_ready = getattr(sensor, "_channel", None) is not None

    # LCD status
    lcd_ready = getattr(lcd, "_lcd", None) is not None

    # Supabase status (best-effort ping)
    supabase = {"enabled": _sb_enabled(), "ok": False, "error": None}
    if _sb_enabled():
        try:
            r = requests.get(
                f"{_SUPABASE_URL}/rest/v1/",
                headers=_sb_headers(),
                timeout=5,
            )
            supabase["ok"] = r.status_code < 500
            if r.status_code >= 300 and r.status_code < 500:
                supabase["error"] = f"http_{r.status_code}"
        except Exception as e:
            supabase["ok"] = False
            supabase["error"] = str(e)

    # One fast sensor read attempt (won't spam hardware due to min spacing)
    read_ok = True
    read_error = None
    try:
        r = _take_reading(force=False)
    except Exception as e:
        read_ok = False
        read_error = str(e)
        r = None

    return jsonify(
        {
            "ok": bool(read_ok),
            "hardware": {
                "turbidity_libs_available": _TURB_HW_AVAILABLE,
                "turbidity_sensor_ready": bool(sensor_ready),
                "lcd_libs_available": _LCD_HW_AVAILABLE,
                "lcd_ready": bool(lcd_ready),
                "ads1115_addr": os.environ.get("ADS1115_ADDR", "0x48"),
                "lcd_i2c_addr": os.environ.get("LCD_I2C_ADDR", "0x27"),
                "i2c_port": os.environ.get("I2C_PORT", "1"),
            },
            "supabase": supabase,
            "settings": settings,
            "latest_reading": r,
            "error": read_error,
            "server_time_utc": datetime.now(timezone.utc).isoformat(),
        }
    )

# ── Boot ──────────────────────────────────────────────────────────────────────
def _start_once() -> None:
    global _started
    if _started:
        return
    _started = True
    _db_load_settings()
    threading.Thread(target=sensor_loop, daemon=True).start()


# Start background threads under `flask run` as well.
#
# When Flask's debug reloader is enabled, the module is imported twice; only
# start the loop in the reloader "main" process.
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not os.environ.get("FLASK_RUN_FROM_CLI"):
    _start_once()


if __name__ == "__main__":
    _start_once()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)