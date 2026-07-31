import os
import sys
import sysconfig

from app.core.constants import ERR_TERMINAL_PTY_PLATFORM_UNSUPPORTED
from app.core.i18n import t


def build_subprocess_env() -> dict[str, str]:
    """Build the subprocess environment used by shell execution."""
    env = os.environ.copy()
    scripts_dir = sysconfig.get_path("scripts")
    if scripts_dir:
        env["PATH"] = scripts_dir + os.pathsep + env.get("PATH", "")
    if sys.prefix != sys.base_prefix:
        env["VIRTUAL_ENV"] = sys.prefix
    return env


def build_interactive_shell_argv(command: str) -> tuple[str, ...]:
    """Build the platform-specific argv for an interactive shell command."""
    if sys.platform == "win32":
        return (os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", command)
    if sys.platform.startswith("linux"):
        return ("/bin/sh", "-c", command)
    raise RuntimeError(t(ERR_TERMINAL_PTY_PLATFORM_UNSUPPORTED, platform=sys.platform))


__all__ = ["build_interactive_shell_argv", "build_subprocess_env"]
