import shutil
import subprocess
from pathlib import Path

import pytest
from deep_researcher.sandbox import (
    DockerSandbox,
    SandboxRequest,
    SandboxUnavailableError,
)


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI 不可用")
def test_sandbox_blocks_unapproved_host_files_and_network(tmp_path) -> None:
    sandbox = DockerSandbox(image="python:3.13-slim")
    output_dir = Path("var/sandbox-tests") / tmp_path.name
    request = SandboxRequest(
        code="""
from pathlib import Path
import socket

try:
    Path('/Users/lystran/.ssh/id_rsa').read_text()
    print('HOST_FILE_LEAKED')
except OSError:
    print('HOST_FILE_BLOCKED')

try:
    socket.create_connection(('example.com', 443), timeout=2)
    print('NETWORK_LEAKED')
except OSError:
    print('NETWORK_BLOCKED')

Path('/output/result.txt').write_text('可审计产物')
""",
        input_mounts=[],
        output_dir=output_dir,
        timeout_seconds=10,
    )

    result = sandbox.execute(request)

    assert result.status == "completed", result.stderr
    assert result.stdout.splitlines() == ["HOST_FILE_BLOCKED", "NETWORK_BLOCKED"]
    assert result.artifacts == [output_dir.resolve() / "result.txt"]
    assert result.artifacts[0].read_text() == "可审计产物"


def test_sandbox_reports_unavailable_daemon_without_running_host_python(
    tmp_path, monkeypatch
) -> None:
    def unavailable(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=125,
            stdout="",
            stderr="Cannot connect to the Docker daemon",
        )

    monkeypatch.setattr(subprocess, "run", unavailable)
    sandbox = DockerSandbox(image="python:3.13-slim")

    with pytest.raises(SandboxUnavailableError, match="Docker daemon 不可用"):
        sandbox.execute(
            SandboxRequest(
                code="raise AssertionError('绝不能由宿主 Python 执行')",
                input_mounts=[],
                output_dir=tmp_path / "output",
                timeout_seconds=2,
            )
        )
