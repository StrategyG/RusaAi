"""Nash-equilibrium analysis and zero-sum mixed-strategy solver."""
from __future__ import annotations

import numpy as np
from scipy.optimize import linprog


def pure_nash_equilibria(row_payoff, column_payoff) -> list[dict]:
    """Find pure Nash equilibria for a finite two-player normal-form game."""
    row = np.asarray(row_payoff, dtype=float)
    col = np.asarray(column_payoff, dtype=float)
    if row.ndim != 2 or row.shape != col.shape:
        raise ValueError("Payoff matrices must be two-dimensional and have equal shapes.")
    if not np.isfinite(row).all() or not np.isfinite(col).all():
        raise ValueError("Payoff matrices must contain finite values.")

    equilibria = []
    for i in range(row.shape[0]):
        for j in range(row.shape[1]):
            row_best = row[i, j] >= np.max(row[:, j]) - 1e-12
            col_best = col[i, j] >= np.max(col[i, :]) - 1e-12
            if row_best and col_best:
                equilibria.append(
                    {
                        "row_strategy": i,
                        "column_strategy": j,
                        "row_payoff": float(row[i, j]),
                        "column_payoff": float(col[i, j]),
                    }
                )
    return equilibria


def solve_zero_sum_game(payoff_matrix) -> dict:
    """Solve a finite zero-sum matrix game with linear programming."""
    payoff = np.asarray(payoff_matrix, dtype=float)
    if payoff.ndim != 2 or min(payoff.shape) < 2:
        raise ValueError("payoff_matrix must be a 2D matrix with at least 2 strategies per side.")
    if not np.isfinite(payoff).all():
        raise ValueError("payoff_matrix must contain finite values.")

    rows, cols = payoff.shape

    # Row player maximizes v subject to A.T @ p >= v and sum(p) = 1.
    row_objective = np.r_[np.zeros(rows), -1.0]
    row_inequality = np.hstack([-payoff.T, np.ones((cols, 1))])
    row_equality = np.r_[np.ones(rows), 0.0].reshape(1, -1)
    row_result = linprog(
        row_objective,
        A_ub=row_inequality,
        b_ub=np.zeros(cols),
        A_eq=row_equality,
        b_eq=np.ones(1),
        bounds=[(0, None)] * rows + [(None, None)],
        method="highs",
    )

    # Column player minimizes w subject to A @ q <= w and sum(q) = 1.
    col_objective = np.r_[np.zeros(cols), 1.0]
    col_inequality = np.hstack([payoff, -np.ones((rows, 1))])
    col_equality = np.r_[np.ones(cols), 0.0].reshape(1, -1)
    col_result = linprog(
        col_objective,
        A_ub=col_inequality,
        b_ub=np.zeros(rows),
        A_eq=col_equality,
        b_eq=np.ones(1),
        bounds=[(0, None)] * cols + [(None, None)],
        method="highs",
    )

    if not row_result.success or not col_result.success:
        raise RuntimeError(
            f"Game solver failed: row={row_result.message}; column={col_result.message}"
        )

    return {
        "row_strategy": row_result.x[:rows].tolist(),
        "column_strategy": col_result.x[:cols].tolist(),
        "value_to_row_player": float(row_result.x[-1]),
        "value_from_column_solver": float(col_result.x[-1]),
    }


def game_theory_engine(row_payoff, column_payoff=None) -> dict:
    """Return pure equilibria and, for zero-sum games, mixed strategies."""
    row = np.asarray(row_payoff, dtype=float)
    col = -row if column_payoff is None else np.asarray(column_payoff, dtype=float)
    result = {"pure_nash_equilibria": pure_nash_equilibria(row, col)}
    if np.allclose(col, -row):
        result["zero_sum_mixed_strategy"] = solve_zero_sum_game(row)
    else:
        result["zero_sum_mixed_strategy"] = None
    return result
