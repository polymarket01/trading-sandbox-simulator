from __future__ import annotations

from collections import deque
import os
import re
from queue import Empty
import subprocess
import sys
import time
from typing import Any

from app.multiprocess.protocol import now_ms, put_latest


_MAC_MEMORY_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}


def _percentile(values: deque[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((percentile / 100) * (len(ordered) - 1))))
    return round(float(ordered[index]), 3)


def _parse_bytes(value: str) -> int:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?)(?:i?B)?", value, re.IGNORECASE)
    if not match:
        return 0
    number = float(match.group(1))
    multiplier = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}.get(
        match.group(2).upper(),
        1,
    )
    return int(number * multiplier)


def _mac_process_memory(pid: int) -> dict[str, Any]:
    """Read macOS physical footprint and per-process swapped bytes.

    ``psutil.memory_full_info`` needs task_for_pid permission on macOS and can
    therefore fail for a process owned by the same user. ``footprint`` is the
    system's own accounting surface and gives a materially different number
    from RSS. A missing value is returned as unavailable instead of being
    relabeled as USS.
    """

    if sys.platform != "darwin":
        return {"physical_footprint_bytes": None, "swap_bytes": None, "source": "unavailable"}
    cached = _MAC_MEMORY_CACHE.get(int(pid))
    if cached is not None and time.monotonic() - cached[0] < 5.0:
        return dict(cached[1])
    command = "/usr/bin/footprint"
    if not os.path.exists(command):
        return {"physical_footprint_bytes": None, "swap_bytes": None, "source": "unavailable"}
    try:
        result = subprocess.run(
            [command, "-p", str(pid), "--swapped", "--noCategories", "--format", "bytes"],
            check=False,
            capture_output=True,
            text=True,
            timeout=0.6,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {
            "physical_footprint_bytes": None,
            "swap_bytes": None,
            "source": "unavailable",
            "error": str(exc)[:160],
        }
        _MAC_MEMORY_CACHE[int(pid)] = (time.monotonic(), result)
        return result
    output = f"{result.stdout}\n{result.stderr}"
    footprint_match = re.search(r"Footprint:\s*([0-9]+)\s+B", output, re.IGNORECASE)
    physical_match = re.search(r"phys_footprint:\s*([0-9]+)\s+B", output, re.IGNORECASE)
    # With --swapped, the second value on the TOTAL row is the swapped-out
    # portion. Keep the raw tool output out of the API; only expose numbers.
    total_match = re.search(
        r"(?m)^\s*([0-9]+)\s+B\s+([0-9]+)\s+B\s+([0-9]+)\s+B.*\bTOTAL\s*$",
        output,
    )
    physical = int(physical_match.group(1)) if physical_match else (
        int(footprint_match.group(1)) if footprint_match else None
    )
    swapped = int(total_match.group(2)) if total_match else None
    result = {
        "physical_footprint_bytes": physical,
        "swap_bytes": swapped,
        "source": "macos_footprint" if result.returncode == 0 and physical is not None else "unavailable",
    }
    _MAC_MEMORY_CACHE[int(pid)] = (time.monotonic(), result)
    return result


def _mac_system_memory() -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"compressed_bytes": None, "swap_total_bytes": None, "swap_used_bytes": None, "source": "unavailable"}
    page_size = 16_384
    compressed_pages = 0
    try:
        vm_stat = subprocess.run(
            ["/usr/bin/vm_stat"],
            check=False,
            capture_output=True,
            text=True,
            timeout=0.6,
        ).stdout
        page_match = re.search(r"page size of (\d+) bytes", vm_stat)
        if page_match:
            page_size = int(page_match.group(1))
        compressed_match = re.search(r"Pages occupied by compressor:\s*([0-9]+)", vm_stat)
        if compressed_match:
            compressed_pages = int(compressed_match.group(1))
    except (OSError, subprocess.TimeoutExpired):
        pass
    swap_total = swap_used = None
    try:
        swap = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "vm.swapusage"],
            check=False,
            capture_output=True,
            text=True,
            timeout=0.6,
        ).stdout
        total_match = re.search(r"total\s*=\s*([0-9.]+)M", swap)
        used_match = re.search(r"used\s*=\s*([0-9.]+)M", swap)
        swap_total = int(float(total_match.group(1)) * 1024**2) if total_match else None
        swap_used = int(float(used_match.group(1)) * 1024**2) if used_match else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "compressed_bytes": compressed_pages * page_size if compressed_pages else None,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_used,
        "page_size": page_size,
        "source": "macos_vm_stat",
    }


