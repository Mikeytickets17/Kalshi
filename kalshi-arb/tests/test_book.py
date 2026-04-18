"""OrderBook + halted-state-machine unit tests."""

from __future__ import annotations

from kalshi_arb.scanner.book import BookState, OrderBook


def _book(**kw) -> OrderBook:
    return OrderBook(ticker="TEST-1", empty_halt_sec=2.0, recover_sec=5.0, **kw)


def test_apply_delta_adds_and_subtracts() -> None:
    b = _book()
    b.apply_delta("yes", 40, 10, now=0.0)
    b.apply_delta("yes", 40, -4, now=0.0)
    assert b.yes_levels[40] == 6


def test_best_ask_finds_lowest_nonzero() -> None:
    b = _book()
    b.apply_delta("yes", 70, 5, now=0.0)
    b.apply_delta("yes", 40, 3, now=0.0)
    b.apply_delta("yes", 55, 2, now=0.0)
    level = b.best_ask("yes")
    assert level is not None
    assert level.price_cents == 40
    assert level.qty == 3


def test_fully_empty_book_is_halted_after_threshold() -> None:
    """BOTH sides empty past empty_halt_sec -> HALTED. One-sided book is
    NOT halted (scanner handles that via SKIP_EMPTY)."""
    b = _book()
    # Populate both sides then remove everything.
    b.apply_delta("yes", 40, 5, now=0.0)
    b.apply_delta("no", 55, 5, now=0.0)
    assert not b.is_halted(now=0.0)
    b.apply_delta("yes", 40, -5, now=0.1)
    b.apply_delta("no", 55, -5, now=0.1)
    # Still within empty_halt_sec=2.0
    assert not b.is_halted(now=0.5)
    # Past threshold -> HALTED
    assert b.is_halted(now=3.0)


def test_one_sided_book_is_not_halted() -> None:
    """Scanner handles one-sided books via SKIP_EMPTY, not SKIP_HALTED."""
    b = _book()
    b.apply_delta("yes", 40, 5, now=0.0)
    # Only YES populated. No "both sides empty" condition exists.
    assert not b.is_halted(now=10.0)  # well past empty_halt_sec


def test_book_recovers_only_after_recover_sec() -> None:
    b = _book()
    # Start halted
    b.mark_paused(now=0.0)
    assert b.is_halted(now=0.0)
    # Both sides come back
    b.apply_delta("yes", 40, 5, now=10.0)
    b.apply_delta("no", 55, 5, now=10.0)
    # Still halted immediately after populate
    assert b.is_halted(now=10.0)
    assert b.is_halted(now=12.0)  # within recover_sec=5.0
    # Past recovery -> ACTIVE
    assert not b.is_halted(now=16.0)
    assert b.state is BookState.ACTIVE


def test_mark_paused_forces_halted_state() -> None:
    b = _book()
    b.apply_delta("yes", 40, 5, now=0.0)
    b.apply_delta("no", 55, 5, now=0.0)
    assert not b.is_halted(now=0.5)
    b.mark_paused(now=1.0)
    assert b.is_halted(now=1.0)


def test_snapshot_replaces_all_levels() -> None:
    b = _book()
    b.apply_delta("yes", 40, 100, now=0.0)
    b.apply_snapshot(
        yes_levels=[[55, 5], [60, 10]],
        no_levels=[[44, 3]],
        seq=42,
        now=1.0,
    )
    assert b.yes_levels[40] == 0   # old level cleared
    assert b.yes_levels[55] == 5
    assert b.yes_levels[60] == 10
    assert b.no_levels[44] == 3
    assert b.last_seq == 42


def test_delta_cannot_drive_level_negative() -> None:
    b = _book()
    b.apply_delta("yes", 40, 2, now=0.0)
    b.apply_delta("yes", 40, -5, now=0.0)  # would go negative
    assert b.yes_levels[40] == 0  # clamped to zero
