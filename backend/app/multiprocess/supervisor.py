from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import multiprocessing as mp
import os
from pathlib import Path
from queue import Empty, Full
import signal
import socket
import sys
import time
from typing import Any, Callable
from uuid import uuid4

from app.multiprocess.engine_worker import engine_worker_main
from app.multiprocess.feed import feed_process_main
from app.multiprocess.gateway import gateway_process_main
from app.multiprocess.metrics import metrics_process_main
from app.multiprocess.protocol import IPCEnvelope, ReliableWorkerEvent, TrackedMPQueue, now_ms
from app.multiprocess.sampler import sampler_process_main


COMPONENT_ORDER = ("metrics", "sampler", "feed", "spot_engine", "perp_engine", "gateway")
ENGINE_COMPONENTS = ("spot_engine", "perp_engine")
MARKET_BY_ENGINE = {"spot_engine": "BTCUSDT", "perp_engine": "BTCUSDT-PERP"}


@dataclass(slots=True)
class SupervisorConfig:
    runtime_dir: str
    history_path: str
    host: str = "127.0.0.1"
    port: int = 5184
    uds_path: str | None = None
    api_key: str = "mp-sandbox-key"
    feed_mode: str = "binance"
    feed_interval_ms: int = 20
    command_queue_max: int = 2_048
    plan_queue_max: int = 1
    reference_queue_max: int = 1
    control_queue_max: int = 64
    reliable_queue_max: int = 4_096
    book_queue_max: int = 64
    metrics_queue_max: int = 4_096
    metrics_output_max: int = 2
    sampler_input_max: int = 2
    ready_timeout_seconds: float = 15.0
    sampler_flush_seconds: float = 5.0
    restart_budget: int = 5
    restart_window_seconds: float = 60.0
    restart_backoff_base_seconds: float = 0.25
    restart_backoff_max_seconds: float = 10.0
    reference_stale_ms: int = 1_000
    book_stale_ms: int = 5_000
    heartbeat_stale_ms: int = 2_500
    ui_coalesce_ms: int = 25
    enable_cpu_affinity: bool = False

    def normalized(self) -> "SupervisorConfig":
        runtime = Path(self.runtime_dir).expanduser().resolve()
        history = Path(self.history_path).expanduser().resolve()
        uds = Path(self.uds_path).expanduser().resolve() if self.uds_path else runtime / "gateway.sock"
        if len(os.fsencode(str(uds))) >= 100:
            digest = sha256(str(runtime).encode("utf-8")).hexdigest()[:16]
            uds = Path("/tmp") / f"market-sandbox-{digest}.sock"
        self.runtime_dir = str(runtime)
        self.history_path = str(history)
        self.uds_path = str(uds)
        return self