def _process_sample(pids: dict[str, int], handles: dict[int, Any]) -> dict[str, Any]:
    try:
        import psutil
    except ImportError:
        return {
            "components": {},
            "tree_cpu_single_core_pct": 0.0,
            "tree_cpu_machine_pct": 0.0,
            "tree_rss_bytes": 0,
            "tree_footprint_bytes": 0,
            "tree_physical_footprint_bytes": 0,
            "tree_compressed_bytes": None,
            "tree_swap_bytes": None,
            "cpu_count": os.cpu_count() or 1,
            "collector": "unavailable",
        }

    # Roles are registered by the Supervisor. The recursive child walk adds
    # helper processes (for example multiprocessing's resource tracker) once
    # so a process tree never double-counts thread rows or a PID alias.
    role_by_pid = {int(pid): str(component) for component, pid in pids.items() if int(pid) > 0}
    tree_pids: set[int] = set(role_by_pid)
    supervisor_pid = next((pid for pid, role in role_by_pid.items() if role == "supervisor"), 0)
    if supervisor_pid:
        try:
            root = psutil.Process(supervisor_pid)
            tree_pids.update(int(child.pid) for child in root.children(recursive=True))
        except Exception:
            pass

    samples: dict[str, dict[str, Any]] = {}
    total_cpu = 0.0
    total_rss = 0
    total_footprint = 0
    total_swap = 0
    swap_known = False
    process_by_pid = {int(pid): str(component) for component, pid in pids.items() if int(pid) > 0}
    for pid in sorted(tree_pids):
        component = process_by_pid.get(pid, f"child_{pid}")
        try:
            process = handles.get(pid)
            if process is None:
                process = psutil.Process(pid)
                process.cpu_percent(None)
                handles[pid] = process
            cpu = float(process.cpu_percent(None))
            # macOS ``memory_full_info`` asks for task_for_pid and can block
            # or return AccessDenied even for the same user. RSS is stable
            # and cheap; physical footprint is collected separately through
            # the macOS accounting tool below.
            memory = process.memory_info()
            rss = int(memory.rss)
            mac_memory = _mac_process_memory(pid)
            physical_footprint = mac_memory.get("physical_footprint_bytes")
            footprint = int(physical_footprint if physical_footprint is not None else rss)
            swap_bytes = mac_memory.get("swap_bytes")
            if swap_bytes is not None:
                total_swap += int(swap_bytes)
                swap_known = True
            create_time = float(process.create_time())
            samples[component] = {
                "pid": pid,
                "cpu_single_core_pct": round(cpu, 3),
                "cpu_machine_pct": round(cpu / max(1, int(psutil.cpu_count(logical=True) or 1)), 3),
                "rss_bytes": rss,
                "footprint_bytes": footprint,
                "physical_footprint_bytes": physical_footprint,
                "uss_bytes": int(getattr(memory, "uss", 0) or 0),
                "swap_bytes": swap_bytes,
                "memory_source": mac_memory.get("source", "rss_fallback"),
                "thread_count": int(process.num_threads()),
                "uptime_seconds": round(max(0.0, time.time() - create_time), 3),
                "started_at_ms": int(create_time * 1_000),
                "alive": bool(process.is_running()),
                "status": process.status(),
            }
            total_cpu += cpu
            total_rss += rss
            total_footprint += footprint
        except Exception as exc:
            samples[component] = {"pid": pid, "status": "unavailable", "error": str(exc)[:160]}
            handles.pop(pid, None)
    cpu_count = max(1, int(psutil.cpu_count(logical=True) or os.cpu_count() or 1))
    system_memory = _mac_system_memory()
    supervisor_sample = samples.get("supervisor")
    tree_uptime = supervisor_sample.get("uptime_seconds") if supervisor_sample else None
    return {
        "components": samples,
        "role_pids": {role: int(pid) for role, pid in pids.items() if int(pid) > 0},
        "tree_pids": sorted(tree_pids),
        "root_pid": int(pids.get("supervisor") or 0) or None,
        "tree_cpu_single_core_pct": round(total_cpu, 3),
        "tree_cpu_machine_pct": round(total_cpu / cpu_count, 3),
        "tree_rss_bytes": total_rss,
        "tree_footprint_bytes": total_footprint,
        "tree_physical_footprint_bytes": total_footprint,
        "tree_compressed_bytes": system_memory.get("compressed_bytes"),
        "tree_swap_bytes": total_swap if swap_known else system_memory.get("swap_used_bytes"),
        "tree_swap_source": "per_process_footprint" if swap_known else "system_vm_swapusage",
        "compressed_memory_scope": "system",
        "system_memory": system_memory,
        "thread_count": sum(int(item.get("thread_count") or 0) for item in samples.values()),
        "uptime_seconds": tree_uptime,
        "cpu_count": cpu_count,
        "collector": "psutil+macos_footprint" if sys.platform == "darwin" else "psutil",
    }


