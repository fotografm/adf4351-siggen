# pico/main.py — ADF4351 signal generator firmware
# Pico 2W (RP2350) / MicroPython
#
# Two control paths run concurrently via uasyncio:
#   USB serial   — same command set as before (mpremote repl / miniterm)
#   WiFi AP      — mobile web interface at http://10.42.0.1
#
# WiFi: SSID=ADF4351  password=reticulum
#
# Wiring
#   GP2  SCK  → ADF4351 CLK
#   GP3  MOSI → ADF4351 DATA
#   GP4  LE   → ADF4351 LE
#   GP5  CE   → ADF4351 CE  (keep high)
#   GP6  LD   ← ADF4351 LD  (lock detect)

import sys
import time
import select
import network
import uasyncio as asyncio
from machine import Pin


# ── WiFi AP config ────────────────────────────────────────────────────────────

AP_SSID = "ADF4351"
AP_PASS = "reticulum"
AP_IP   = "192.168.4.1"
AP_MASK = "255.255.255.0"


# ── Bit-bang SPI ──────────────────────────────────────────────────────────────

_sck  = Pin(2, Pin.OUT, value=0)
_mosi = Pin(3, Pin.OUT, value=0)
_le   = Pin(4, Pin.OUT, value=0)
_ce   = Pin(5, Pin.OUT, value=1)
_ld   = Pin(6, Pin.IN)


def _write_reg(val):
    _le.low()
    for bit in range(31, -1, -1):
        _sck.low()
        _mosi.value((val >> bit) & 1)
        _sck.high()
    _sck.low()
    _le.high()
    time.sleep_us(1)
    _le.low()


def _write_all(regs):
    """Full R5 → R0 load (required order on power-up and freq changes)."""
    for i in range(5, -1, -1):
        _write_reg(regs[i])
        time.sleep_us(20)


# ── PLL parameters ────────────────────────────────────────────────────────────

_PFD      = 25_000_000
_MOD      = 2000
_CP_IDX   = 7
_BAND_DIV = 200

# ── Mutable state ─────────────────────────────────────────────────────────────

_freq  = 144_000_000
_power = 3
_rf_on = True
_regs  = [0] * 6


# ── Register calculation ──────────────────────────────────────────────────────

def _output_divider(freq):
    for sel in range(7):
        d   = 1 << sel
        vco = freq * d
        if 2_200_000_000 <= vco <= 4_400_000_000:
            return d, sel
    raise ValueError


def _build(freq, power, rf_on):
    try:
        out_div, div_sel = _output_divider(freq)
    except ValueError:
        return None

    vco  = freq * out_div
    n    = vco / _PFD
    INT  = int(n)
    FRAC = round((n - INT) * _MOD)
    if FRAC >= _MOD:
        FRAC = 0
        INT += 1

    if not (75 <= INT <= 65535):
        return None

    rf = 1 if rf_on else 0

    r0 = (INT << 15) | (FRAC << 3)
    r1 = (1 << 27) | (1 << 15) | (_MOD << 3) | 1
    r2  = (0b110 << 26) | (1 << 14) | (_CP_IDX << 9) | (1 << 6) | 2
    r3  = (150 << 3) | 3
    r4  = (1 << 23) | (div_sel << 20) | (_BAND_DIV << 12) | (rf << 5) | (power << 3) | 4
    r5  = (0b01 << 22) | (1 << 20) | (1 << 19) | 5

    return r0, r1, r2, r3, r4, r5


# ── Command execution (always returns a response string, never prints) ────────

def _wait_lock():
    deadline = time.ticks_add(time.ticks_ms(), 50)
    while not _ld.value():
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            break
        time.sleep_ms(5)
    return "OK:" + ("LOCKED" if _ld.value() else "UNLOCKED")


def _cmd_freq(arg):
    global _freq, _regs
    try:
        freq = int(arg)
    except ValueError:
        return "ERR:not an integer"
    if freq <= 0:
        return "ERR:frequency must be positive"
    regs = _build(freq, _power, _rf_on)
    if regs is None:
        return "ERR:frequency out of range (approx 34-4400 MHz)"
    _freq = freq
    _regs = list(regs)
    _write_all(regs)
    return _wait_lock()


def _cmd_pwr(arg):
    global _power, _regs
    try:
        pwr = int(arg)
    except ValueError:
        return "ERR:not an integer"
    if not 0 <= pwr <= 3:
        return "ERR:power must be 0-3"
    _power = pwr
    regs   = _build(_freq, _power, _rf_on)
    _regs  = list(regs)
    _write_reg(regs[4])
    return _wait_lock()


