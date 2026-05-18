# pico/main.py — ADF4351 wideband signal generator firmware
# Target: Raspberry Pi Pico 2W (RP2350) / MicroPython
#
# Wiring
#   GP2  SCK  → ADF4351 CLK
#   GP3  MOSI → ADF4351 DATA
#   GP4  LE   → ADF4351 LE   (rising edge latches each 32-bit word)
#   GP5  CE   → ADF4351 CE   (keep high = device enabled)
#   GP6  LD   ← ADF4351 LD   (digital lock detect; high = locked)
#
# USB serial command set (newline-terminated, case-insensitive)
#   FREQ:<hz>    set output frequency in Hz
#   PWR:<0-3>    output power  0=−4 dBm  1=−1 dBm  2=+2 dBm  3=+5 dBm
#   RF:ON|OFF    enable / disable RF output
#   STATUS       query current settings
#
# Responses
#   OK:LOCKED | OK:UNLOCKED         — command accepted
#   ERR:<reason>                    — command rejected
#   STATUS:FREQ=…,PWR=…,RF=…,…     — status reply (includes lock state)

import sys
import time
from machine import Pin


# ── Bit-bang SPI (write-only; ADF4351 has no data output) ───────────────────

_sck  = Pin(2, Pin.OUT, value=0)
_mosi = Pin(3, Pin.OUT, value=0)
_le   = Pin(4, Pin.OUT, value=0)
_ce   = Pin(5, Pin.OUT, value=1)   # CE high = device on
_ld   = Pin(6, Pin.IN)


def _write_reg(val):
    """Clock 32 bits MSB-first into ADF4351, then raise LE to latch."""
    _le.low()
    for bit in range(31, -1, -1):
        _sck.low()
        _mosi.value((val >> bit) & 1)
        _sck.high()
    _sck.low()
    _le.high()          # rising edge latches the register
    time.sleep_us(1)
    _le.low()


def _write_all(regs):
    """Full boot load: R5 → R0.  ADF4351 requires this order on power-up."""
    for i in range(5, -1, -1):
        _write_reg(regs[i])
        time.sleep_us(20)


# ── Fixed PLL parameters ─────────────────────────────────────────────────────

_PFD      = 25_000_000   # phase-freq detector = REF 25 MHz / R 1
_MOD      = 2000         # modulus → step = 25 MHz / 2000 = 12.5 kHz
_CP_IDX   = 7            # charge-pump index 7 = 2.50 mA
_BAND_DIV = 200          # band-select clock = PFD / 200 = 125 kHz (datasheet max)

# ── Mutable state ────────────────────────────────────────────────────────────

_freq  = 144_000_000
_power = 3
_rf_on = True
_regs  = [0] * 6         # shadow of the last register set written


# ── Register calculation ─────────────────────────────────────────────────────

def _output_divider(freq):
    """Return (divisor_value, RF_DIVIDER_SELECT) or raise ValueError.

    Walks div-select 0-6 (÷1 … ÷64) and returns the first that places the
    VCO inside its 2200–4400 MHz operating window.
    """
    for sel in range(7):
        d   = 1 << sel
        vco = freq * d
        if 2_200_000_000 <= vco <= 4_400_000_000:
            return d, sel
    raise ValueError("frequency cannot be placed in VCO range 2200–4400 MHz")


