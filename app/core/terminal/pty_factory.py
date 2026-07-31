"""Platform-specific PTY driver factory."""

import sys

from app.core.constants import ERR_TERMINAL_PTY_PLATFORM_UNSUPPORTED
from app.core.i18n import t
from app.core.terminal.pty_base import PtyDriver, PtyProcessConfig


def create_pty_driver(config: PtyProcessConfig) -> PtyDriver:
    """Create the PTY driver supported by the current platform."""
    if sys.platform == "win32":
        from app.core.terminal.pty_windows import WindowsPtyDriver

        return WindowsPtyDriver(config)
    if sys.platform.startswith("linux"):
        from app.core.terminal.pty_unix import LinuxPtyDriver

        return LinuxPtyDriver(config)
    raise RuntimeError(t(ERR_TERMINAL_PTY_PLATFORM_UNSUPPORTED, platform=sys.platform))


__all__ = ["create_pty_driver"]
