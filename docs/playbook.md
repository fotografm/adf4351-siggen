# ADF4351 Signal Generator — Project Playbook

Wideband signal generator using an ADF4351 PLL module controlled by a
Raspberry Pi Pico 2W (RP2350), with a PyQt5 desktop GUI over USB serial.

---

## Hardware

### Parts
- ADF4351 breakout board (e.g. eBay/AliExpress module)
- Raspberry Pi Pico 2W (RP2350)
- 25 MHz TCXO (±1 ppm or better) — replaces onboard crystal for frequency stability
- Passive loop filter (fitted on most ADF4351 modules already)

### Wiring — Pico 2W → ADF4351

| Pico GPIO        | Pico pin | ADF4351 header | ADF4351 pin | Notes                            |
|------------------|----------|----------------|-------------|----------------------------------|
| GP2 (SPI0 SCK)   | 4        | CLK            | 4           | SPI clock                        |
| GP3 (SPI0 TX)    | 5        | DAT            | 5           | SPI data                         |
| GP4 (GPIO out)   | 6        | LE             | 6           | Latch enable, toggled by firmware |
| GP5 (GPIO out)   | 7        | CE             | 8           | Pull HIGH to enable chip         |
| GP6 (GPIO in)    | 9        | LD             | 2           | Lock detect input                |
| GND              | 38       | GND            | 10          |                                  |
| 3V3              | 36       | PDR            | 1           | Tie PDR high to keep chip running |
| —                | —        | VCC            | —           | **Separate 5 V PSU — never power the ADF4351 board from the Pico** |

LD is the digital lock-detect output from the ADF4351; read on GP6.

### TCXO mod
Desolder or bypass the onboard crystal. Connect the TCXO output to the
ADF4351 `REF_IN` pin. Supply the TCXO from 3V3. Without this mod,
frequency drift of tens of kHz over temperature is normal.

---

## Firmware (MicroPython on Pico 2W)

### First-time MicroPython install
Download the RP2350 `.uf2` from micropython.org. Hold BOOTSEL, plug USB,
drop the file onto the `RPI-RP2` drive. Pico reboots into MicroPython.

### Flash the firmware
```bash
sudo mpremote cp pico/main.py :main.py
sudo mpremote reset
```

### Test over serial REPL
```bash
sudo mpremote repl
```
Commands (newline-terminated, case-insensitive):

| Command        | Description                        | Response          |
|----------------|------------------------------------|-------------------|
| `FREQ:<hz>`    | Set frequency in Hz                | `OK:LOCKED` / `OK:UNLOCKED` |
| `PWR:<0-3>`    | Output power (0=−4, 1=−1, 2=+2, 3=+5 dBm) | `OK:…`   |
| `RF:ON\|OFF`   | Enable / disable RF output         | `OK:…`            |
| `STATUS`       | Query all state                    | `STATUS:FREQ=…,PWR=…,RF=…,LOCKED\|UNLOCKED` |

Exit REPL: **Ctrl+]**

### Frequency range
- GUI limits: 144 MHz – 1300 MHz
- Hardware capable of ~34 MHz – 4400 MHz (firmware will reject out-of-range)
- VCO runs 2200–4400 MHz; output divider (÷1 … ÷64) brings it down
- PFD = 25 MHz, MOD = 2000 → minimum step = 12.5 kHz

### PLL parameters (pico/main.py)
```python
_PFD      = 25_000_000   # reference / R-divider
_MOD      = 2000         # fractional modulus → 12.5 kHz steps
_CP_IDX   = 7            # charge-pump 2.50 mA
_BAND_DIV = 200          # band-select clock = 125 kHz (datasheet max)
```
Change `_PFD` if using a 10 MHz reference (set to `10_000_000`).

---

## Desktop GUI

### One-time setup
```bash
cd ~/repos/adf4351-siggen
python3 -m venv --system-site-packages .venv
.venv/bin/pip install pyserial
```
Requires system packages (install once with sudo):
```bash
sudo apt install python3.12-venv python3-pyqt5
```

### Run
```bash
cd ~/repos/adf4351-siggen
source .venv/bin/activate
python desktop/siggen_gui.py
```

### GUI features
- Large frequency display in MHz
- Step size: 12.5 kHz / 100 kHz / 1 MHz / 10 MHz
- ▲ UP / ▼ DOWN step buttons
- Direct MHz entry field (press Enter or SET)
- Power selector: −4 / −1 / +2 / +5 dBm
- RF ON/OFF toggle
- Lock detect indicator — green = locked, red = unlocked
- Status bar shows every command and response
- Polls `STATUS` every 2 seconds to sync lock state

### Serial port
Default: `/dev/ttyACM0`. User must be in the `dialout` group:
```bash
sudo usermod -aG dialout $USER   # then log out and back in
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `OK:UNLOCKED` after FREQ command | PLL needs time to acquire lock | Firmware polls LD for 50 ms — if still unlocked, check loop filter and VCO range |
| Frequency drifts with temperature | Onboard crystal, poor stability | Replace with 25 MHz TCXO on REF_IN |
| `/dev/ttyACM0` permission denied | User not in dialout group | `sudo usermod -aG dialout $USER` + re-login |
| GUI blank / won't start | PyQt5 missing | `sudo apt install python3-pyqt5` |
| `ERR:frequency out of range` | Outside ~34–4400 MHz | GUI enforces 144–1300 MHz; adjust `FREQ_MIN/MAX` in siggen_gui.py |
| Pico not detected | USB issue | Unplug/replug; check `lsusb` for `2e8a:0005` |

---

## Repo layout

```
adf4351-siggen/
├── pico/
│   └── main.py          # MicroPython firmware
├── desktop/
│   └── siggen_gui.py    # PyQt5 GUI
└── docs/
    └── playbook.md      # this file
```