class ProcessSupervisor:
    """Spawn-only lifecycle owner for the isolated process tree."""

    def __init__(self, config: SupervisorConfig) -> None:
        self.config = config.normalized()
        self.ctx = mp.get_context("spawn")
        self.run_id = str(uuid4())
        self.runtime_dir = Path(self.config.runtime_dir)
        self.pid_path = self.runtime_dir / "supervisor.pid"
        self.status_path = self.runtime_dir / "supervisor_status.json"
        self.action_results_dir = self.runtime_dir / "action_results"
        self.processes: dict[str, mp.Process] = {}
        self.stop_events: dict[str, Any] = {}
        self.ready: dict[str, dict[str, Any]] = {}
        self.restart_history: dict[str, deque[float]] = defaultdict(deque)
        self.restart_counts: dict[str, int] = defaultdict(int)
        self.component_state: dict[str, str] = {name: "STOPPED" for name in COMPONENT_ORDER}
        self.degraded_reason: str | None = None
        self.stopping = False
        self.stop_requested = False
        self.last_action: dict[str, Any] | None = None
        self._lock_owned = False
        self._create_queues()

    def _create_queues(self) -> None:
        self.ready_queue = TrackedMPQueue(self.ctx, maxsize=64)
        self.command_queues = {
            market: TrackedMPQueue(self.ctx, maxsize=max(1, self.config.command_queue_max))
            for market in MARKET_BY_ENGINE.values()
        }
        self.plan_queues = {
            market: TrackedMPQueue(self.ctx, maxsize=max(1, self.config.plan_queue_max))
            for market in MARKET_BY_ENGINE.values()
        }
        self.reference_queues = {
            market: TrackedMPQueue(self.ctx, maxsize=max(1, self.config.reference_queue_max))
            for market in MARKET_BY_ENGINE.values()
        }
        self.control_queues = {
            market: TrackedMPQueue(self.ctx, maxsize=max(1, self.config.control_queue_max))
            for market in MARKET_BY_ENGINE.values()
        }
        self.book_queues = {
            market: TrackedMPQueue(self.ctx, maxsize=max(1, self.config.book_queue_max))
            for market in MARKET_BY_ENGINE.values()
        }
        self.sampler_queues = {
            market: TrackedMPQueue(self.ctx, maxsize=max(1, self.config.sampler_input_max))
            for market in MARKET_BY_ENGINE.values()
        }
        # Reliable ACK/private/lifecycle traffic is sharded by market.  Killing
        # one worker must never strand another market behind a Queue pipe/read
        # lock that belonged to the failed process.
        self.reliable_queues = {
            market: TrackedMPQueue(self.ctx, maxsize=max(1, self.config.reliable_queue_max))
            for market in MARKET_BY_ENGINE.values()
        }
        self.metrics_ingress_queue = TrackedMPQueue(self.ctx, maxsize=max(1, self.config.metrics_queue_max))
        self.metrics_output_queue = TrackedMPQueue(self.ctx, maxsize=max(1, self.config.metrics_output_max))
        self.supervisor_control_queue = TrackedMPQueue(self.ctx, maxsize=max(1, self.config.control_queue_max))

    def _component_spec(self, component: str) -> tuple[Callable[..., None], tuple[Any, ...]]:
        common = {"run_id": self.run_id, "component": component}
        if component == "metrics":
            return metrics_process_main, (
                {**common, "interval_seconds": 1.0, "started_at": time.time()},
                self.metrics_ingress_queue,
                self.metrics_output_queue,
                self.ready_queue,
                self.stop_events[component],
            )
        if component == "sampler":
            return sampler_process_main, (
                {
                    **common,
                    "history_path": self.config.history_path,
                    "feed_interval_ms": self.config.feed_interval_ms,
                    "sampler_interval_ms": 1_000,
                    "flush_seconds": self.config.sampler_flush_seconds,
                    "interval_ms": 1_000,
                },
                self.sampler_queues,
                self.metrics_ingress_queue,
                self.ready_queue,
                self.stop_events[component],
            )
        if component == "feed":
            return feed_process_main, (
                {
                    **common,
                    "mode": self.config.feed_mode,
                    "interval_ms": self.config.feed_interval_ms,
                },
                self.reference_queues,
                self.sampler_queues,
                self.metrics_ingress_queue,
                self.ready_queue,
                self.stop_events[component],
            )
        if component in ENGINE_COMPONENTS:
            market = MARKET_BY_ENGINE[component]
            return engine_worker_main, (
                {
                    **common,
                    "market_id": market,
                    "product_type": "PERP" if market.endswith("-PERP") else "SPOT",
                    "reference_stale_ms": self.config.reference_stale_ms,
                },
                self.command_queues[market],
                self.plan_queues[market],
                self.reference_queues[market],
                self.control_queues[market],
                self.reliable_queues[market],
                self.book_queues[market],
                self.metrics_ingress_queue,
                self.ready_queue,
                self.stop_events[component],
            )
        if component == "gateway":
            queues = {
                "command_queues": self.command_queues,
                "plan_queues": self.plan_queues,
                "control_queues": self.control_queues,
                "reliable_queues": self.reliable_queues,
                "book_queues": self.book_queues,
                "metrics_output_queue": self.metrics_output_queue,
                "metrics_ingress_queue": self.metrics_ingress_queue,
                "supervisor_control_queue": self.supervisor_control_queue,
            }
            return gateway_process_main, (
                {
                    **common,
                    "host": self.config.host,
                    "port": self.config.port,
                    "uds_path": self.config.uds_path,
                    "api_key": self.config.api_key,
                    "runtime_dir": self.config.runtime_dir,
                    "history_path": self.config.history_path,
                    "action_results_dir": str(self.action_results_dir),
                    "reference_stale_ms": self.config.reference_stale_ms,
                    "book_stale_ms": self.config.book_stale_ms,
                    "heartbeat_stale_ms": self.config.heartbeat_stale_ms,
                    "ui_coalesce_ms": self.config.ui_coalesce_ms,
                },
                queues,
                self.metrics_ingress_queue,
                self.ready_queue,
                self.stop_events[component],
            )
        raise KeyError(component)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _acquire_lock(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if self.pid_path.exists():
            try:
                existing = int(self.pid_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                existing = 0
            if self._pid_alive(existing):
                raise RuntimeError(f"multiprocess supervisor already running pid={existing}")
            self.pid_path.unlink(missing_ok=True)
        descriptor = os.open(self.pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        finally:
            os.close(descriptor)
        self._lock_owned = True

    def _release_lock(self) -> None:
        if not self._lock_owned:
            return
        try:
            if int(self.pid_path.read_text(encoding="utf-8").strip()) == os.getpid():
                self.pid_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        self._lock_owned = False

    def _preflight(self) -> None:
        if self.config.port == 5174:
            raise RuntimeError("canonical 5174 is protected; choose an isolated gateway port")
        uds_path = Path(str(self.config.uds_path))
        if uds_path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                probe.connect(str(uds_path))
            except OSError:
                uds_path.unlink(missing_ok=True)
            else:
                raise RuntimeError(f"UDS already has a live owner: {uds_path}")
            finally:
                probe.close()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # A clean stop can leave only TCP TIME_WAIT entries.  Reuse is
            # safe here because the probe has no listener and the PID/UDS
            # lock plus the bind still reject a live owner.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((self.config.host, int(self.config.port)))
        except OSError as exc:
            raise RuntimeError(f"gateway port already in use: {self.config.host}:{self.config.port}") from exc
        finally:
            probe.close()

    def start(self) -> None:
        if any(process.is_alive() for process in self.processes.values()):
            raise RuntimeError("supervisor is already started")
        self._preflight()
        self._acquire_lock()
        self.stopping = False
        self.stop_requested = False
        self.action_results_dir.mkdir(parents=True, exist_ok=True)
        try:
            for component in COMPONENT_ORDER:
                self._start_component(component)
            self._write_status()
        except Exception:
            self.stop()
            raise

    def _start_component(self, component: str) -> dict[str, Any]:
        stop_event = self.ctx.Event()
        self.stop_events[component] = stop_event
        target, args = self._component_spec(component)
        process = self.ctx.Process(
            name=f"market-sandbox-{component}",
            target=target,
            args=args,
            daemon=False,
        )
        self.component_state[component] = "STARTING"
        process.start()
        self.processes[component] = process
        self._apply_affinity(component, process.pid)
        ready = self._wait_ready(component, process)
        self.ready[component] = ready
        self.component_state[component] = "READY"
        try:
            self.metrics_ingress_queue.put_nowait(
                {
                    "type": "REGISTER",
                    "component": component,
                    "pid": process.pid,
                    "restart_count": self.restart_counts[component],
                }
            )
        except Full:
            pass
        if component == "metrics":
            try:
                self.metrics_ingress_queue.put_nowait(
                    {
                        "type": "REGISTER",
                        "component": "supervisor",
                        "pid": os.getpid(),
                        "restart_count": 0,
                    }
                )
            except Full:
                pass
        if component == "gateway":
            self._rehydrate_gateway()
        return ready

    def _rehydrate_gateway(self) -> None:
        """Re-announce live worker epochs and request immutable snapshots."""

        for engine_component in ENGINE_COMPONENTS:
            worker = self.ready.get(engine_component) or {}
            market = MARKET_BY_ENGINE[engine_component]
            epoch = str(worker.get("stream_epoch") or "")
            if not epoch:
                continue
            event = ReliableWorkerEvent(
                run_id=self.run_id,
                market_id=market,
                stream_epoch=epoch,
                event_type="WORKER_READY",
                command_id="",
                request_fingerprint="",
                payload=dict(worker),
            )
            try:
                self.reliable_queues[market].put(event, timeout=0.2)
            except Full:
                pass
            timestamp = now_ms()
            request = IPCEnvelope.build(
                run_id=self.run_id,
                market_id=market,
                stream_epoch=epoch,
                kind="SNAPSHOT_REQUEST",
                payload={"reason": "gateway_started"},
                command_sequence=timestamp,
                priority_sequence=0,
                deadline=timestamp + 2_000,
            )
            try:
                self.control_queues[market].put_nowait(request)
            except Full:
                pass

    def _wait_ready(self, component: str, process: mp.Process) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.ready_timeout_seconds
        deferred: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            if not process.is_alive():
                raise RuntimeError(f"{component} exited before ready code={process.exitcode}")
            try:
                message = self.ready_queue.get(timeout=0.1)
            except Empty:
                continue
            if isinstance(message, dict) and message.get("component") == component:
                for item in deferred:
                    self.ready_queue.put_nowait(item)
                return message
            if isinstance(message, dict):
                deferred.append(message)
        for item in deferred:
            self.ready_queue.put_nowait(item)
        raise TimeoutError(f"{component} did not become ready")

    def _apply_affinity(self, component: str, pid: int | None) -> None:
        if not self.config.enable_cpu_affinity or not sys.platform.startswith("linux") or not pid:
            return
        if not hasattr(os, "sched_setaffinity"):
            return
        cpu_count = max(1, os.cpu_count() or 1)
        preferred = {
            "spot_engine": 0,
            "perp_engine": 1,
            "gateway": 2,
            "feed": 3,
            "sampler": 4,
            "metrics": 5,
        }[component] % cpu_count
        try:
            os.sched_setaffinity(pid, {preferred})
        except OSError:
            pass

    @staticmethod
    def _drain(queue: Any) -> int:
        count = 0
        while True:
            try:
                queue.get_nowait()
                count += 1
            except Empty:
                return count

    def monitor_once(self) -> dict[str, Any]:
        if self.stopping:
            return self.status()
        self._handle_control_messages()
        if self.stopping:
            return self.status()
        for component, process in list(self.processes.items()):
            if process.is_alive() or self.component_state.get(component) == "DEGRADED":
                continue
            self.component_state[component] = "EXITED"
            # A dead gateway/worker must not leave a maker writing against a
            # stale control plane. Pause all currently live makers before any
            # automatic restart, then rehydrate only the affected component.
            self._pause_makers(reason=f"component_exit:{component}")
            if component in ENGINE_COMPONENTS:
                market = MARKET_BY_ENGINE[component]
                self._drain(self.command_queues[market])
                self._drain(self.plan_queues[market])
                self._drain(self.control_queues[market])
                # Reliable lifecycle/ACK and latest-only book frames belong
                # to the old worker generation as well.  Leaving either
                # queue populated lets a newly spawned worker race a stale
                # frame and can leave Gateway without a valid new-epoch
                # snapshot after it has fenced the old book.
                self._drain(self.reliable_queues[market])
                self._drain(self.book_queues[market])
            if not self._restart_allowed(component):
                self.component_state[component] = "DEGRADED"
                self.degraded_reason = f"restart budget exhausted: {component}"
                continue
            self.restart_counts[component] += 1
            backoff = min(
                self.config.restart_backoff_max_seconds,
                self.config.restart_backoff_base_seconds * (2 ** max(0, self.restart_counts[component] - 1)),
            )
            self.component_state[component] = "BACKING_OFF"
            deadline = time.monotonic() + backoff
            while not self.stopping and time.monotonic() < deadline:
                time.sleep(min(0.05, deadline - time.monotonic()))
            if not self.stopping:
                self._start_component(component)
        self._write_status()
        return self.status()

    def _action_result_path(self, action_id: str) -> Path:
        safe = "".join(char for char in str(action_id) if char.isalnum() or char in {"-", "_"})[:120]
        return self.action_results_dir / f"{safe or 'action'}.json"

    def _write_action_result(self, result: dict[str, Any]) -> dict[str, Any]:
        payload = {"at": now_ms(), **result}
        action_id = str(payload.get("action_id") or uuid4())
        payload["action_id"] = action_id
        self.action_results_dir.mkdir(parents=True, exist_ok=True)
        target = self._action_result_path(action_id)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary.replace(target)
        self.last_action = payload
        return payload

    @staticmethod
    def _component_alias(target: str) -> str | None:
        value = str(target or "").strip().lower().replace("-", "_")
        return {
            "spot": "spot_engine",
            "spot_worker": "spot_engine",
            "spot_engine": "spot_engine",
            "perp": "perp_engine",
            "perp_worker": "perp_engine",
            "perp_engine": "perp_engine",
            "gateway": "gateway",
            "feed": "feed",
            "sampler": "sampler",
            "metrics": "metrics",
        }.get(value)

    def _pause_maker(self, market: str, *, reason: str) -> None:
        component = "spot_engine" if market == "BTCUSDT" else "perp_engine"
        sequence = now_ms()
        envelope = IPCEnvelope.build(
            run_id=self.run_id,
            market_id=market,
            stream_epoch=str(self.ready.get(component, {}).get("stream_epoch") or "*"),
            kind="PAUSE_MAKER",
            payload={"reason": reason},
            command_sequence=sequence,
            priority_sequence=0,
            deadline=sequence + 1_000,
        )
        try:
            self.control_queues[market].put_nowait(envelope)
        except Full:
            pass

    def _pause_makers(self, *, reason: str = "supervisor_shutdown", markets: tuple[str, ...] | None = None) -> None:
        selected = markets or tuple(MARKET_BY_ENGINE.values())
        for market in selected:
            self._pause_maker(market, reason=reason)
        time.sleep(0.05)

    def _restart_allowed(self, component: str) -> bool:
        now = time.monotonic()
        history = self.restart_history[component]
        while history and now - history[0] > self.config.restart_window_seconds:
            history.popleft()
        if len(history) >= self.config.restart_budget:
            return False
        history.append(now)
        return True

    def _restart_component(self, component: str, *, reason: str, enforce_budget: bool = False) -> dict[str, Any]:
        if component not in COMPONENT_ORDER:
            return {"status": "INVALID_TARGET", "reason": f"unknown component: {component}"}
        old_pid = self.processes.get(component).pid if self.processes.get(component) is not None else None
        if enforce_budget and not self._restart_allowed(component):
            self.component_state[component] = "DEGRADED"
            self.degraded_reason = f"restart budget exhausted: {component}"
            return {"status": "RESTART_BUDGET_EXHAUSTED", "component": component, "old_pid": old_pid}
        if component in ENGINE_COMPONENTS:
            market = MARKET_BY_ENGINE[component]
            self._pause_makers(reason=reason, markets=(market,))
            for queue in (
                self.command_queues[market],
                self.plan_queues[market],
                self.control_queues[market],
                self.reliable_queues[market],
                self.book_queues[market],
            ):
                self._drain(queue)
        self._stop_component(component, 3.0)
        self.ready.pop(component, None)
        self.component_state[component] = "STARTING"
        self._start_component(component)
        new_pid = self.processes.get(component).pid if self.processes.get(component) is not None else None
        return {
            "status": "RESTARTED",
            "component": component,
            "old_pid": old_pid,
            "new_pid": new_pid,
            "restart_count": int(self.restart_counts.get(component, 0)),
            "reason": reason,
        }

    def _restart_all(self, *, reason: str) -> dict[str, Any]:
        self._pause_makers(reason=reason)
        old_pids = {component: process.pid for component, process in self.processes.items()}
        for component in reversed(COMPONENT_ORDER):
            self._stop_component(component, self.config.sampler_flush_seconds + 1.0 if component == "sampler" else 3.0)
            self.ready.pop(component, None)
        for component in COMPONENT_ORDER:
            self._start_component(component)
        return {
            "status": "RESTARTED",
            "scope": "all",
            "old_pids": old_pids,
            "new_pids": {component: process.pid for component, process in self.processes.items()},
            "reason": reason,
        }

    def _rotate_history(self) -> dict[str, Any]:
        history = Path(self.config.history_path).resolve()
        self._stop_component("sampler", self.config.sampler_flush_seconds + 1.0)
        archive: Path | None = None
        if history.exists():
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            archive = history.with_name(f"{history.name}.archive.{stamp}.{self.run_id}")
            os.replace(history, archive)
        for sidecar in (Path(f"{history}-wal"), Path(f"{history}-shm")):
            if sidecar.exists() and sidecar.parent == history.parent:
                sidecar.unlink()
        self.ready.pop("sampler", None)
        self._start_component("sampler")
        return {"status": "ROTATED", "history_path": str(history), "archive": str(archive) if archive else None}

    def _export_run_summary(self, *, action_id: str) -> dict[str, Any]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        target = self.runtime_dir / f"run_summary_{self.run_id}_{stamp}.json"
        payload = {
            "run_id": self.run_id,
            "exported_at": now_ms(),
            "action_id": action_id,
            "supervisor": self.status(),
            "config": asdict(self.config),
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return {"status": "EXPORTED", "path": str(target)}

    def _handle_control_messages(self) -> None:
        while True:
            try:
                request = self.supervisor_control_queue.get_nowait()
            except Empty:
                return
            if not isinstance(request, dict):
                continue
            action_id = str(request.get("action_id") or uuid4())
            action = str(request.get("action") or "").lower()
            target = str(request.get("target") or "")
            impact = str(request.get("impact") or "")
            try:
                if action in {"start", "start_all"}:
                    missing = [component for component in COMPONENT_ORDER if not self.processes.get(component) or not self.processes[component].is_alive()]
                    for component in COMPONENT_ORDER:
                        if component in missing:
                            self._start_component(component)
                    result = {"status": "STARTED" if missing else "ALREADY_RUNNING", "components": missing}
                elif action in {"stop", "shutdown"}:
                    result = {"status": "STOP_ACCEPTED", "scope": "all", "impact": impact or "pause makers and stop the complete isolated run"}
                    self.stop_requested = True
                elif action in {"restart_worker", "start_worker", "restart"}:
                    component = self._component_alias(target)
                    if component is None or component not in ENGINE_COMPONENTS:
                        result = {"status": "INVALID_TARGET", "reason": "target must be spot or perp worker"}
                    else:
                        result = self._restart_component(component, reason=impact or "operator_requested_worker_restart")
                elif action in {"restart_all", "restart_run"}:
                    result = self._restart_all(reason=impact or "operator_requested_full_restart")
                elif action in {"set_profile", "set_refresh_profile"}:
                    profile = int(request.get("profile_ms") or target or 0)
                    if profile not in {50, 100, 300}:
                        result = {"status": "INVALID_PROFILE", "allowed_ms": [50, 100, 300]}
                    else:
                        self.config.feed_interval_ms = profile
                        result = self._restart_component("feed", reason=f"operator_profile_{profile}ms")
                        result.update({"profile_ms": profile, "status": "PROFILE_APPLIED", "scope": "feed reference cadence"})
                elif action in {"clear_memory", "clear_run_memory"}:
                    self._pause_makers(reason="operator_clear_run_memory")
                    result = {
                        "status": "MEMORY_CLEARED",
                        "scope": "spot_engine,perp_engine process memory",
                        "history_retained": True,
                    }
                    for component in ENGINE_COMPONENTS:
                        result.update({component: self._restart_component(component, reason="operator_clear_run_memory")})
                elif action in {"rotate_history", "rotate_history_db"}:
                    result = self._rotate_history()
                elif action in {"export_run_summary", "export_summary"}:
                    result = self._export_run_summary(action_id=action_id)
                else:
                    result = {"status": "INVALID_ACTION", "action": action}
            except Exception as exc:
                result = {"status": "ACTION_FAILED", "action": action, "reason": str(exc)[:500]}
            result.update({"action_id": action_id, "action": action, "target": target, "impact": impact})
            self._write_action_result(result)
            self._write_status()

    def terminate_component(self, component: str, *, kill: bool = True) -> int | None:
        process = self.processes.get(component)
        if process is None:
            return None
        pid = process.pid
        if process.is_alive():
            if kill and hasattr(process, "kill"):
                process.kill()
            else:
                process.terminate()
            process.join(timeout=2)
        return pid

    def _stop_component(self, component: str, timeout: float) -> None:
        process = self.processes.get(component)
        event = self.stop_events.get(component)
        if event is not None:
            event.set()
        if process is None:
            return
        process.join(timeout=max(0.1, timeout))
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=2)
        self.component_state[component] = "STOPPED"

    def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        try:
            # Keep Gateway alive until the end so it can expose run-reset and
            # final shutdown state.  Makers/plans stop before engine writers.
            self._pause_makers()
            for component in ENGINE_COMPONENTS:
                self._stop_component(component, 3.0)
            self._stop_component("sampler", self.config.sampler_flush_seconds + 1.0)
            self._stop_component("feed", 3.0)
            self._stop_component("metrics", 3.0)
            self._stop_component("gateway", 3.0)
            self._cleanup_tracked_orphans()
            self._write_status()
        finally:
            Path(str(self.config.uds_path)).unlink(missing_ok=True)
            self._release_lock()

    def _cleanup_tracked_orphans(self) -> None:
        """Kill only exact children created by this supervisor."""

        for component, process in self.processes.items():
            if not process.is_alive():
                continue
            process.terminate()
            process.join(timeout=1)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=1)
            self.component_state[component] = "STOPPED"

    def status(self) -> dict[str, Any]:
        components: dict[str, Any] = {}
        for component in COMPONENT_ORDER:
            process = self.processes.get(component)
            components[component] = {
                "pid": None if process is None else process.pid,
                "alive": bool(process is not None and process.is_alive()),
                "exitcode": None if process is None else process.exitcode,
                "state": self.component_state.get(component),
                "restart_count": int(self.restart_counts.get(component, 0)),
                "ready": self.ready.get(component),
            }
        degraded = self.degraded_reason is not None or any(
            item["state"] == "DEGRADED" for item in components.values()
        )
        return {
            "run_id": self.run_id,
            "status": "DEGRADED" if degraded else ("STOPPING" if self.stopping else "HEALTHY"),
            "effective_status": "DEGRADED" if degraded else ("STOPPING" if self.stopping else "HEALTHY"),
            "degraded_reason": self.degraded_reason,
            "spawn_method": self.ctx.get_start_method(),
            "supervisor_pid": os.getpid(),
            "config": asdict(self.config),
            "components": components,
            "last_action": self.last_action,
            "at": now_ms(),
        }

    def _write_status(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.status(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary.replace(self.status_path)

    def run_forever(self) -> None:
        stopping = False

        def request_stop(_signum: int, _frame: Any) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        self.start()
        try:
            while not stopping:
                self.monitor_once()
                if self.stop_requested:
                    break
                time.sleep(0.1)
        finally:
            self.stop()
