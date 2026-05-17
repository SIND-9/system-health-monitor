"""
System Monitor — Full Edition
──────────────────────────────
Features:
  • Live Rich TUI dashboard (updates in-place, no scrolling)
  • CPU, RAM, Disk, Network, GPU, Temperature, Battery
  • Top processes by CPU and RAM
  • Auto-recovery: kill CPU hogs, clear temp, restart watched processes
  • Alert cooldowns (no spam)
  • SQLite metric + alert history
  • Built-in web dashboard server (visit http://localhost:8765)

Install deps:
  pip install psutil rich gputil

Run:
  python monitor.py           # TUI only (default)
  python monitor.py --web     # Web dashboard only
  python monitor.py --both    # TUI + web dashboard simultaneously
"""

import argparse
import json
import os
import platform
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import psutil
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    import GPUtil
    GPU_AVAILABLE = True
except ImportError:
    GPU_AVAILABLE = False

# ── Configuration ──────────────────────────────────────────────────────────────
CPU_WARN              = 80      # % CPU — warning level
CPU_CRIT              = 95      # % CPU — critical level
RAM_WARN              = 80
RAM_CRIT              = 95
DISK_WARN_GB          = 10      # GB free before warning
DISK_CRIT_GB          = 3       # GB free before critical + temp-clear
DISK_PATH             = "C:/" if platform.system() == "Windows" else "/"
INTERVAL              = 2       # seconds between refreshes
TOP_N                 = 8       # processes shown in each table
LOG_DB                = "monitor.db"
WEB_PORT              = 8765

COOLDOWN_SEC          = 120     # seconds before re-alerting on same issue

# Auto-recovery: kill any process at >= this CPU for 3+ consecutive ticks
AUTO_KILL_CPU_THRESHOLD = 98
AUTO_KILL_ENABLED       = False   # flip True to enable

# Processes to watch and restart if they disappear
WATCHED_PROCS: list[str] = []    # e.g. ["nginx", "redis-server"]

TEMP_DIR = os.environ.get("TEMP", "/tmp")

# ── Shared state ───────────────────────────────────────────────────────────────
console          = Console()
alert_last_sent: dict[str, datetime] = {}
high_cpu_streak: dict[int, int]      = {}
_prev_net                            = (0.0, 0.0)
_latest_metrics: dict                = {}


