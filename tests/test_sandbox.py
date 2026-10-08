"""Unit tests for OptChat Sandbox Manager."""

from pathlib import Path
from unittest.mock import patch

from optchat.sandbox import SandboxBackend, SandboxConfig, SandboxManager


def test_sandbox_detect_backend():
    assert SandboxManager.detect_backend(preferred="none") == SandboxBackend.NONE

    with patch("shutil.which", return_value="/usr/bin/bwrap"):
        assert SandboxManager.detect_backend() == SandboxBackend.BWRAP
        assert SandboxManager.detect_backend(preferred="bwrap") == SandboxBackend.BWRAP

    with patch("shutil.which", side_effect=lambda x: "/data/data/com.termux/files/usr/bin/proot" if x == "proot" else None):
        assert SandboxManager.detect_backend() == SandboxBackend.PROOT
        assert SandboxManager.detect_backend(preferred="proot") == SandboxBackend.PROOT

    with patch("shutil.which", side_effect=lambda x: "/usr/bin/unshare" if x == "unshare" else None):
        assert SandboxManager.detect_backend() == SandboxBackend.UNSHARE

    with patch("shutil.which", return_value=None):
        assert SandboxManager.detect_backend() == SandboxBackend.NONE


def test_sandbox_bwrap_wrapping(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = SandboxConfig(
        workspace=workspace,
        backend=SandboxBackend.BWRAP,
        rw_dirs=[workspace],
        hidden_dirs=[tmp_path / "fake_ssh"],
        allow_network=False,
        isolate_pid=True,
    )

    with patch("shutil.which", return_value="/usr/bin/bwrap"):
        cmd = ["agy", "explain", "code"]
        wrapped = SandboxManager.wrap_command(cmd, config)
        assert wrapped[0] == "/usr/bin/bwrap"
        assert "--ro-bind" in wrapped
        assert "--unshare-net" in wrapped
        assert "--unshare-pid" in wrapped
        assert str(workspace) in wrapped
        assert wrapped[-3:] == ["--", "agy", "explain"] or wrapped[-4:] == ["--", "agy", "explain", "code"]


def test_sandbox_proot_wrapping(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = SandboxConfig(
        workspace=workspace,
        backend=SandboxBackend.PROOT,
        rw_dirs=[workspace],
    )

    with patch("shutil.which", return_value="/usr/bin/proot"):
        cmd = ["agy", "--version"]
        wrapped = SandboxManager.wrap_command(cmd, config)
        assert wrapped[0] == "/usr/bin/proot"
        assert "-0" in wrapped
        assert "-r" in wrapped
        assert "-b" in wrapped
        assert str(workspace) in wrapped
        assert wrapped[-3:] == ["--", "agy", "--version"]


def test_sandbox_none_backend(tmp_path: Path):
    workspace = tmp_path / "workspace"
    config = SandboxConfig(workspace=workspace, backend=SandboxBackend.NONE)
    cmd = ["echo", "hello"]
    assert SandboxManager.wrap_command(cmd, config) == cmd


def test_sandbox_env_filter(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "super_secret_token")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("GEMINI_API_KEY", "valid_gemini_key")

    config = SandboxConfig(workspace=Path.cwd())
    filtered = SandboxManager.filter_env(config)

    assert "PATH" in filtered
    assert "GEMINI_API_KEY" in filtered
    assert "SECRET_KEY" not in filtered
