"""
Turbidity sensor reader via ADS1115 on I2C.

Sensor spec:
  - Supply voltage : 3.3–5 V  (connected to 5V)
  - Output         : Analog 0–Vcc
  - Range          : 0–1000 NTU

Wiring:
  Turbidity VCC  → Pi Pin 2  (5V)
  Turbidity GND  → Pi Pin 6  (GND)
  Turbidity AO   → ADS1115 A0

  ADS1115 VDD    → Pi Pin 1  (3.3V)
  ADS1115 SDA    → Pi Pin 3  (GPIO2)
  ADS1115 SCL    → Pi Pin 5  (GPIO3)
  ADS1115 GND    → Pi Pin 6  (GND)
  ADS1115 ADDR   → Pi Pin 6  (GND)  →  I2C address 0x48

Voltage → NTU relationship:
  This sensor outputs HIGHER voltage for CLEANER water.
  At ~4.2 V  → ~0 NTU   (very clean)
  At ~2.5 V  → ~500 NTU (moderately turbid)
  At ~1.0 V  → ~1000 NTU (very turbid / max range)

  Linear approximation used here (calibrate with known reference):
    NTU = (V_max - V) / (V_max - V_min) * NTU_max
  where V_max ≈ 4.2 V, V_min ≈ 0.8 V, NTU_max = 1000
"""

import math
import random
import time

try:
    import board
    import busio
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.analog_in import AnalogIn
    _HW_AVAILABLE = True
except ImportError:
    _HW_AVAILABLE = False

# ── Calibration constants ─────────────────────────────────────────────────────
# Adjust these to match your specific sensor after calibration.
# V_CLEAN  : voltage reading in clean (distilled) water → 0 NTU
# V_DIRTY  : voltage reading at maximum turbidity       → 1000 NTU
V_CLEAN  = 4.2   # volts — high voltage = clean
V_DIRTY  = 0.8   # volts — low voltage  = dirty
NTU_MAX  = 1000  # sensor maximum


class TurbiditySensor:
    def __init__(self):
        self._channel = None
        if _HW_AVAILABLE:
            try:
                i2c = busio.I2C(board.SCL, board.SDA)
                ads = ADS.ADS1115(i2c, address=0x48)
                # Use gain=1 → ±4.096 V full-scale, sufficient for 0–5V via ADS
                ads.gain = 1
                self._channel = AnalogIn(ads, ADS.P0)
                print("[Sensor] ADS1115 ready at 0x48 A0.")
            except Exception as e:
                print(f"[Sensor] ADS1115 init failed, running in simulation: {e}")
        else:
            print("[Sensor] Hardware libs unavailable — simulation mode active.")

    @staticmethod
    def _voltage_to_ntu(voltage: float) -> float:
        """
        Linear mapping:  V_CLEAN → 0 NTU,  V_DIRTY → NTU_MAX
        Clamps result to [0, NTU_MAX].
        """
        if V_CLEAN == V_DIRTY:
            return 0.0
        # Invert: higher voltage → cleaner water
        ntu = (V_CLEAN - voltage) / (V_CLEAN - V_DIRTY) * NTU_MAX
        return max(0.0, min(NTU_MAX, ntu))

    def read(self) -> tuple[float, float]:
        """Returns (voltage, ntu)."""
        if self._channel is not None:
            voltage = self._channel.voltage
        else:
            # Simulate a slow oscillation across the full sensor range
            voltage = V_DIRTY + (V_CLEAN - V_DIRTY) * (
                0.5 + 0.45 * math.sin(time.time() / 20)
            ) + random.uniform(-0.05, 0.05)
            voltage = max(V_DIRTY, min(V_CLEAN, voltage))

        ntu = self._voltage_to_ntu(voltage)
        return round(voltage, 4), round(ntu, 2)