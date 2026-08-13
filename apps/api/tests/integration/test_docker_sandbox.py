import os
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
import os

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

print(f'UID={os.getuid()}')
try:
    Path('/root-write-probe').write_text('blocked')
    print('ROOT_WRITABLE')
except OSError:
    print('ROOT_READ_ONLY')

Path('/output/result.txt').write_text('可审计产物')
""",
        input_mounts=[],
        output_dir=output_dir,
        timeout_seconds=10,
    )

    result = sandbox.execute(request)

    assert result.status == "completed", result.stderr
    assert result.stdout.splitlines() == [
        "HOST_FILE_BLOCKED",
        "NETWORK_BLOCKED",
        f"UID={os.getuid()}",
        "ROOT_READ_ONLY",
    ]
    assert os.getuid() != 0
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


def test_sandbox_only_forwards_docker_connection_environment(tmp_path, monkeypatch) -> None:
    """验证 Docker CLI 可选择 daemon 且不会继承应用凭证"""
    captured_env: dict[str, str] = {}
    captured_command: list[str] = []

    def completed(*args, **kwargs):
        captured_command.extend(args[0])
        captured_env.update(kwargs["env"])
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/docker.sock")
    monkeypatch.setenv("DEEP_RESEARCHER_OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setattr(subprocess, "run", completed)

    DockerSandbox(image="python:3.13-slim").execute(
        SandboxRequest(
            code="print('ok')",
            input_mounts=[],
            output_dir=tmp_path / "output",
            timeout_seconds=2,
        )
    )

    assert captured_env["DOCKER_HOST"] == "unix:///tmp/docker.sock"
    assert "DEEP_RESEARCHER_OPENAI_API_KEY" not in captured_env
    assert [
        captured_command[index + 1]
        for index, value in enumerate(captured_command)
        if value == "--network"
    ] == ["none"]
    assert "--read-only" in captured_command
    assert [
        captured_command[index + 1]
        for index, value in enumerate(captured_command)
        if value == "--pids-limit"
    ] == ["64"]
    assert [
        captured_command[index + 1]
        for index, value in enumerate(captured_command)
        if value == "--memory"
    ] == ["256m"]
    assert [
        captured_command[index + 1]
        for index, value in enumerate(captured_command)
        if value == "--cpus"
    ] == ["0.5"]
