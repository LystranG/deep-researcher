import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import uuid4


@dataclass(frozen=True)
class SandboxInputMount:
    host_path: Path
    container_name: str


@dataclass(frozen=True)
class SandboxRequest:
    code: str
    input_mounts: list[SandboxInputMount]
    output_dir: Path
    timeout_seconds: int


@dataclass(frozen=True)
class SandboxResult:
    status: str
    stdout: str
    stderr: str
    artifacts: list[Path]


class SandboxUnavailableError(RuntimeError):
    pass


class DockerSandbox:
    """只通过短生命周期 Docker 容器执行用户代码，永不回退宿主解释器。"""

    def __init__(self, *, image: str) -> None:
        self._image = image
        self._active_containers: dict[str, str] = {}
        self._cancelled_before_start: set[str] = set()
        self._lock = Lock()

    def execute(
        self, request: SandboxRequest, *, execution_key: str | None = None
    ) -> SandboxResult:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        output_dir = request.output_dir.resolve()
        output_dir.chmod(0o777)
        container_name = f"deep-researcher-sandbox-{uuid4().hex}"
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--cpus",
            "0.5",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=32m",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--volume",
            f"{output_dir}:/output:rw",
        ]
        for mount in request.input_mounts:
            host_path = mount.host_path.resolve(strict=True)
            safe_name = Path(mount.container_name).name
            if safe_name != mount.container_name or safe_name in {"", ".", ".."}:
                raise ValueError("沙箱输入名称无效")
            command.extend(["--volume", f"{host_path}:/inputs/{safe_name}:ro"])
        command.extend([self._image, "python", "-I", "-c", request.code])

        if execution_key is not None:
            with self._lock:
                self._active_containers[execution_key] = container_name
                if execution_key in self._cancelled_before_start:
                    self._cancelled_before_start.remove(execution_key)
                    self._active_containers.pop(execution_key, None)
                    return SandboxResult(
                        status="cancelled", stdout="", stderr="执行已取消", artifacts=[]
                    )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
                env={"PATH": os.environ.get("PATH", "")},
            )
        except FileNotFoundError as exc:
            raise SandboxUnavailableError("Docker CLI 不可用") from exc
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                timeout=10,
                check=False,
                env={"PATH": os.environ.get("PATH", "")},
            )
            return SandboxResult(status="timed_out", stdout="", stderr="执行超时", artifacts=[])
        finally:
            if execution_key is not None:
                with self._lock:
                    self._active_containers.pop(execution_key, None)

        if completed.returncode == 125 and any(
            marker in completed.stderr.casefold()
            for marker in ("cannot connect", "is the docker daemon running", "error during connect")
        ):
            raise SandboxUnavailableError("Docker daemon 不可用")

        artifacts = sorted(
            path for path in output_dir.rglob("*") if path.is_file() and not path.is_symlink()
        )
        output_dir.chmod(0o700)
        status = "completed" if completed.returncode == 0 else "failed"
        return SandboxResult(
            status=status,
            stdout=completed.stdout[-100_000:],
            stderr=completed.stderr[-100_000:],
            artifacts=artifacts,
        )

    def cancel(self, execution_key: str) -> bool:
        with self._lock:
            container_name = self._active_containers.get(execution_key)
            if container_name is None:
                self._cancelled_before_start.add(execution_key)
                return True
        for _ in range(40):
            try:
                completed = subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    timeout=10,
                    check=False,
                    env={"PATH": os.environ.get("PATH", "")},
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return False
            if completed.returncode == 0:
                return True
            time.sleep(0.05)
        return False
