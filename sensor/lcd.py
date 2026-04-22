try:
    from RPLCD.i2c import CharLCD
    _HW_AVAILABLE = True
except ImportError:
    _HW_AVAILABLE = False


class LCDDisplay:
    def __init__(self, address: int = 0x27, cols: int = 16, rows: int = 2):
        self._lcd = None
        if _HW_AVAILABLE:
            try:
                self._lcd = CharLCD(
                    i2c_expander="PCF8574",
                    address=address,
                    port=1,
                    cols=cols,
                    rows=rows,
                    dotsize=8,
                )
                self._lcd.clear()
                self._lcd.write_string("AquaSense Ready")
                print(f"[LCD] Initialised at 0x{address:02X}.")
            except Exception as e:
                print(f"[LCD] Init failed – display disabled: {e}")
        else:
            print("[LCD] RPLCD not installed – display disabled.")

    def display(self, ntu: float, status: str):
        if self._lcd is None:
            return
        try:
            self._lcd.clear()
            self._lcd.write_string(f"NTU: {ntu:.1f}")
            self._lcd.cursor_pos = (1, 0)
            self._lcd.write_string(f"Status: {status:<8}")
        except Exception as e:
            print(f"[LCD] Write error: {e}")

    def clear(self):
        if self._lcd:
            try:
                self._lcd.clear()
            except Exception:
                pass