def _build(freq, power, rf_on):
    """Return (r0, r1, r2, r3, r4, r5) or None if freq is not achievable."""
    try:
        out_div, div_sel = _output_divider(freq)
    except ValueError:
        return None

    vco  = freq * out_div
    n    = vco / _PFD
    INT  = int(n)
    FRAC = round((n - INT) * _MOD)
    if FRAC >= _MOD:        # rounding can push FRAC to MOD boundary
        FRAC  = 0
        INT  += 1

    if not (75 <= INT <= 65535):   # 8/9 prescaler requires INT ≥ 75
        return None

    rf = 1 if rf_on else 0

    # R0 — INT[31:15], FRAC[14:3], CTRL[2:0]=000
    r0 = (INT << 15) | (FRAC << 3) | 0

    # R1 — prescaler 8/9 at bit 27, phase word=1, MOD, CTRL=001
    r1 = (1 << 27) | (1 << 15) | (_MOD << 3) | 1

    # R2 — noise mode, MUXOUT, R divider, CP, PD polarity, CTRL=010
    r2  = (0b00  << 29)    # low-noise mode
    r2 |= (0b110 << 26)    # MUXOUT = digital lock detect (probing aid)
    r2 |= (0     << 25)    # REF_DOUBLER = off
    r2 |= (0     << 24)    # RDIV2 = off
    r2 |= (1     << 14)    # R counter = 1
    r2 |= (0     << 13)    # double-buffer R = off
    r2 |= (_CP_IDX << 9)   # charge-pump current
    r2 |= (0     << 8)     # LDF = frac-N mode
    r2 |= (0     << 7)     # LDP = 10 ns
    r2 |= (1     << 6)     # PD polarity = positive (passive loop filter)
    r2 |= (0     << 5)     # power-down = no
    r2 |= (0     << 4)     # CP three-state = no
    r2 |= (0     << 3)     # counter reset = no
    r2 |= 2

    # R3 — CLK_DIV_MODE=off; CLK_DIV value is don't-care when mode=0, CTRL=011
    r3 = (0 << 15) | (150 << 3) | 3

    # R4 — fundamental feedback, output divider, band-select, RF, power, CTRL=100
    r4  = (1         << 23)   # feedback = fundamental (lower phase noise vs divided)
    r4 |= (div_sel   << 20)   # RF_DIVIDER_SELECT[22:20]
    r4 |= (_BAND_DIV << 12)   # BAND_SELECT_CLOCK_DIVIDER_VALUE[19:12]
    r4 |= (0         << 11)   # VCO power-down = no
    r4 |= (0         << 10)   # MTLD = off
    r4 |= (0         << 9)    # AUX_OUTPUT_SELECT (don't care, AUX disabled)
    r4 |= (0         << 8)    # AUX_OUTPUT_ENABLE = off
    r4 |= (0         << 6)    # AUX_OUTPUT_POWER (don't care)
    r4 |= (rf        << 5)    # RF_OUTPUT_ENABLE
    r4 |= (power     << 3)    # OUTPUT_POWER[4:3]
    r4 |= 4

    # R5 — LD_PIN_MODE=digital-lock-detect; DB[23:22]=01, DB[20:19]=11 (reserved=1)
    r5 = (0b01 << 22) | (1 << 20) | (1 << 19) | 5

    return r0, r1, r2, r3, r4, r5


# ── Helpers ──────────────────────────────────────────────────────────────────

def _locked():
    return "LOCKED" if _ld.value() else "UNLOCKED"


def _ok():
    # Poll LD for up to 50 ms; ADF4351 typically locks in 5-20 ms
    deadline = time.ticks_add(time.ticks_ms(), 50)
    while not _ld.value():
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            break
        time.sleep_ms(1)
    print("OK:" + _locked())


# ── Command handlers ─────────────────────────────────────────────────────────

def _cmd_freq(arg):
    global _freq, _regs
    try:
        freq = int(arg)
    except ValueError:
        print("ERR:not an integer")
        return
    if freq <= 0:
        print("ERR:frequency must be positive")
        return

    regs = _build(freq, _power, _rf_on)
    if regs is None:
        print("ERR:frequency out of range (approx 34-4400 MHz)")
        return

    _freq = freq
    _regs = list(regs)

    # Full R5→R0 write guarantees correct divider order and re-arms band-select
    _write_all(regs)
    _ok()


def _cmd_pwr(arg):
    global _power, _regs
    try:
        pwr = int(arg)
    except ValueError:
        print("ERR:not an integer")
        return
    if not 0 <= pwr <= 3:
        print("ERR:power must be 0-3")
        return

    _power = pwr
    regs   = _build(_freq, _power, _rf_on)
    _regs  = list(regs)
    _write_reg(regs[4])   # output power lives in R4
    _ok()


def _cmd_rf(arg):
    global _rf_on, _regs
    if arg == "ON":
        _rf_on = True
    elif arg == "OFF":
        _rf_on = False
    else:
        print("ERR:RF must be ON or OFF")
        return

    regs  = _build(_freq, _power, _rf_on)
    _regs = list(regs)
    _write_reg(regs[4])   # RF_OUTPUT_ENABLE is bit 5 of R4
    _ok()


def _cmd_status(_arg):
    print("STATUS:FREQ={},PWR={},RF={},{}".format(
        _freq, _power, "ON" if _rf_on else "OFF", _locked()
    ))


_CMDS = {
    "FREQ":   _cmd_freq,
    "PWR":    _cmd_pwr,
    "RF":     _cmd_rf,
    "STATUS": _cmd_status,
}


def _dispatch(line):
    line = line.strip()
    if not line:
        return
    print("> " + line)
    upper = line.upper()
    if ":" in upper:
        cmd, arg = upper.split(":", 1)
    else:
        cmd, arg = upper, ""

    fn = _CMDS.get(cmd)
    if fn is None:
        print("ERR:unknown command")
    else:
        fn(arg)


# ── Boot sequence ─────────────────────────────────────────────────────────────

_regs = list(_build(_freq, _power, _rf_on))
_ce.high()
_write_all(_regs)
time.sleep_ms(10)   # allow PLL to acquire lock before accepting commands


# ── Main loop ─────────────────────────────────────────────────────────────────

while True:
    line = sys.stdin.readline()   # blocks until \n; reliable over USB CDC
    _dispatch(line)