# ══════════════════════════════════════════════════════════════════════════════
# SQLite
# ══════════════════════════════════════════════════════════════════════════════
def init_db():
    con = sqlite3.connect(LOG_DB, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS metrics (
        ts TEXT, cpu_pct REAL, ram_pct REAL, ram_used_gb REAL,
        disk_free_gb REAL, net_sent_mb REAL, net_recv_mb REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS alerts (
        ts TEXT, kind TEXT, detail TEXT)""")
    con.commit(); con.close()

def db_write_metric(m: dict):
    con = sqlite3.connect(LOG_DB, check_same_thread=False)
    con.execute("INSERT INTO metrics VALUES (?,?,?,?,?,?,?)", (
        m["ts"], m["cpu"], m["ram_pct"], m["ram_used"],
        m["disk_free"], m["net_sent_delta"], m["net_recv_delta"]))
    con.commit(); con.close()

def db_write_alert(kind: str, detail: str):
    con = sqlite3.connect(LOG_DB, check_same_thread=False)
    con.execute("INSERT INTO alerts VALUES (?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), kind, detail))
    con.commit(); con.close()

def db_recent_alerts(n: int = 20) -> list[dict]:
    con = sqlite3.connect(LOG_DB, check_same_thread=False)
    rows = con.execute(
        f"SELECT ts,kind,detail FROM alerts ORDER BY ts DESC LIMIT {n}"
    ).fetchall()
    con.close()
    return [{"ts": r[0], "kind": r[1], "detail": r[2]} for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# Metric Collection
# ══════════════════════════════════════════════════════════════════════════════
def collect(first_run: bool = False) -> dict:
    global _prev_net

    cpu  = psutil.cpu_percent(interval=None)
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage(DISK_PATH)
    net  = psutil.net_io_counters()
    boot = datetime.fromtimestamp(psutil.boot_time())

    sent_mb = net.bytes_sent / 2**20
    recv_mb = net.bytes_recv / 2**20
    d_sent  = 0.0 if first_run else max(0, sent_mb - _prev_net[0])
    d_recv  = 0.0 if first_run else max(0, recv_mb - _prev_net[1])
    _prev_net = (sent_mb, recv_mb)

    # Temperatures
    temps = {}
    try:
        raw = psutil.sensors_temperatures()
        if raw:
            for name, entries in raw.items():
                if entries:
                    temps[name] = entries[0].current
    except AttributeError:
        pass

    # Battery
    battery = None
    try:
        b = psutil.sensors_battery()
        if b:
            battery = {"pct": b.percent, "charging": b.power_plugged}
    except AttributeError:
        pass

    # GPU
    gpus = []
    if GPU_AVAILABLE:
        try:
            for g in GPUtil.getGPUs():
                gpus.append({"name": g.name, "load": g.load * 100,
                             "mem_used": g.memoryUsed, "mem_total": g.memoryTotal,
                             "temp": g.temperature})
        except Exception:
            pass

    # Processes
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            procs.append(p.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    top_cpu = sorted(procs, key=lambda x: x["cpu_percent"] or 0, reverse=True)[:TOP_N]
    top_ram = sorted(procs, key=lambda x: x["memory_percent"] or 0, reverse=True)[:TOP_N]

    return {
        "ts":             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "uptime":         str(datetime.now() - boot).split(".")[0],
        "cpu":            cpu,
        "ram_pct":        mem.percent,
        "ram_used":       mem.used / 2**30,
        "ram_total":      mem.total / 2**30,
        "disk_free":      disk.free / 2**30,
        "disk_total":     disk.total / 2**30,
        "net_sent_total": sent_mb,
        "net_recv_total": recv_mb,
        "net_sent_delta": d_sent,
        "net_recv_delta": d_recv,
        "temps":          temps,
        "battery":        battery,
        "gpus":           gpus,
        "top_cpu":        top_cpu,
        "top_ram":        top_ram,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Auto-Recovery
# ══════════════════════════════════════════════════════════════════════════════
def _cooldown_ok(key: str) -> bool:
    last = alert_last_sent.get(key)
    if last and (datetime.now() - last).seconds < COOLDOWN_SEC:
        return False
    alert_last_sent[key] = datetime.now()
    return True

def recovery_actions(m: dict) -> list[str]:
    actions = []

    # Kill runaway CPU processes
    if AUTO_KILL_ENABLED:
        CRITICAL_WHITELIST = ["System", "svchost.exe", "explorer.exe", "python.exe", "cmd.exe", "Terminal"]
        for p in m["top_cpu"]:
            pid, cpu, name = p["pid"], p["cpu_percent"] or 0, p["name"] or "?"
            if name in CRITICAL_WHITELIST:
                continue
            if cpu >= AUTO_KILL_CPU_THRESHOLD:
                high_cpu_streak[pid] = high_cpu_streak.get(pid, 0) + 1
                if high_cpu_streak[pid] >= 3:
                    try:
                        psutil.Process(pid).kill()
                        msg = f"Killed PID {pid} ({name}) — {cpu:.0f}% CPU for 3+ ticks"
                        actions.append(msg); db_write_alert("AUTO_KILL", msg)
                        high_cpu_streak.pop(pid, None)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            else:
                high_cpu_streak.pop(pid, None)

    # Clear temp files on critically low disk
    if m["disk_free"] < DISK_CRIT_GB and _cooldown_ok("disk_clear"):
        cleared = 0
        for f in Path(TEMP_DIR).glob("*"):
            try:
                if f.is_file():
                    cleared += f.stat().st_size
                    f.unlink()
            except Exception:
                pass
        msg = f"Cleared {cleared/2**20:.1f} MB from {TEMP_DIR}"
        actions.append(msg); db_write_alert("DISK_CLEAR", msg)

    # Restart watched processes
    running = {p["name"] for p in m["top_cpu"] + m["top_ram"] if p["name"]}
    for name in WATCHED_PROCS:
        if name not in running and _cooldown_ok(f"restart_{name}"):
            try:
                subprocess.Popen([name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                msg = f"Restarted: {name}"
            except FileNotFoundError:
                msg = f"Could not restart {name} — not in PATH"
            actions.append(msg); db_write_alert("PROC_RESTART", msg)

    return actions


# ══════════════════════════════════════════════════════════════════════════════
# Rich TUI
# ══════════════════════════════════════════════════════════════════════════════
def _color(pct: float, warn: float, crit: float) -> str:
    if pct >= crit: return "bold red"
    if pct >= warn: return "bold yellow"
    return "bold green"

def _bar(pct: float, width: int = 18) -> Text:
    filled = int(min(pct, 100) / 100 * width)
    bar    = "█" * filled + "░" * (width - filled)
    color  = "green" if pct < 70 else ("yellow" if pct < 90 else "red")
    return Text(bar, style=color)

def build_layout(m: dict, actions: list[str]) -> Layout:
    root = Layout()
    root.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        *([] if not actions else [Layout(name="footer", size=len(actions) + 2)]),
    )
    root["body"].split_row(Layout(name="left"), Layout(name="right"))
    root["left"].split_column(Layout(name="core"), Layout(name="proc_cpu"))
    root["right"].split_column(Layout(name="extras"), Layout(name="proc_ram"))

    # Header
    root["header"].update(Panel(
        Text(f"⚙  SYSTEM MONITOR  ·  {m['ts']}  ·  uptime {m['uptime']}  ·  db: {LOG_DB}",
             justify="center"),
        style="bold cyan", box=box.HORIZONTALS))

    # Core metrics
    g = Table.grid(padding=(0, 1))
    g.add_column(width=10); g.add_column(width=20); g.add_column()

    def add(label, pct, detail, warn, crit):
        g.add_row(Text(label, style="dim"), _bar(pct),
                  Text(f"{pct:.1f}%  {detail}", style=_color(pct, warn, crit)))

    add("CPU",  m["cpu"],     "", CPU_WARN, CPU_CRIT)
    add("RAM",  m["ram_pct"], f"{m['ram_used']:.1f}/{m['ram_total']:.1f} GB", RAM_WARN, RAM_CRIT)
    disk_pct = 100 * (1 - m["disk_free"] / m["disk_total"])
    add("Disk", disk_pct, f"{m['disk_free']:.1f} GB free on {DISK_PATH}", 70, 90)
    g.add_row(Text("Network", style="dim"),
              Text(f"↑ {m['net_sent_delta']:.3f}  ↓ {m['net_recv_delta']:.3f} MB/s"), Text(""))
    root["core"].update(Panel(g, title="[bold]Core Metrics", border_style="cyan"))

    # Extras
    ex = Table.grid(padding=(0, 1))
    ex.add_column(width=14, style="dim"); ex.add_column()
    if m["gpus"]:
        for g2 in m["gpus"]:
            vc = _color(g2["load"], 70, 90)
            ex.add_row("GPU Load",  Text(f"{g2['load']:.0f}%  {g2['name'][:26]}", style=vc))
            ex.add_row("GPU Mem",   Text(f"{g2['mem_used']:.0f}/{g2['mem_total']:.0f} MB"))
            if g2["temp"]:
                tc = "red" if g2["temp"] > 85 else ("yellow" if g2["temp"] > 70 else "green")
                ex.add_row("GPU Temp", Text(f"{g2['temp']:.0f}°C", style=tc))
    else:
        ex.add_row("GPU", Text("gputil not installed / no GPU detected", style="dim italic"))
    if m["temps"]:
        for sensor, t in list(m["temps"].items())[:4]:
            tc = "red" if t > 90 else ("yellow" if t > 75 else "green")
            ex.add_row(f"Temp/{sensor[:7]}", Text(f"{t:.0f}°C", style=tc))
    else:
        ex.add_row("Temps", Text("N/A on this platform", style="dim italic"))
    if m["battery"]:
        b = m["battery"]
        bc = "green" if b["charging"] else ("yellow" if b["pct"] > 20 else "red")
        ex.add_row("Battery", Text(("⚡ " if b["charging"] else "🔋 ") + f"{b['pct']:.0f}%", style=bc))
    root["extras"].update(Panel(ex, title="[bold]GPU / Temps / Battery", border_style="cyan"))

    # Process tables
    for side, key, sort_col in [("proc_cpu", m["top_cpu"], "cpu_percent"),
                                  ("proc_ram", m["top_ram"], "memory_percent")]:
        pt = Table(box=box.SIMPLE, show_header=True, header_style="bold dim", padding=(0, 1))
        pt.add_column("PID", width=7); pt.add_column("Name", width=20)
        pt.add_column("CPU%", width=7); pt.add_column("RAM%", width=7)
        procs_list = m["top_cpu"] if side == "proc_cpu" else m["top_ram"]
        title_label = "CPU" if side == "proc_cpu" else "RAM"
        for p in procs_list:
            cpu_c = "red" if (p["cpu_percent"] or 0) > CPU_WARN else "white"
            ram_c = "red" if (p["memory_percent"] or 0) > RAM_WARN else "white"
            pt.add_row(str(p["pid"]), (p["name"] or "?")[:19],
                       Text(f"{p['cpu_percent'] or 0:.1f}", style=cpu_c),
                       Text(f"{p['memory_percent'] or 0:.1f}", style=ram_c))
        root[side].update(Panel(pt, title=f"[bold]Top Processes — {title_label}", border_style="blue"))

    # Footer: recovery actions
    if actions:
        root["footer"].update(Panel(
            Text("\n".join(f"  ⚡ {a}" for a in actions), style="bold yellow"),
            title="[bold red]Auto-Recovery Actions", border_style="red"))

    return root


# ══════════════════════════════════════════════════════════════════════════════
# Web Dashboard
# ══════════════════════════════════════════════════════════════════════════════
WEB_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>System Monitor</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@700;800&display=swap');
:root{--bg:#090b10;--panel:#0e1220;--border:#1a2035;--accent:#00e5ff;
  --warn:#ffb300;--crit:#ff3d71;--ok:#00e676;--text:#c9d1e0;--muted:#3d4f6b;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px}
header{display:flex;align-items:center;justify-content:space-between;
  padding:16px 28px;border-bottom:1px solid var(--border);
  background:linear-gradient(90deg,#090b10,#0e1220)}
header h1{font-family:'Syne',sans-serif;font-size:20px;color:var(--accent);
  letter-spacing:3px;text-transform:uppercase}
#ts{color:var(--muted);font-size:11px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));
  gap:14px;padding:18px 28px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:16px}
.card h2{font-family:'Syne',sans-serif;font-size:10px;letter-spacing:3px;
  text-transform:uppercase;color:var(--muted);margin-bottom:12px}
.m{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.ml{width:80px;color:var(--muted);flex-shrink:0;font-size:11px}
.bw{flex:1;background:#131929;border-radius:3px;height:7px;overflow:hidden}
.bf{height:100%;border-radius:3px;transition:width .5s ease}
.ok{color:var(--ok)}.warn{color:var(--warn)}
.crit{color:var(--crit);animation:pulse .7s infinite alternate}
.bok{background:var(--ok)}.bwarn{background:var(--warn)}
.bcrit{background:var(--crit);box-shadow:0 0 6px var(--crit)}
@keyframes pulse{to{opacity:.4}}
.mv{width:60px;text-align:right;font-weight:700;font-size:12px}
.md{color:var(--muted);font-size:11px;min-width:100px}
table{width:100%;border-collapse:collapse}
th{color:var(--muted);font-weight:400;text-align:left;padding:4px 5px;
  border-bottom:1px solid var(--border);font-size:10px;letter-spacing:1px}
td{padding:5px 5px;border-bottom:1px solid #10172a}
tr:last-child td{border:none}
#alerts{padding:0 28px 22px}
#alerts h2{font-family:'Syne',sans-serif;font-size:10px;letter-spacing:3px;
  text-transform:uppercase;color:var(--muted);margin-bottom:10px}
.ar{display:flex;gap:12px;padding:6px 12px;border-left:2px solid var(--warn);
  background:#18140a;margin-bottom:5px;border-radius:0 4px 4px 0;font-size:11px}
.at{color:var(--muted);min-width:150px}
.badge{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;
  background:#2a1f00;color:var(--warn);border:1px solid #4a3700}
</style></head><body>
<header><h1>⚙ System Monitor</h1><span id="ts">—</span></header>
<div class="grid" id="grid"></div>
<div id="alerts"><h2>Recent Alerts</h2><div id="al"></div></div>
<script>
function bc(p,w,c){return p>=c?'bcrit':p>=w?'bwarn':'bok'}
function vc(p,w,c){return p>=c?'crit':p>=w?'warn':'ok'}
function row(label,pct,detail,w=70,c=90){
  return`<div class="m"><span class="ml">${label}</span>
    <div class="bw"><div class="bf ${bc(pct,w,c)}" style="width:${Math.min(100,pct)}%"></div></div>
    <span class="mv ${vc(pct,w,c)}">${pct.toFixed(1)}%</span>
    <span class="md">${detail}</span></div>`}
function render(d){
  document.getElementById('ts').textContent=`${d.ts}  ·  uptime ${d.uptime}`;
  const dp=100*(1-d.disk_free/d.disk_total);
  let h=`<div class="card"><h2>Core Metrics</h2>
    ${row('CPU',d.cpu,'',80,95)}
    ${row('RAM',d.ram_pct,d.ram_used.toFixed(1)+'/'+d.ram_total.toFixed(1)+' GB',80,95)}
    ${row('Disk',dp,d.disk_free.toFixed(1)+' GB free',70,90)}</div>`;
  h+=`<div class="card"><h2>Network</h2>
    <div class="m"><span class="ml">↑ Sent</span><span class="ok">${d.net_sent_delta.toFixed(3)} MB/s</span></div>
    <div class="m"><span class="ml">↓ Recv</span><span style="color:var(--accent)">${d.net_recv_delta.toFixed(3)} MB/s</span></div>
    <div class="m"><span class="ml">Total ↑</span><span>${d.net_sent_total.toFixed(0)} MB</span></div>
    <div class="m"><span class="ml">Total ↓</span><span>${d.net_recv_total.toFixed(0)} MB</span></div></div>`;
  (d.gpus||[]).forEach(g=>{
    h+=`<div class="card"><h2>GPU — ${g.name}</h2>
      ${row('Load',g.load,'',70,90)}
      ${row('VRAM',100*g.mem_used/g.mem_total,g.mem_used.toFixed(0)+'/'+g.mem_total.toFixed(0)+' MB')}
      ${g.temp?row('Temp',g.temp,g.temp.toFixed(0)+'°C',70,85):''}</div>`;});
  if(d.battery){const b=d.battery;
    h+=`<div class="card"><h2>Battery</h2>${row('Charge',b.pct,b.charging?'⚡ Charging':'On battery',30,15)}</div>`;}
  h+=`<div class="card"><h2>Top Processes — CPU</h2><table>
    <tr><th>PID</th><th>Name</th><th>CPU%</th><th>RAM%</th></tr>`;
  (d.top_cpu||[]).forEach(p=>{const c=p.cpu_percent>=80?'crit':p.cpu_percent>=50?'warn':'';
    h+=`<tr><td>${p.pid}</td><td>${(p.name||'?').slice(0,20)}</td>
      <td class="${c}">${(p.cpu_percent||0).toFixed(1)}</td>
      <td>${(p.memory_percent||0).toFixed(1)}</td></tr>`;});
  h+=`</table></div><div class="card"><h2>Top Processes — RAM</h2><table>
    <tr><th>PID</th><th>Name</th><th>CPU%</th><th>RAM%</th></tr>`;
  (d.top_ram||[]).forEach(p=>{const c=p.memory_percent>=10?'warn':'';
    h+=`<tr><td>${p.pid}</td><td>${(p.name||'?').slice(0,20)}</td>
      <td>${(p.cpu_percent||0).toFixed(1)}</td>
      <td class="${c}">${(p.memory_percent||0).toFixed(1)}</td></tr>`;});
  h+='</table></div>';
  document.getElementById('grid').innerHTML=h;
}
function renderAlerts(a){
  document.getElementById('al').innerHTML=a.length
    ?a.map(x=>`<div class="ar"><span class="at">${x.ts}</span>
      <span class="badge">${x.kind}</span><span>${x.detail}</span></div>`).join('')
    :'<span style="color:var(--muted)">No alerts yet.</span>';}
async function poll(){
  try{
    const[md,ad]=await Promise.all([
      fetch('/api/metrics').then(r=>r.json()),
      fetch('/api/alerts').then(r=>r.json())]);
    render(md);renderAlerts(ad);
  }catch(e){console.warn(e);}
  setTimeout(poll,2000);}
poll();
</script></body></html>"""

class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if   self.path == "/":             self._ok("text/html", WEB_HTML.encode())
        elif self.path == "/api/metrics":  self._ok("application/json", json.dumps(_latest_metrics).encode())
        elif self.path == "/api/alerts":   self._ok("application/json", json.dumps(db_recent_alerts()).encode())
        else:                              self._send(404, "text/plain", b"not found")
    def _ok(self, ct, body):   self._send(200, ct, body)
    def _send(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        self.end_headers(); self.wfile.write(body)

def start_web():
    srv = HTTPServer(("0.0.0.0", WEB_PORT), WebHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# Main Loops
# ══════════════════════════════════════════════════════════════════════════════
def prime_cpu():
    psutil.cpu_percent(interval=None)
    for p in psutil.process_iter():
        try: p.cpu_percent(interval=None)
        except: pass

def run_tui(with_web: bool = False):
    global _latest_metrics
    init_db(); prime_cpu()
    if with_web:
        start_web()
        console.print(f"[bold cyan]Web dashboard →[/] http://localhost:{WEB_PORT}")
        time.sleep(0.5)
    m = collect(first_run=True)
    _latest_metrics = m
    with Live(build_layout(m, []), refresh_per_second=1, screen=True) as live:
        while True:
            time.sleep(INTERVAL)
            m = collect()
            acts = recovery_actions(m)
            _latest_metrics = m
            db_write_metric(m)
            live.update(build_layout(m, acts))

def run_web_only():
    global _latest_metrics
    init_db(); prime_cpu(); start_web()
    console.print(f"[bold cyan]Web →[/] http://localhost:{WEB_PORT}  (Ctrl+C to stop)")
    while True:
        m = collect()
        recovery_actions(m)
        _latest_metrics = m
        db_write_metric(m)
        time.sleep(INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="System Monitor")
    g  = ap.add_mutually_exclusive_group()
    g.add_argument("--web",  action="store_true", help="Web dashboard only")
    g.add_argument("--both", action="store_true", help="TUI + web dashboard")
    args = ap.parse_args()

    def _exit(sig, frame):
        console.print("\n[yellow]Monitor stopped.[/]"); sys.exit(0)
    signal.signal(signal.SIGINT, _exit)

    try:
        if args.web:  run_web_only()
        else:         run_tui(with_web=args.both)
    except KeyboardInterrupt:
        console.print("\n[yellow]Monitor stopped.[/]")