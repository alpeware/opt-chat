"""OptChat Sandbox Manager.

Enforces isolated runtime environments for OptChat daemon and child agy invocations.
Supports:
- Bubblewrap (bwrap) on Gentoo / standard Linux.
- PRoot (proot) on Android Termux.
- Unshare (util-linux) as lightweight Linux namespace isolation.
- None (direct execution fallback or debugging).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
import shutil
from typing import Dict, List, Optional


class SandboxBackend(str, Enum):
    BWRAP = "bwrap"
    PROOT = "proot"
    UNSHARE = "unshare"
    NONE = "none"


@dataclass
class SandboxConfig:
    workspace: Path
    backend: SandboxBackend = SandboxBackend.NONE
    rw_dirs: List[Path] = field(default_factory=list)
    ro_dirs: List[Path] = field(default_factory=list)
    hidden_dirs: List[Path] = field(default_factory=list)
    allow_network: bool = True
    isolate_pid: bool = True
    env_whitelist: List[str] = field(
        default_factory=lambda: [
            "PATH",
            "HOME",
            "USER",
            "LANG",
            "TERM",
            "GEMINI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "OPTCHAT_DIR",
            "OPTCHAT_WORKSPACE",
        ]
    )


class SandboxManager:
    """Detects available sandboxing utilities and wraps execution commands."""

    @staticmethod
    def detect_backend(preferred: Optional[str] = None) -> SandboxBackend:
        """Detect the optimal sandbox backend available on this host."""
        if preferred:
            pref_lower = preferred.lower().strip()
            if pref_lower in ("none", "off", "disable", "disabled"):
                return SandboxBackend.NONE
            if pref_lower == "bwrap" and shutil.which("bwrap"):
                return SandboxBackend.BWRAP
            if pref_lower == "proot" and shutil.which("proot"):
                return SandboxBackend.PROOT
            if pref_lower == "unshare" and shutil.which("unshare"):
                return SandboxBackend.UNSHARE

        # Auto-detection priority: bwrap -> proot -> unshare -> none
        if shutil.which("bwrap"):
            return SandboxBackend.BWRAP
        if shutil.which("proot"):
            return SandboxBackend.PROOT
        if shutil.which("unshare"):
            return SandboxBackend.UNSHARE
        return SandboxBackend.NONE

    @staticmethod
    def create_default_config(
        workspace: Path,
        backend: Optional[SandboxBackend] = None,
        optchat_dir: Optional[Path] = None,
    ) -> SandboxConfig:
        """Construct standard sandbox policy for workspace and OptChat storage."""
        ws_resolved = Path(workspace).resolve()
        optchat_resolved = Path(optchat_dir or os.path.expanduser("~/.optchat")).resolve()
        home = Path(os.path.expanduser("~")).resolve()

        gemini_resolved = Path(os.environ.get("ANTIGRAVITY_APP_DATA_DIR", home / ".gemini")).resolve()
        base_gemini = (home / ".gemini").resolve()

        rw = [ws_resolved, optchat_resolved]
        if gemini_resolved.exists():
            rw.append(gemini_resolved)
        if base_gemini.exists() and base_gemini not in rw:
            rw.append(base_gemini)

        hidden = [
            home / ".ssh",
            home / ".gnupg",
            home / ".bash_history",
            home / ".zsh_history",
        ]

        be = backend or SandboxManager.detect_backend()
        return SandboxConfig(
            workspace=ws_resolved,
            backend=be,
            rw_dirs=rw,
            hidden_dirs=hidden,
            allow_network=True,
            isolate_pid=True,
        )

    @classmethod
    def wrap_command(cls, cmd: List[str], config: SandboxConfig) -> List[str]:
        """Wrap command with sandbox isolation flags according to config."""
        if config.backend == SandboxBackend.NONE or not cmd:
            return list(cmd)

        if config.backend == SandboxBackend.BWRAP:
            return cls._wrap_bwrap(cmd, config)
        elif config.backend == SandboxBackend.PROOT:
            return cls._wrap_proot(cmd, config)
        elif config.backend == SandboxBackend.UNSHARE:
            return cls._wrap_unshare(cmd, config)

        return list(cmd)

    @staticmethod
    def _wrap_bwrap(cmd: List[str], config: SandboxConfig) -> List[str]:
        bwrap = shutil.which("bwrap") or "bwrap"
        args = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]

        # Bind read-write directories
        for rw in config.rw_dirs:
            if rw.exists():
                args.extend(["--bind", str(rw), str(rw)])

        # Mask sensitive directories
        for h in config.hidden_dirs:
            if h.exists():
                args.extend(["--tmpfs", str(h)])

        if not config.allow_network:
            args.append("--unshare-net")

        if config.isolate_pid:
            args.append("--unshare-pid")

        args.append("--")
        args.extend(cmd)
        return args

    @staticmethod
    def _wrap_proot(cmd: List[str], config: SandboxConfig) -> List[str]:
        proot = shutil.which("proot") or "proot"
        args = [proot, "-0", "-r", "/"]

        # Bind read-write directories in proot
        for rw in config.rw_dirs:
            if rw.exists():
                args.extend(["-b", str(rw)])

        # Mask sensitive directories by binding empty/tmp location if desired
        args.append("--")
        args.extend(cmd)
        return args

    @staticmethod
    def _wrap_unshare(cmd: List[str], config: SandboxConfig) -> List[str]:
        unshare = shutil.which("unshare") or "unshare"
        args = [unshare, "--user", "--map-root-user", "--mount"]
        if config.isolate_pid:
            args.extend(["--pid", "--fork"])
        args.append("--")
        args.extend(cmd)
        return args

    @staticmethod
    def filter_env(config: SandboxConfig) -> Dict[str, str]:
        """Filter host environment to whitelist permissible variables and all Antigravity/Gemini settings."""
        filtered = {}
        for k in config.env_whitelist:
            if k in os.environ:
                filtered[k] = os.environ[k]
        for k, v in os.environ.items():
            if k.startswith("ANTIGRAVITY_") or k.startswith("GEMINI_"):
                filtered[k] = v
        return filtered

