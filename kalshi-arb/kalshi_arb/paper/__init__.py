"""Paper-mode runner.

The `kalshi-arb paper` CLI wires this module end-to-end:
scanner -> sizer -> PaperKalshiAPI -> EventStore, fed by either
the real Kalshi WS (production paper) or an in-process FakeWSSource
(`--smoke-test` mode).

Public:
    PaperRunner     -- the orchestrator
    FakeWSSource    -- deterministic delta generator for tests + smoke test
"""

from .fake_ws import FakeWSSource, SyntheticDelta
from .runner import PaperRunner, PaperRunnerConfig

__all__ = [
    "FakeWSSource",
    "PaperRunner",
    "PaperRunnerConfig",
    "SyntheticDelta",
]