def _cmd_rf(arg):
    global _rf_on, _regs
    if arg == "ON":
        _rf_on = True
    elif arg == "OFF":
        _rf_on = False
    else:
        return "ERR:RF must be ON or OFF"
    regs  = _build(_freq, _power, _rf_on)
    _regs = list(regs)
    _write_reg(regs[4])
    return _wait_lock()


def _cmd_status(_arg):
    return "STATUS:FREQ={},PWR={},RF={},WIFI={},{}".format(
        _freq, _power, "ON" if _rf_on else "OFF",
        _wifi_status,
        "LOCKED" if _ld.value() else "UNLOCKED"
    )


_wifi_status = "starting"

_CMDS = {"FREQ": _cmd_freq, "PWR": _cmd_pwr, "RF": _cmd_rf, "STATUS": _cmd_status}


def _execute(line):
    line = line.strip()
    if not line:
        return ""
    upper = line.upper()
    cmd, arg = upper.split(":", 1) if ":" in upper else (upper, "")
    fn = _CMDS.get(cmd)
    return fn(arg) if fn else "ERR:unknown command"


# ── Embedded web page ─────────────────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#12122a">
<title>ADF4351</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#12122a;color:#fff;font-family:sans-serif;padding:14px;max-width:480px;margin:0 auto}
h1{color:#aabbcc;font-size:1.3em;margin-bottom:8px}
.freq{font-size:2.1em;font-weight:bold;color:#00ff88;background:#080818;border:2px solid #003322;border-radius:8px;padding:14px;text-align:center;margin:8px 0}
.lk{font-weight:bold;font-size:1em;margin-bottom:2px}
.on{color:#00dd55}.off{color:#ee2222}
.lbl{color:#7799aa;font-size:.85em;margin:10px 0 4px}
.row{display:flex;gap:6px;margin-bottom:6px;flex-wrap:wrap}
button{flex:1;padding:12px 4px;border:none;border-radius:6px;font-weight:bold;font-size:.9em;cursor:pointer;color:#fff;min-width:52px;touch-action:manipulation}
.up{background:#22aa44}.dn{background:#cc3311}
.step{background:#1a2a44}.step.sel{background:#2255dd}
.ps{background:#1a2a44}.ps.psel{background:#cc8800}
.ron{background:#009966;max-width:160px}.rof{background:#882222;max-width:160px}
.setb{background:#334499;max-width:65px;flex:0 0 65px}
input{background:#18183a;color:#fff;border:1px solid #334488;border-radius:4px;padding:9px;font-size:1em;flex:1;min-width:0;width:100%}
.st{color:#7799aa;font-size:.78em;margin-top:10px;word-break:break-all;min-height:1.2em}
</style></head><body>
<h1>ADF4351 Sig Gen</h1>
<div class="lk off" id="lk">&#9679; UNLOCKED</div>
<div class="freq" id="freq">--- MHz</div>
<div class="lbl">Step size</div>
<div class="row" id="sr">
<button class="step sel" onclick="ss(0)">12.5kHz</button>
<button class="step" onclick="ss(1)">100kHz</button>
<button class="step" onclick="ss(2)">1MHz</button>
<button class="step" onclick="ss(3)">10MHz</button>
</div>
<div class="row">
<button class="dn" onclick="go(-1)">&#9660; DOWN</button>
<button class="up" onclick="go(1)">&#9650; UP</button>
</div>
<div class="lbl">Direct entry (MHz)</div>
<div class="row">
<input type="number" id="mhz" step="0.0125" placeholder="446.100">
<button class="setb" onclick="sd()">SET</button>
</div>
<div class="lbl">Output power</div>
<div class="row" id="pr">
<button class="ps" onclick="sp(0)">-4dBm</button>
<button class="ps" onclick="sp(1)">-1dBm</button>
<button class="ps" onclick="sp(2)">+2dBm</button>
<button class="ps psel" onclick="sp(3)">+5dBm</button>
</div>
<div class="lbl">RF output</div>
<div class="row">
<button class="ron" id="rfb" onclick="tr()">&#9679; RF ON</button>
</div>
<div class="st" id="st">Connecting...</div>
<script>
const S=[12500,100000,1000000,10000000];
let f=0,si=0,pi=3,rf=true;
const $=id=>document.getElementById(id);
function cmd(c){
  fetch('/cmd',{method:'POST',body:c})
  .then(r=>r.text()).then(t=>$('st').textContent=c+' -> '+t)
  .catch(()=>$('st').textContent='No response - retry in 2s');
}
function ss(i){si=i;[...$('sr').children].forEach((b,j)=>b.classList.toggle('sel',i==j));}
function go(d){
  if(!f)return;
  f=Math.max(144000000,Math.min(1300000000,f+d*S[si]));
  $('freq').textContent=(f/1e6).toFixed(6)+' MHz';cmd('FREQ:'+f);
}
function sd(){
  let v=parseFloat($('mhz').value);if(isNaN(v))return;
  let n=Math.round(v*1e6);
  if(n<144000000||n>1300000000){$('st').textContent='Range: 144-1300 MHz';return;}
  f=n;$('freq').textContent=(f/1e6).toFixed(6)+' MHz';cmd('FREQ:'+f);
}
function sp(p){
  pi=p;[...$('pr').children].forEach((b,j)=>b.classList.toggle('psel',p==j));cmd('PWR:'+p);
}
function tr(){
  rf=!rf;let b=$('rfb');
  b.textContent=rf?'● RF ON':'○ RF OFF';
  b.className=rf?'ron':'rof';cmd('RF:'+(rf?'ON':'OFF'));
}
function poll(){
  fetch('/status').then(r=>r.json()).then(d=>{
    f=d.freq;$('freq').textContent=(d.freq/1e6).toFixed(6)+' MHz';
    let l=$('lk');l.textContent=d.locked?'● LOCKED':'● UNLOCKED';
    l.className='lk '+(d.locked?'on':'off');
    if(d.power!=pi){pi=d.power;[...$('pr').children].forEach((b,j)=>b.classList.toggle('psel',pi==j));}
    if(d.rf!=rf){rf=d.rf;let b=$('rfb');b.textContent=rf?'● RF ON':'○ RF OFF';b.className=rf?'ron':'rof';}
  }).catch(()=>{});
}
setInterval(poll,2000);poll();
</script></body></html>"""


# ── HTTP server ───────────────────────────────────────────────────────────────

async def _http_handler(reader, writer):
    try:
        req_line = (await reader.readline()).decode()
        parts = req_line.split()
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1]

        clen = 0
        while True:
            h = (await reader.readline()).decode().strip()
            if not h:
                break
            if h.lower().startswith("content-length:"):
                clen = int(h.split(":", 1)[1].strip())

        body = ""
        if method == "POST" and clen > 0:
            body = (await reader.read(clen)).decode().strip()

        if path in ("/", "/index.html"):
            data = _HTML.encode()
            writer.write("HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\n\r\n".format(len(data)).encode())
            writer.write(data)

        elif path == "/status":
            data = '{{"freq":{},"power":{},"rf":{},"locked":{}}}'.format(
                _freq, _power,
                "true" if _rf_on else "false",
                "true" if _ld.value() else "false"
            ).encode()
            writer.write("HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n".format(len(data)).encode())
            writer.write(data)

        elif path == "/cmd" and method == "POST":
            resp = _execute(body)
            data = resp.encode()
            writer.write("HTTP/1.0 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\n\r\n".format(len(data)).encode())
            writer.write(data)

        else:
            writer.write(b"HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\n\r\n")

        await writer.drain()

    except Exception:
        pass
    finally:
        writer.close()


# ── USB serial task ───────────────────────────────────────────────────────────

async def _usb_task():
    buf    = ""
    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
    while True:
        if poller.poll(0):
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                if buf:
                    print("> " + buf)
                    resp = _execute(buf)
                    if resp:
                        print(resp)
                    buf = ""
                await asyncio.sleep_ms(0)
            else:
                buf += ch
                await asyncio.sleep_ms(0)
        else:
            await asyncio.sleep_ms(10)


# ── WiFi AP + web server task ─────────────────────────────────────────────────

async def _wifi_task():
    global _wifi_status
    try:
        ap = network.WLAN(network.AP_IF)
        ap.active(True)

        for _ in range(50):
            if ap.active():
                break
            await asyncio.sleep_ms(100)

        ap.config(ssid=AP_SSID, password=AP_PASS)
        ap.ifconfig((AP_IP, AP_MASK, AP_IP, AP_IP))
        await asyncio.sleep_ms(200)

        actual_ip = ap.ifconfig()[0]
        _wifi_status = "up:" + actual_ip

        await asyncio.start_server(_http_handler, "0.0.0.0", 80)
        _wifi_status = "listening:" + actual_ip

    except Exception as e:
        _wifi_status = "err:" + str(e)
        return

    while True:
        await asyncio.sleep_ms(1000)


# ── Boot + main ───────────────────────────────────────────────────────────────

async def _main():
    global _regs
    _regs = list(_build(_freq, _power, _rf_on))
    _ce.high()
    _write_all(_regs)
    await asyncio.sleep_ms(10)

    asyncio.create_task(_usb_task())
    asyncio.create_task(_wifi_task())

    while True:
        await asyncio.sleep_ms(1000)


asyncio.run(_main())