def metrics_process_main(
    config: dict[str, Any],
    metrics_queue: Any,
    output_queue: Any,
    ready_queue: Any,
    stop_event: Any,
) -> None:
    component = str(config["component"])
    ready_queue.put({"component": component, "pid": os.getpid(), "status": "READY", "at": now_ms()})
    pids: dict[str, int] = {component: os.getpid()}
    handles: dict[int, Any] = {}
    latest: dict[str, dict[str, Any]] = {}
    restart_counts: dict[str, int] = {}
    ipc_samples: deque[float] = deque(maxlen=max(128, int(config.get("latency_window", 8_192))))
    book_samples: deque[float] = deque(maxlen=max(128, int(config.get("latency_window", 8_192))))
    last_publish = 0.0

    while not stop_event.is_set():
        while True:
            try:
                message = metrics_queue.get_nowait()
            except Empty:
                break
            if not isinstance(message, dict):
                continue
            message_type = str(message.get("type") or "HEARTBEAT").upper()
            name = str(message.get("component") or "unknown")
            if message_type == "REGISTER":
                pid = int(message.get("pid") or 0)
                if pid > 0:
                    pids[name] = pid
                restart_counts[name] = int(message.get("restart_count") or restart_counts.get(name, 0))
                continue
            if message_type == "UNREGISTER":
                pids.pop(name, None)
                continue
            latest[name] = dict(message)
            if message.get("ipc_latency_ms") is not None:
                try:
                    ipc_samples.append(float(message["ipc_latency_ms"]))
                except (TypeError, ValueError):
                    pass
            if message.get("book_latency_ms") is not None:
                try:
                    book_samples.append(float(message["book_latency_ms"]))
                except (TypeError, ValueError):
                    pass

        current = time.monotonic()
        if current - last_publish >= max(0.2, float(config.get("interval_seconds", 1.0))):
            last_publish = current
            process = _process_sample(pids, handles)
            snapshot = {
                "run_id": str(config["run_id"]),
                "component": component,
                "pid": os.getpid(),
                "at": now_ms(),
                "uptime_seconds": round(max(0.0, time.time() - float(config.get("started_at", time.time()))), 3),
                "process_tree": process,
                "heartbeats": latest,
                "worker_restarts": dict(restart_counts),
                "latency": {
                    "ipc": {
                        "count": len(ipc_samples),
                        "p50_ms": _percentile(ipc_samples, 50),
                        "p99_ms": _percentile(ipc_samples, 99),
                        "max_ms": round(max(ipc_samples), 3) if ipc_samples else 0.0,
                    },
                    "book_end_to_end": {
                        "count": len(book_samples),
                        "p50_ms": _percentile(book_samples, 50),
                        "p99_ms": _percentile(book_samples, 99),
                        "max_ms": round(max(book_samples), 3) if book_samples else 0.0,
                    },
                },
            }
            put_latest(output_queue, snapshot)
        time.sleep(0.01)
