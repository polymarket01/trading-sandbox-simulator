#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
BACKEND_VENV = BACKEND_DIR / ".venv"
LOCAL_DATA_DIR = BACKEND_DIR / "data"
LOCAL_SQLITE_PATH = LOCAL_DATA_DIR / "sandbox.db"
BACKEND_PORT = 5174
FRONTEND_PORT = 5173


def _detect_host() -> str:
    """自动检测本机对外可访问的 host，用于 VPS 部署时 CORS 和前端 API 地址。"""
    # 1. 获取主网卡 IP（连接外网时使用的出口 IP，VPS 上多为公网 IP）
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.3)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        if ip and ip != "127.0.0.1":
            return ip
    except OSError:
        pass
    # 2. 尝试获取公网 IP（VPS 通常能访问）
    try:
        import urllib.request

        with urllib.request.urlopen("https://api.ipify.org", timeout=2) as resp:
            ip = resp.read().decode().strip()
        if ip:
            return ip
    except Exception:
        pass
    return "localhost"


def _get_effective_host(manual_host: str) -> str:
    """获取实际使用的 host：手动指定优先，否则自动检测。"""
    if manual_host:
        return manual_host
    return _detect_host()


def _find_python() -> Path:
    """自动检测用于创建 venv 的 Python 解释器，优先使用当前运行的解释器。"""
    if sys.executable:
        p = Path(sys.executable)
        if p.exists():
            return p
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            p = Path(found)
            if p.exists():
                return p
    raise SystemExit(
        "[runner] 未找到可用的 Python 解释器。"
        "请确保已安装 Python 3.10+ 并加入 PATH。"
    )


class ManagedProcess:
    def __init__(self, name: str, process: subprocess.Popen[str]) -> None:
        self.name = name
        self.process = process


class SandboxRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.processes: list[ManagedProcess] = []
        self.started_docker_services = False
        self._stopping = False
        self.database_url = ""
        self.redis_url = "redis://localhost:6379/0"
        self.runtime_mode = "sqlite"
        self._backend_python: str = ""  # 用于 alembic/uvicorn 的 Python 路径
        self._host = _get_effective_host(args.public_host)

    def run(self) -> int:
        self._check_ports()
        self._ensure_backend_venv()
        self._ensure_frontend_node_modules()
        self._choose_runtime()
        self._run_alembic()
        self._start_backend()
        self._start_frontend()
        self._print_banner()
        return self._wait_loop()

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        if self.processes:
            print("\n[runner] 正在停止前后端进程...")
        for managed in reversed(self.processes):
            self._terminate_process(managed)
        self.processes.clear()
        if self.started_docker_services:
            print("[runner] 正在停止由启动器拉起的 db/redis...")
            subprocess.run(
                ["docker", "compose", "stop", "db", "redis"],
                cwd=ROOT,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )

    def _check_ports(self) -> None:
        for port, label in [(FRONTEND_PORT, "前端"), (BACKEND_PORT, "后端")]:
            if self._is_port_open("127.0.0.1", port):
                if os.name == "nt":
                    kill_msg = f"  可执行: netstat -ano | findstr :{port} 查找 PID，然后 taskkill /PID <PID> /F"
                else:
                    kill_msg = f"  可执行: lsof -ti :{port} | xargs kill -9"
                raise SystemExit(
                    f"[runner] {label}端口 {port} 已被占用，请先释放端口。\n{kill_msg}"
                )

    def _ensure_backend_venv(self) -> None:
        python = _find_python()
        req = BACKEND_DIR / "requirements.txt"

        if self.args.no_venv:
            self._backend_python = str(python)
            stamp = BACKEND_DIR / ".requirements.installed"
            if (not stamp.exists()) or stamp.stat().st_mtime < req.stat().st_mtime:
                print("[runner] 安装后端依赖（系统 Python）...")
                self._run([str(python), "-m", "pip", "install", "-r", "requirements.txt"], cwd=BACKEND_DIR)
                stamp.touch()
        else:
            if os.name == "nt":
                venv_python = BACKEND_VENV / "Scripts" / "python.exe"
            else:
                venv_python = BACKEND_VENV / "bin" / "python"

            if not venv_python.exists():
                print("[runner] 创建后端虚拟环境...")
                self._run([str(python), "-m", "venv", str(BACKEND_VENV)], cwd=BACKEND_DIR)

            self._backend_python = str(venv_python)
            stamp = BACKEND_VENV / ".requirements.installed"
            if (not stamp.exists()) or stamp.stat().st_mtime < req.stat().st_mtime:
                print("[runner] 安装后端依赖...")
                self._run([self._backend_python, "-m", "pip", "install", "-r", "requirements.txt"], cwd=BACKEND_DIR)
                stamp.touch()

    def _ensure_frontend_node_modules(self) -> None:
        node_modules = FRONTEND_DIR / "node_modules"
        package_lock = FRONTEND_DIR / "package-lock.json"
        package_json = FRONTEND_DIR / "package.json"
        if (not node_modules.exists()) or (
            package_lock.exists() and node_modules.stat().st_mtime < max(package_json.stat().st_mtime, package_lock.stat().st_mtime)
        ):
            print("[runner] 安装前端依赖...")
            npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
            self._run([npm_cmd, "install"], cwd=FRONTEND_DIR)

    def _choose_runtime(self) -> None:
        if self.args.use_docker_infra:
            if not self._docker_available():
                raise SystemExit("[runner] 你指定了 --use-docker-infra，但当前找不到可用的 Docker。")
            print("[runner] 使用 Docker 启动 PostgreSQL 与 Redis...")
            self._run(["docker", "compose", "up", "-d", "db", "redis"], cwd=ROOT)
            self.started_docker_services = True
            self._wait_for_port("127.0.0.1", 5432, "PostgreSQL")
            self._wait_for_port("127.0.0.1", 6379, "Redis")
            self.database_url = "postgresql+asyncpg://sandbox:sandbox@localhost:5432/sandbox"
            self.redis_url = "redis://localhost:6379/0"
            self.runtime_mode = "docker-pg"
            return

        if self.args.use_local_pg:
            if not self._is_port_open("127.0.0.1", 5432):
                raise SystemExit("[runner] 你指定了 --use-local-pg，但本地 5432 没有可用 PostgreSQL。")
            self.database_url = "postgresql+asyncpg://sandbox:sandbox@localhost:5432/sandbox"
            self.redis_url = "redis://localhost:6379/0"
            self.runtime_mode = "local-pg"
            print("[runner] 使用本地 PostgreSQL 模式。")
            return

        LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.database_url = f"sqlite+aiosqlite:///{LOCAL_SQLITE_PATH}"
        self.redis_url = "redis://localhost:6379/0"
        self.runtime_mode = "sqlite"
        print(f"[runner] 使用本地 SQLite 模式: {LOCAL_SQLITE_PATH}")

    def _run_alembic(self) -> None:
        print("[runner] 执行数据库迁移...")
        env = os.environ.copy()
        env["DATABASE_URL"] = self.database_url
        env["REDIS_URL"] = self.redis_url
        self._run(
            [self._backend_python, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND_DIR,
            env=env,
        )

    def _start_backend(self) -> None:
        print("[runner] 启动后端...")
        env = os.environ.copy()
        env["DATABASE_URL"] = self.database_url
        env["REDIS_URL"] = self.redis_url
        # 沙盒环境默认放开 CORS，避免 VPS 上切 IP、换域名后前端动态数据被跨域拦截。
        env["CORS_ORIGINS"] = json.dumps(["*"])
        process = self._spawn(
            "backend",
            [
                self._backend_python,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(BACKEND_PORT),
                "--log-level",
                "warning",
                "--no-access-log",
            ],
            cwd=BACKEND_DIR,
            env=env,
        )
        self._wait_for_port("127.0.0.1", BACKEND_PORT, "后端")
        self.processes.append(process)

    def _start_frontend(self) -> None:
        print("[runner] 启动前端...")
        env = os.environ.copy()
        if self.args.public_host:
            host = self.args.public_host
            env["VITE_API_BASE_URL"] = f"http://{host}:{BACKEND_PORT}/api/v1"
            env["VITE_PUBLIC_WS_URL"] = f"ws://{host}:{BACKEND_PORT}/ws/public"
            env["VITE_PRIVATE_WS_URL"] = f"ws://{host}:{BACKEND_PORT}/ws/private"
        else:
            env.pop("VITE_API_BASE_URL", None)
            env.pop("VITE_PUBLIC_WS_URL", None)
            env.pop("VITE_PRIVATE_WS_URL", None)
        env["VITE_MANUAL_API_KEY"] = "manual-demo-key"
        env["VITE_MANUAL_API_SECRET"] = "manual-demo-secret"
        env["VITE_ADMIN_API_KEY"] = "admin-demo-key"
        npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
        process = self._spawn(
            "frontend",
            [npm_cmd, "run", "dev", "--", "--host", "0.0.0.0", "--port", str(FRONTEND_PORT)],
            cwd=FRONTEND_DIR,
            env=env,
        )
        self._wait_for_port("127.0.0.1", FRONTEND_PORT, "前端")
        self.processes.append(process)

    def _wait_loop(self) -> int:
        try:
            while True:
                for managed in self.processes:
                    code = managed.process.poll()
                    if code is not None:
                        print(f"[runner] {managed.name} 已退出，exit code={code}")
                        return code
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[runner] 收到中断信号。")
            return 0
        finally:
            self.stop()

    def _spawn(
        self,
        name: str,
        cmd: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> ManagedProcess:
        kwargs: dict = {}
        if os.name != "nt":
            kwargs["preexec_fn"] = os.setsid
        else:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            **kwargs,
        )
        assert process.stdout is not None
        thread = threading.Thread(target=self._stream_output, args=(name, process.stdout), daemon=True)
        thread.start()
        return ManagedProcess(name, process)

    def _stream_output(self, name: str, pipe) -> None:
        for line in pipe:
            print(f"[{name}] {line}", end="")

    def _terminate_process(self, managed: ManagedProcess) -> None:
        process = managed.process
        if process.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", 1)
                os.kill(process.pid, ctrl_break)
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
        except Exception:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except Exception:
                pass

    def _docker_available(self) -> bool:
        try:
            docker = subprocess.run(
                ["docker", "info"],
                cwd=ROOT,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return docker.returncode == 0
        except FileNotFoundError:
            return False

    def _wait_for_port(self, host: str, port: int, label: str, timeout: int = 60) -> None:
        start = time.time()
        while time.time() - start < timeout:
            if self._is_port_open(host, port):
                return
            time.sleep(0.5)
        raise SystemExit(f"[runner] {label} 在 {timeout}s 内未成功启动。")

    @staticmethod
    def _is_port_open(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((host, port)) == 0

    @staticmethod
    def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
        result = subprocess.run(cmd, cwd=cwd, env=env, text=True)
        if result.returncode != 0:
            raise SystemExit(f"[runner] 命令执行失败: {' '.join(cmd)}")

    def _print_banner(self) -> None:
        print("[runner] 已启动。")
        if self._host != "localhost":
            print(f"[runner] 检测到本机地址: {self._host}（可通过 http://{self._host}:{FRONTEND_PORT} 访问）")
        print(f"[runner] 模式: {self.runtime_mode}")
        host = self._host
        print(f"[runner] 前端: http://{host}:{FRONTEND_PORT}")
        print(f"[runner] 后端: http://{host}:{BACKEND_PORT}")
        print(f"[runner] OpenAPI: http://{host}:{BACKEND_PORT}/docs")
        print("[runner] 关闭本程序即可停止前端、后端，以及本程序自动拉起的 db/redis。")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="一键启动现货做市沙盒")
    parser.add_argument(
        "--use-docker-infra",
        action="store_true",
        help="使用 Docker 拉起 PostgreSQL/Redis。",
    )
    parser.add_argument(
        "--use-local-pg",
        action="store_true",
        help="使用本地 PostgreSQL:5432；默认不需要。",
    )
    parser.add_argument(
        "--no-venv",
        action="store_true",
        help="不使用虚拟环境，直接用系统 Python 运行（适合 VPS 等环境）。",
    )
    parser.add_argument(
        "--public-host",
        metavar="HOST",
        default="",
        help="手动指定公网域名或 IP（可选）。不填则自动检测本机 IP，换服务器可直接运行。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = SandboxRunner(args)
    atexit.register(runner.stop)
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
