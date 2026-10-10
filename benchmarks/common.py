"""Shared helpers for the benchmark scripts."""

from __future__ import annotations

import os
import platform
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH_ROOT = PROJECT_ROOT / ".tmp" / "bench"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def scratch_dir(name: str) -> Path:
    """An empty folder under .tmp/bench (inside the project, never on C:)."""
    folder = SCRATCH_ROOT / name
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    return folder


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def summarize(seconds: list[float]) -> dict[str, float]:
    """Median and tail latency in microseconds."""
    return {
        "median_us": statistics.median(seconds) * 1e6,
        "p95_us": percentile(seconds, 0.95) * 1e6,
        "p99_us": percentile(seconds, 0.99) * 1e6,
        "samples": len(seconds),
    }


def _powershell(command: str) -> str:
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def environment() -> dict[str, str]:
    """Facts every benchmark report must state."""
    info = {
        "os": f"{platform.system()} {platform.release()} (build {platform.version()})",
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "machine": platform.machine(),
        "logical_cpus": str(os.cpu_count()),
        "cpu": platform.processor() or "unknown",
        "ram": "unknown",
        "drive": "unknown",
        "defender_realtime": "unknown",
    }
    if sys.platform == "win32":
        drive = PROJECT_ROOT.drive.rstrip(":")
        info["cpu"] = _powershell("(Get-CimInstance Win32_Processor).Name") or info["cpu"]
        ram = _powershell(
            "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)"
        )
        if ram:
            info["ram"] = f"{ram} GB"
        media = _powershell(
            f"(Get-Partition -DriveLetter {drive} | Get-Disk | Get-PhysicalDisk | "
            "ForEach-Object { $_.MediaType + ' / ' + $_.BusType + ' / ' + $_.FriendlyName })"
        )
        info["drive"] = f"{drive}: {media}" if media else f"{drive}: type not detected"
        defender = _powershell("(Get-MpComputerStatus).RealTimeProtectionEnabled")
        if defender:
            info["defender_realtime"] = "on" if defender.lower() == "true" else "off"
    return info
