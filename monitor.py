import psutil
import time
import logging
import os
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────────
CPU_WARN_THRESHOLD    = 80    # % CPU usage before warning
RAM_WARN_THRESHOLD    = 80    # % RAM usage before warning
DISK_WARN_THRESHOLD   = 10   # GB free before warning
DISK_PATH             = "C:/"   # Mount point to monitor ("C:/" on Windows)
CHECK_INTERVAL        = 5     # Seconds between checks
TOP_PROCESS_COUNT     = 5     # How many top CPU processes to display
LOG_FILE              = "system_monitor.log"

# ── Terminal Colors (ANSI) ─────────────────────────────────────────────────────
class Color:
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

def red(text):    return f"{Color.RED}{text}{Color.RESET}"
def green(text):  return f"{Color.GREEN}{text}{Color.RESET}"
def yellow(text): return f"{Color.YELLOW}{text}{Color.RESET}"
def cyan(text):   return f"{Color.CYAN}{text}{Color.RESET}"
def bold(text):   return f"{Color.BOLD}{text}{Color.RESET}"

# ── Logging Setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

def log(level: str, message: str):
    """Write a message to the log file."""
    getattr(logging, level)(message)

# ── Metric Collectors ──────────────────────────────────────────────────────────
def get_cpu() -> float:
    """Return CPU usage as a percentage (1-second sample)."""
    return psutil.cpu_percent(interval=1)

def get_ram() -> tuple[float, float, float]:
    """Return (used_gb, total_gb, percent_used)."""
    mem = psutil.virtual_memory()
    used_gb  = mem.used  / (2**30)
    total_gb = mem.total / (2**30)
    return used_gb, total_gb, mem.percent

def get_disk(path: str) -> tuple[float, float]:
    """Return (free_gb, total_gb) for the given mount point."""
    usage = psutil.disk_usage(path)
    free_gb  = usage.free  / (2**30)
    total_gb = usage.total / (2**30)
    return free_gb, total_gb

def get_network() -> tuple[float, float]:
    """Return cumulative (sent_mb, recv_mb) since boot."""
    net = psutil.net_io_counters()
    sent_mb = net.bytes_sent / (2**20)
    recv_mb = net.bytes_recv / (2**20)
    return sent_mb, recv_mb

def get_top_processes(n: int) -> list[dict]:
    """Return the top-n processes by CPU usage."""
    procs = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            procs.append(proc.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return sorted(procs, key=lambda p: p["cpu_percent"] or 0, reverse=True)[:n]

# ── Display & Alert ────────────────────────────────────────────────────────────
def status_line(label: str, value: str, warning: bool) -> str:
    indicator = red("⚠") if warning else green("✔")
    return f"  {indicator} {bold(label)}: {value}"

def check_system_health(prev_net: tuple[float, float]) -> tuple[float, float]:
    """
    Collect all metrics, print a report, log any alerts, and
    return the current network totals for delta calculation next run.
    """
    now         = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cpu         = get_cpu()
    ram_used, ram_total, ram_pct = get_ram()
    disk_free, disk_total        = get_disk(DISK_PATH)
    net_sent, net_recv           = get_network()
    top_procs   = get_top_processes(TOP_PROCESS_COUNT)

    # Network delta since last check
    delta_sent = net_sent - prev_net[0]
    delta_recv = net_recv - prev_net[1]

    # Determine warning states
    cpu_warn  = cpu        > CPU_WARN_THRESHOLD
    ram_warn  = ram_pct    > RAM_WARN_THRESHOLD
    disk_warn = disk_free  < DISK_WARN_THRESHOLD

    # ── Print report ──
    print(f"\n{bold(cyan('═══ SYSTEM HEALTH ═══'))}  {now}")

    print(status_line(
        "CPU Usage",
        f"{cpu:.1f}%  (warn > {CPU_WARN_THRESHOLD}%)",
        cpu_warn,
    ))
    if cpu_warn:
        print(red(f"    ↳ ALERT: CPU is critically high!"))
        log("warning", f"CPU high: {cpu:.1f}%")

    print(status_line(
        "RAM Usage",
        f"{ram_used:.1f} GB / {ram_total:.1f} GB  ({ram_pct:.1f}%)",
        ram_warn,
    ))
    if ram_warn:
        print(red(f"    ↳ ALERT: RAM usage is critically high!"))
        log("warning", f"RAM high: {ram_pct:.1f}%")

    print(status_line(
        "Disk Free",
        f"{disk_free:.1f} GB / {disk_total:.1f} GB  on {DISK_PATH}",
        disk_warn,
    ))
    if disk_warn:
        print(red(f"    ↳ ALERT: Disk space is critically low!"))
        log("warning", f"Disk low: {disk_free:.1f} GB free")

    print(status_line(
        "Network",
        f"↑ {delta_sent:.2f} MB  ↓ {delta_recv:.2f} MB  (this interval)",
        False,
    ))

    # ── Top processes ──
    print(f"\n  {bold('Top Processes (by CPU):')}")
    for i, p in enumerate(top_procs, 1):
        name    = (p["name"] or "?")[:22].ljust(22)
        cpu_p   = p["cpu_percent"] or 0.0
        mem_p   = p["memory_percent"] or 0.0
        line    = f"  {i}. {name}  CPU: {cpu_p:5.1f}%   RAM: {mem_p:5.1f}%"
        print(red(line) if cpu_p > CPU_WARN_THRESHOLD else line)

    if not any([cpu_warn, ram_warn, disk_warn]):
        print(f"\n  {green('✔ All systems nominal.')}")

    log("info", (
        f"CPU={cpu:.1f}%  RAM={ram_pct:.1f}%  "
        f"DiskFree={disk_free:.1f}GB  "
        f"Net↑{delta_sent:.2f}MB ↓{delta_recv:.2f}MB"
    ))

    return net_sent, net_recv

# ── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(bold(cyan("Starting System Monitor...")) + "  Press Ctrl+C to stop.")
    print(f"Logs → {os.path.abspath(LOG_FILE)}")

    # Prime psutil's per-process CPU counter (first call always returns 0.0)
    psutil.cpu_percent(interval=None)
    for p in psutil.process_iter():
        try:
            p.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    prev_net = get_network()   # baseline for delta calculation

    try:
        while True:
            prev_net = check_system_health(prev_net)
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        print(f"\n{yellow('Monitoring stopped.')}  Full log saved to {LOG_FILE}")