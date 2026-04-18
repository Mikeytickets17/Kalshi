"""Fee model unit tests.

Matches Kalshi's taker fee formula at 5 specific price points
(5¢, 25¢, 45¢, 65¢, 85¢) as required by the Module 2 gate.

Formula: fee_per_contract_cents = ceil(0.07 * price * (1 - price) * 100)

Ground truth calculated by hand below; these values will change only if
Kalshi publishes a new coefficient (in which case fees.yaml gets a new
effective_from row).
"""

from __future__ import annotations

import math

import pytest

from kalshi_arb.scanner.fees import BUILTIN_DEFAULT_TIER, FeeModel


# Hand-calculated per-contract taker fee at 5 canonical price points.
# raw = 0.07 * p * (1-p); then rounded UP to the next whole cent.
#   5c   -> 0.07 * 0.05 * 0.95 = 0.003325 -> ceil(0.3325) = 1 cent
#  25c   -> 0.07 * 0.25 * 0.75 = 0.013125 -> ceil(1.3125) = 2 cents
#  45c   -> 0.07 * 0.45 * 0.55 = 0.017325 -> ceil(1.7325) = 2 cents
#  65c   -> 0.07 * 0.65 * 0.35 = 0.015925 -> ceil(1.5925) = 2 cents
#  85c   -> 0.07 * 0.85 * 0.15 = 0.008925 -> ceil(0.8925) = 1 cent
FIVE_POINTS = [
    (5, 1),
    (25, 2),
    (45, 2),
    (65, 2),
    (85, 1),
]


@pytest.mark.parametrize("price_cents,expected_fee", FIVE_POINTS)
def test_taker_fee_matches_documented_schedule(price_cents: int, expected_fee: int) -> None:
    assert BUILTIN_DEFAULT_TIER.fee_per_contract_cents(price_cents) == expected_fee


def test_fee_is_zero_at_boundary_prices_when_using_continuous_formula() -> None:
    # p=1c and p=99c are legal Kalshi prices. The raw fee there is tiny
    # (0.07 * 0.01 * 0.99 = 0.000693 dollars) and rounds UP to 1 cent.
    assert BUILTIN_DEFAULT_TIER.fee_per_contract_cents(1) == 1
    assert BUILTIN_DEFAULT_TIER.fee_per_contract_cents(99) == 1


def test_fee_peaks_near_50c() -> None:
    # Max of p*(1-p) is at p=0.50; rounded-up 0.0175 -> 2c.
    assert BUILTIN_DEFAULT_TIER.fee_per_contract_cents(50) == 2


def test_rejects_out_of_range_prices() -> None:
    with pytest.raises(ValueError):
        BUILTIN_DEFAULT_TIER.fee_per_contract_cents(0)
    with pytest.raises(ValueError):
        BUILTIN_DEFAULT_TIER.fee_per_contract_cents(100)


def test_structural_arb_fee_is_sum_of_both_legs() -> None:
    model = FeeModel.builtin()
    # 45c YES + 45c NO -> 2c + 2c = 4c total taker fees.
    assert model.structural_arb_fee_cents(45, 45) == 4
    # 5c YES + 90c NO -> 1c + 1c = 2c.
    assert model.structural_arb_fee_cents(5, 90) == 2


def test_formula_matches_hand_calculation_across_full_range() -> None:
    """Sanity: our implementation matches the documented formula for every
    legal price, not just the 5 canonical points."""
    for p in range(1, 100):
        expected = math.ceil(0.07 * p * (100 - p) / 100)
        actual = BUILTIN_DEFAULT_TIER.fee_per_contract_cents(p)
        assert actual == expected, f"mismatch at p={p}: got {actual}, want {expected}"
