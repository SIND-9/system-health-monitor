import os
import shutil
import time

def check_system_health():
    # 1. Check Disk Space
    # Total, used, and free space on the main C: drive
    total, used, free = shutil.disk_usage("C:")
    free_gb = free / (2**30)  # Convert bytes to Gigabytes
    
    # 2. Check CPU Load (Using Windows native built-in command via os.popen)
    # This keeps our script ultra-lightweight without needing heavy external pip packages
    cpu_command = os.popen("wmic cpu get loadpercentage").read()
    cpu_lines = cpu_command.split()
    
    # Safely parse the CPU text output
    cpu_usage = 0
    for line in cpu_lines:
        if line.isdigit():
            cpu_usage = int(line)
            break

    # 3. Print Clean Terminal Alerts
    print("\n--- SYSTEM HEALTH UPDATE ---")
    print(f"Available Storage: {free_gb:.2f} GB Free")
    print(f"Current CPU Usage: {cpu_usage}%")
    
    # Threshold Warnings
    if cpu_usage > 80:
        print("⚠️ ALERT: CPU utilization is critically high!")
    else:
        print("✅ System performance is stable.")
    print("----------------------------")

if __name__ == "__main__":
    print("Starting System Monitor... Press Ctrl+C to exit.")
    try:
        while True:
            check_system_health()
            time.sleep(5)  # Wait 5 seconds before checking again
    except KeyboardInterrupt:
        print("\nMonitoring stopped safely.")