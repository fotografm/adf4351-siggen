#!/usr/bin/env python3
"""
siggen_gui.py — PyQt5 desktop controller for ADF4351 signal generator.
Target: Pico 2W running pico/main.py, connected via USB at /dev/ttyACM0.

Setup (run once):
    python3 -m venv .venv
    source .venv/bin/activate
    pip install pyserial PyQt5
    python desktop/siggen_gui.py

If you see a permissions error on /dev/ttyACM0, ensure your user is in the
dialout group:  sudo usermod -aG dialout $USER  (then log out and back in).
"""

import sys
import queue
import serial
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit,
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QFont

# ── Configuration ─────────────────────────────────────────────────────────────

PORT       = "/dev/ttyACM0"
BAUD       = 115200
FREQ_MIN   = 144_000_000
FREQ_MAX   = 1_300_000_000
POLL_MS    = 2000

STEPS = [
    ("12.5 kHz",   12_500),
    ("100 kHz",   100_000),
    ("1 MHz",   1_000_000),
    ("10 MHz", 10_000_000),
]

POWERS = [
    ("-4 dBm", 0),
    ("-1 dBm", 1),
    ("+2 dBm", 2),
    ("+5 dBm", 3),
]

# ── Colour palette ────────────────────────────────────────────────────────────

BG         = "#12122a"
FREQ_BG    = "#080818"
FREQ_FG    = "#00ff88"
UP_N       = "#22aa44";  UP_H   = "#33ee66"
DN_N       = "#cc3311";  DN_H   = "#ff5533"
STEP_SEL   = "#2255dd";  STEP_U = "#1a2a44"
PWR_SEL    = "#cc8800";  PWR_U  = "#1a2a44"
RF_ON      = "#009966";  RF_OFF = "#882222"
CON_N      = "#1a5533";  CON_H  = "#227744"
DIS_N      = "#553311";  DIS_H  = "#885522"
LOCK_G     = "#00dd55"
LOCK_R     = "#ee2222"
FG         = "#ffffff"
DIM        = "#7799aa"


def _css(n, h, fg=FG):
    return (f"QPushButton{{background:{n};color:{fg};border-radius:5px;border:none;padding:4px 8px;}}"
            f"QPushButton:hover{{background:{h};}}"
            f"QPushButton:pressed{{background:{n};}}")


# ── Serial thread ─────────────────────────────────────────────────────────────

