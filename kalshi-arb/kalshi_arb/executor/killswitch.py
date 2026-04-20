"""File-based kill switch.

Presence of the configured sentinel file = halted. Checked before every
order and after every execution result. Auto-trip sources:
  - operator touches the file
  - daily realized P&L breach (executor)
  - DegradedModeDetected (monitor)
  - UnwindFailed (executor)

Reset = manual delete. No code path re-arms automatically.
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import log

_log = log.get("executor.killswitch")


class KillSwitch:
    def __init__(self, sentinel: Path) -> None:
        self.sentinel = sentinel

    def is_tripped(self) -> bool:
        return self.sentinel.exists()

    def trip(self, reason: str) -> None:
        if self.is_tripped():
            _log.info("killswitch.already_tripped", reason=reason)
            return
        self.sentinel.parent.mkdir(parents=True, exist_ok=True)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        body = f"TRIPPED_AT: {now_iso}\nREASON: {reason}\n"
        self.sentinel.write_text(body, encoding="utf-8")
        _log.critical("killswitch.tripped", reason=reason, file=str(self.sentinel))

    def reset(self) -> None:
        """Manual reset only. Present for tests; operator does this by hand in prod."""
        try:
            self.sentinel.unlink()
        except FileNotFoundError:
            pass
        _log.info("killswitch.reset", file=str(self.sentinel))
