from flask import Flask, render_template, jsonify, request
from datetime import datetime
from dotenv import load_dotenv
import threading
import time
import os

load_dotenv()

from sensor.turbidity import TurbiditySensor
from sensor.lcd import LCDDisplay
from supabase import create_client

app = Flask(__name__)

# ── Supabase (internal only, never exposed to UI) ─────────────────────────────
_supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

# ── Hardware ──────────────────────────────────────────────────────────────────
sensor = TurbiditySensor()
lcd    = LCDDisplay()

# ── State ─────────────────────────────────────────────────────────────────────
latest_reading = {
    "ntu":       0.0,
    "voltage":   0.0,
    "status":    "Unknown",
    "timestamp": datetime.utcnow().isoformat(),
}

# Sensor range: 0–1000 NTU
settings = {
    "clean_threshold":   300,   # NTU <= this  → Clean
    "warning_threshold": 700,   # NTU <= this  → Turbid, else → Dirty
    "sampling_interval": 5,     # seconds between readings
    "lcd_enabled":       True,
    "logging_enabled":   True,
}

# ── Classification ────────────────────────────────────────────────────────────
def classify(ntu: float) -> str:
    if ntu <= settings["clean_threshold"]:
        return "Clean"
    if ntu <= settings["warning_threshold"]:
        return "Turbid"
    return "Dirty"

# ── Supabase helpers ──────────────────────────────────────────────────────────
def _db_log(reading: dict):
    if not settings["logging_enabled"]:
        return
    try:
        _supabase.table("readings").insert({
            "ntu":        reading["ntu"],
            "voltage":    reading["voltage"],
            "status":     reading["status"],
            "created_at": reading["timestamp"],
        }).execute()
    except Exception as e:
        print(f"[DB] Insert error: {e}")

def _db_save_settings():
    try:
        _supabase.table("settings").upsert({"id": 1, **settings}).execute()
    except Exception as e:
        print(f"[DB] Settings save error: {e}")

def _db_load_settings():
    try:
        res = _supabase.table("settings").select("*").eq("id", 1).execute()
        if res.data:
            row = res.data[0]
            for k in settings:
                if k in row:
                    settings[k] = row[k]
    except Exception as e:
        print(f"[DB] Settings load error: {e}")

# ── Sensor loop ───────────────────────────────────────────────────────────────
def sensor_loop():
    while True:
        try:
            voltage, ntu = sensor.read()
            status = classify(ntu)
            ts     = datetime.utcnow().isoformat()
            latest_reading.update(
                ntu=round(ntu, 2),
                voltage=round(voltage, 4),
                status=status,
                timestamp=ts,
            )
            if settings["lcd_enabled"]:
                lcd.display(ntu, status)
            _db_log(latest_reading.copy())
        except Exception as e:
            print(f"[Sensor] Error: {e}")
        time.sleep(settings["sampling_interval"])

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
    return jsonify(latest_reading)

@app.route("/api/logs")
def api_logs():
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    status   = request.args.get("status", "")
    try:
        q = _supabase.table("readings").select("*", count="exact") \
                     .order("created_at", desc=True) \
                     .range((page - 1) * per_page, page * per_page - 1)
        if status:
            q = q.eq("status", status)
        res = q.execute()
        return jsonify({"data": res.data, "count": res.count})
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
            val = bool(val)
        elif isinstance(settings[key], int):
            val = int(val)
        elif isinstance(settings[key], float):
            val = float(val)
        settings[key] = val
    _db_save_settings()
    return jsonify({"ok": True, "settings": settings})

# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _db_load_settings()
    threading.Thread(target=sensor_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)