class SerialThread(QThread):
    """Owns the serial port; drains a command queue and emits responses."""

    response   = pyqtSignal(str, str)   # (command, response)
    port_error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._ser   = None
        self._q     = queue.Queue()
        self._alive = True

    # ── API called from GUI thread ────────────────────────────────────────────

    def open(self, port: str) -> bool:
        try:
            self._ser = serial.Serial(port, baudrate=BAUD, timeout=1.0)
            self._ser.reset_input_buffer()
            return True
        except serial.SerialException as exc:
            self.port_error.emit(str(exc))
            return False

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    def send(self, cmd: str):
        self._q.put(cmd)

    def stop(self):
        self._alive = False
        self.wait(2000)

    # ── Thread body ───────────────────────────────────────────────────────────

    def run(self):
        while self._alive:
            try:
                cmd = self._q.get(timeout=0.1)
            except queue.Empty:
                continue

            if self._ser is None or not self._ser.is_open:
                continue

            try:
                self._ser.reset_input_buffer()
                self._ser.write((cmd + "\n").encode())
                self._ser.flush()

                # Firmware echoes "> CMD\r\n" then the real response line.
                resp = ""
                for _ in range(2):
                    raw = self._ser.readline().decode(errors="replace").strip()
                    if raw.startswith(">"):
                        continue
                    resp = raw
                    break

                self.response.emit(cmd, resp)

            except serial.SerialException as exc:
                self.port_error.emit(str(exc))
                self._ser = None


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ADF4351 Signal Generator")
        self.setMinimumWidth(640)

        self._freq      = 446_100_000
        self._power_idx = 3
        self._rf_on     = True
        self._connected = False
        self._step_idx  = 0
        self._step_btns: list[QPushButton] = []
        self._pwr_btns:  list[QPushButton] = []

        self._thread = SerialThread()
        self._thread.response.connect(self._on_response)
        self._thread.port_error.connect(self._on_error)
        self._thread.start()

        self._poll = QTimer()
        self._poll.timeout.connect(lambda: self._thread.send("STATUS"))

        self._build_ui()
        self.setStyleSheet(f"QMainWindow,QWidget{{background:{BG};color:{DIM};}}")
        self._refresh_freq_label()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        root   = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(16)
        layout.setContentsMargins(18, 18, 18, 18)
        self.setCentralWidget(root)

        layout.addLayout(self._top_row())
        layout.addWidget(self._freq_widget())
        layout.addLayout(self._step_row())
        layout.addLayout(self._entry_row())
        layout.addLayout(self._power_row())
        layout.addLayout(self._rf_row())
        layout.addStretch()

        self._status = QLabel("Not connected")
        self._status.setFont(QFont("Monospace", 10))
        self._status.setStyleSheet(
            f"color:{DIM};background:#0a0a1a;border-top:1px solid #222244;"
            "padding:6px 10px;"
        )
        layout.addWidget(self._status)

    def _top_row(self):
        row = QHBoxLayout()

        self._lock_lbl = QLabel("● UNLOCKED")
        self._lock_lbl.setFont(QFont("Monospace", 12, QFont.Bold))
        self._set_lock(False)
        row.addWidget(self._lock_lbl)

        row.addStretch()

        self._con_btn = QPushButton()
        self._con_btn.setFont(QFont("Sans", 10, QFont.Bold))
        self._con_btn.setFixedHeight(38)
        self._con_btn.clicked.connect(self._toggle_connect)
        self._refresh_con_btn()
        row.addWidget(self._con_btn)

        return row

    def _freq_widget(self):
        self._freq_lbl = QLabel()
        self._freq_lbl.setAlignment(Qt.AlignCenter)
        self._freq_lbl.setFont(QFont("Monospace", 38, QFont.Bold))
        self._freq_lbl.setMinimumHeight(100)
        self._freq_lbl.setStyleSheet(
            f"color:{FREQ_FG};background:{FREQ_BG};"
            "border:2px solid #003322;border-radius:8px;padding:10px;"
        )
        return self._freq_lbl

    def _step_row(self):
        row = QHBoxLayout()
        row.setSpacing(10)

        self._dn = QPushButton("▼  DOWN")
        self._dn.setFont(QFont("Sans", 12, QFont.Bold))
        self._dn.setFixedSize(140, 52)
        self._dn.setStyleSheet(_css(DN_N, DN_H))
        self._dn.clicked.connect(self._step_down)
        row.addWidget(self._dn)

        row.addStretch()

        lbl = QLabel("Step:")
        lbl.setFont(QFont("Sans", 10))
        row.addWidget(lbl)

        for i, (label, _) in enumerate(STEPS):
            btn = QPushButton(label)
            btn.setFont(QFont("Sans", 10, QFont.Bold))
            btn.setFixedSize(96, 38)
            btn.clicked.connect(lambda _, idx=i: self._select_step(idx))
            self._step_btns.append(btn)
            row.addWidget(btn)
        self._refresh_step_btns()

        row.addStretch()

        self._up = QPushButton("▲  UP")
        self._up.setFont(QFont("Sans", 12, QFont.Bold))
        self._up.setFixedSize(140, 52)
        self._up.setStyleSheet(_css(UP_N, UP_H))
        self._up.clicked.connect(self._step_up)
        row.addWidget(self._up)

        return row

    def _entry_row(self):
        row = QHBoxLayout()
        row.setSpacing(8)

        lbl = QLabel("Enter MHz:")
        lbl.setFont(QFont("Sans", 10))
        row.addWidget(lbl)

        self._entry = QLineEdit()
        self._entry.setFont(QFont("Monospace", 12))
        self._entry.setPlaceholderText("e.g.  446.100")
        self._entry.setFixedWidth(170)
        self._entry.setStyleSheet(
            f"background:#18183a;color:{FG};border:1px solid #334488;"
            "border-radius:4px;padding:4px 10px;"
        )
        self._entry.returnPressed.connect(self._set_from_entry)
        row.addWidget(self._entry)

        set_btn = QPushButton("SET")
        set_btn.setFont(QFont("Sans", 10, QFont.Bold))
        set_btn.setFixedSize(72, 36)
        set_btn.setStyleSheet(_css("#334499", "#5566bb"))
        set_btn.clicked.connect(self._set_from_entry)
        row.addWidget(set_btn)

        row.addStretch()
        return row

    def _power_row(self):
        row = QHBoxLayout()
        row.setSpacing(8)

        lbl = QLabel("Power:")
        lbl.setFont(QFont("Sans", 10))
        row.addWidget(lbl)

        for i, (label, _) in enumerate(POWERS):
            btn = QPushButton(label)
            btn.setFont(QFont("Sans", 10, QFont.Bold))
            btn.setFixedSize(86, 38)
            btn.clicked.connect(lambda _, idx=i: self._select_power(idx))
            self._pwr_btns.append(btn)
            row.addWidget(btn)
        self._refresh_pwr_btns()

        row.addStretch()
        return row

    def _rf_row(self):
        row = QHBoxLayout()

        lbl = QLabel("RF Output:")
        lbl.setFont(QFont("Sans", 10))
        row.addWidget(lbl)

        self._rf_btn = QPushButton()
        self._rf_btn.setFont(QFont("Sans", 12, QFont.Bold))
        self._rf_btn.setFixedSize(120, 44)
        self._rf_btn.clicked.connect(self._toggle_rf)
        self._refresh_rf_btn()
        row.addWidget(self._rf_btn)

        row.addStretch()
        return row

    # ── Visual refresh helpers ────────────────────────────────────────────────

    def _refresh_freq_label(self):
        self._freq_lbl.setText(f"{self._freq / 1e6:.6f} MHz")

    def _refresh_step_btns(self):
        for i, btn in enumerate(self._step_btns):
            if i == self._step_idx:
                btn.setStyleSheet(_css(STEP_SEL, "#3366ff"))
            else:
                btn.setStyleSheet(_css(STEP_U, "#243555"))

    def _refresh_pwr_btns(self):
        for i, btn in enumerate(self._pwr_btns):
            if i == self._power_idx:
                btn.setStyleSheet(_css(PWR_SEL, "#ffbb33"))
            else:
                btn.setStyleSheet(_css(PWR_U, "#243555"))

    def _refresh_rf_btn(self):
        if self._rf_on:
            self._rf_btn.setText("●  RF ON")
            self._rf_btn.setStyleSheet(_css(RF_ON, "#00cc88"))
        else:
            self._rf_btn.setText("○  RF OFF")
            self._rf_btn.setStyleSheet(_css(RF_OFF, "#bb3333"))

    def _refresh_con_btn(self):
        if self._connected:
            self._con_btn.setText("● CONNECTED — DISCONNECT")
            self._con_btn.setStyleSheet(_css(CON_N, CON_H))
        else:
            self._con_btn.setText(f"CONNECT  {PORT}")
            self._con_btn.setStyleSheet(_css(DIS_N, DIS_H))

    def _set_lock(self, locked: bool):
        if locked:
            self._lock_lbl.setText("● LOCKED")
            self._lock_lbl.setStyleSheet(f"color:{LOCK_G};")
        else:
            self._lock_lbl.setText("● UNLOCKED")
            self._lock_lbl.setStyleSheet(f"color:{LOCK_R};")

    def _set_status(self, msg: str):
        self._status.setText(msg)

    # ── Control slots ─────────────────────────────────────────────────────────

    def _toggle_connect(self):
        if self._connected:
            self._poll.stop()
            self._thread.close()
            self._connected = False
            self._set_lock(False)
            self._set_status("Disconnected")
        else:
            if self._thread.open(PORT):
                self._connected = True
                self._set_status(f"Connected to {PORT}")
                self._thread.send("STATUS")
                self._poll.start(POLL_MS)
        self._refresh_con_btn()

    def _select_step(self, idx: int):
        self._step_idx = idx
        self._refresh_step_btns()

    def _select_power(self, idx: int):
        self._power_idx = idx
        self._refresh_pwr_btns()
        if self._connected:
            self._send(f"PWR:{POWERS[idx][1]}")

    def _step_up(self):
        step = STEPS[self._step_idx][1]
        self._apply_freq(min(self._freq + step, FREQ_MAX))

    def _step_down(self):
        step = STEPS[self._step_idx][1]
        self._apply_freq(max(self._freq - step, FREQ_MIN))

    def _set_from_entry(self):
        text = self._entry.text().strip()
        try:
            freq = int(round(float(text) * 1_000_000))
        except ValueError:
            self._set_status("Bad frequency — enter a value in MHz, e.g. 446.100")
            return
        if not FREQ_MIN <= freq <= FREQ_MAX:
            lo = FREQ_MIN / 1e6
            hi = FREQ_MAX / 1e6
            self._set_status(f"Out of range — {lo:.0f} – {hi:.0f} MHz")
            return
        self._apply_freq(freq)

    def _toggle_rf(self):
        self._rf_on = not self._rf_on
        self._refresh_rf_btn()
        if self._connected:
            self._send(f"RF:{'ON' if self._rf_on else 'OFF'}")

    def _apply_freq(self, freq: int):
        self._freq = freq
        self._refresh_freq_label()
        if self._connected:
            self._send(f"FREQ:{freq}")

    def _send(self, cmd: str):
        self._set_status(f"→  {cmd}")
        self._thread.send(cmd)

    # ── Response handler ──────────────────────────────────────────────────────

    def _on_response(self, cmd: str, resp: str):
        self._set_status(f"{cmd}   →   {resp}")

        if resp.startswith("STATUS:"):
            self._parse_status(resp[7:])
        elif resp.startswith("OK:"):
            self._set_lock(resp == "OK:LOCKED")
        # ERR: already visible in the status label

    def _parse_status(self, body: str):
        """Parse  FREQ=…,PWR=…,RF=…,LOCKED|UNLOCKED  from STATUS reply body."""
        fields = dict(p.split("=", 1) for p in body.split(",") if "=" in p)
        lock_word = body.rsplit(",", 1)[-1]
        self._set_lock(lock_word == "LOCKED")

        if "FREQ" in fields:
            try:
                self._freq = int(fields["FREQ"])
                self._refresh_freq_label()
            except ValueError:
                pass

        if "PWR" in fields:
            try:
                pwr = int(fields["PWR"])
                if 0 <= pwr <= 3 and pwr != self._power_idx:
                    self._power_idx = pwr
                    self._refresh_pwr_btns()
            except ValueError:
                pass

        if "RF" in fields and (fields["RF"] == "ON") != self._rf_on:
            self._rf_on = fields["RF"] == "ON"
            self._refresh_rf_btn()

    def _on_error(self, msg: str):
        self._connected = False
        self._poll.stop()
        self._set_lock(False)
        self._refresh_con_btn()
        self._set_status(f"Serial error: {msg}")

    def closeEvent(self, event):
        self._poll.stop()
        self._thread.close()
        self._thread.stop()
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
