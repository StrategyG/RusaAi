"""Two-outcome stake-balancing calculator; not a betting recommendation."""
from __future__ import annotations


def hedge_two_outcomes(
    decimal_odds_a: float,
    decimal_odds_b: float,
    stake_a: float,
) -> dict[str, float | str]:
    """Balance gross payout across two mutually exclusive outcomes.

    Assumes decimal odds, both outcomes settle normally, and no fees/taxes/voids.
    The calculation does not estimate outcome probabilities or guarantee profit.
    """
    odds_a = float(decimal_odds_a)
    odds_b = float(decimal_odds_b)
    stake_a = float(stake_a)
    if odds_a <= 1 or odds_b <= 1 or stake_a <= 0:
        raise ValueError("Decimal odds must be > 1 and stake_a must be > 0.")

    gross_payout = stake_a * odds_a
    stake_b = gross_payout / odds_b
    total_stake = stake_a + stake_b
    net_a = gross_payout - total_stake
    net_b = stake_b * odds_b - total_stake

    return {
        "stake_a": stake_a,
        "stake_b": stake_b,
        "total_stake": total_stake,
        "gross_payout_a": gross_payout,
        "gross_payout_b": stake_b * odds_b,
        "net_if_a_wins": net_a,
        "net_if_b_wins": net_b,
        "worst_case_net": min(net_a, net_b),
        "note": "Excludes fees, taxes, limits, odds movement, voids, and settlement risk.",
    }
