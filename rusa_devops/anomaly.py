"""Bayesian anomaly scoring with robust baseline estimation."""
from __future__ import annotations

import math
from statistics import median

from .contracts import AnomalyAssessment


class BayesianAnomalyDetector:
    """Estimate P(anomaly | observation) from normal/anomaly likelihoods and a prior."""

    def __init__(self, prior: float = 0.01, posterior_threshold: float = 0.90, anomaly_scale: float = 5.0):
        if not 0 < prior < 1:
            raise ValueError("prior must be between zero and one")
        if not 0.5 <= posterior_threshold < 1:
            raise ValueError("posterior_threshold must be in [0.5, 1)")
        if anomaly_scale <= 1:
            raise ValueError("anomaly_scale must be greater than one")
        self.prior = prior
        self.posterior_threshold = posterior_threshold
        self.anomaly_scale = anomaly_scale
        self.center = 0.0
        self.scale = 1.0
        self.fitted = False

    def fit(self, baseline_values) -> "BayesianAnomalyDetector":
        values = [float(value) for value in baseline_values if math.isfinite(float(value))]
        if len(values) < 20:
            raise ValueError("at least 20 finite baseline values are required")
        self.center = median(values)
        self.scale = max(1.4826 * median([abs(value - self.center) for value in values]), 1e-6)
        self.fitted = True
        return self

    def assess(self, values, timestamps=None) -> list[AnomalyAssessment]:
        if not self.fitted:
            raise RuntimeError("anomaly detector must be fitted before assessment")
        output = []
        for index, raw in enumerate(values):
            value = float(raw)
            z_score = (value - self.center) / self.scale
            normal_log = -math.log(math.sqrt(2.0 * math.pi)) - 0.5 * z_score * z_score
            anomaly_log = (
                -math.log(self.anomaly_scale * math.sqrt(2.0 * math.pi))
                - 0.5 * (z_score / self.anomaly_scale) ** 2
            )
            log_odds = math.log(self.prior / (1.0 - self.prior)) + anomaly_log - normal_log
            if log_odds >= 0:
                posterior = 1.0 / (1.0 + math.exp(-min(log_odds, 700.0)))
            else:
                odds = math.exp(max(log_odds, -700.0))
                posterior = odds / (1.0 + odds)
            stamp = str(timestamps[index]) if timestamps is not None else str(index)
            output.append(AnomalyAssessment(
                observed_at=stamp, value=value, z_score=z_score,
                posterior_probability=posterior,
                is_anomaly=posterior >= self.posterior_threshold,
            ))
        return output
