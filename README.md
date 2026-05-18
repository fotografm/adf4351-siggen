# adf4351-siggen

Wideband signal generator using an ADF4351 PLL synthesiser controlled by a
Raspberry Pi Pico 2W over USB serial.

## Overview

The ADF4351 covers 35 MHz – 4400 MHz with 10 Hz resolution, making it useful
as a general-purpose RF signal source for lab work, SDR calibration, and
filter alignment.  The Pico 2W handles SPI register writes to the ADF4351 and
exposes a simple serial protocol over USB CDC, which the desktop GUI uses to
set frequency, output power, and enable/disable the RF output.

## Repository Layout

```
pico/       MicroPython firmware for the Raspberry Pi Pico 2W
desktop/    Linux Mint Python GUI (tkinter / PyQt)
docs/       Wiring diagrams, pin assignments, register notes
```

## Hardware

| Component         | Notes                                      |
|-------------------|--------------------------------------------|
| ADF4351 module    | Common breakout with onboard LDO and TCXO |
| Raspberry Pi Pico 2W | USB CDC serial, SPI master             |
| Reference clock   | Module onboard 25 MHz TCXO (typical)       |

## Planned Features

- [x] USB serial control from desktop GUI
- [ ] Frequency sweep / marker mode
- [ ] SoftAP web interface served from the Pico 2W (no desktop app required)
- [ ] Preset memory stored in Pico flash

## Quick Start

1. Flash `pico/main.py` to the Pico 2W with `mpremote` or Thonny.
2. Connect the ADF4351 module per `docs/wiring.md`.
3. Run `desktop/siggen_gui.py` — the GUI auto-detects the Pico's USB serial port.

## Serial Protocol

Commands are newline-terminated ASCII strings sent to the Pico's USB CDC port.

| Command          | Example            | Description                    |
|------------------|--------------------|--------------------------------|
| `FREQ <hz>`      | `FREQ 433920000`   | Set output frequency in Hz     |
| `PWR <0-3>`      | `PWR 3`            | Set output power (0 = –4 dBm)  |
| `RF <on\|off>`   | `RF on`            | Enable / disable RF output     |
| `STATUS`         | `STATUS`           | Query current settings         |

## License

MIT
