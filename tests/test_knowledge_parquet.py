from pathlib import Path

import pytest

from rusa_knowledge.parquet import iter_telemetry_aggregates


def test_parquet_telemetry_aggregation_is_grouped_and_batched(tmp_path: Path):
    pytest.importorskip("duckdb")
    parquet = tmp_path / "telemetry.parquet"
    connection = duckdb.connect()
    try:
        target = str(parquet).replace("'", "''")
        connection.execute(
            f"""
            COPY (
                SELECT * FROM (VALUES
                    (TIMESTAMP '2026-11-09 10:00:00', 'api', 80.0, 50.0),
                    (TIMESTAMP '2026-11-09 10:05:00', 'api', 90.0, 60.0),
                    (TIMESTAMP '2026-11-09 10:10:00', 'worker', 40.0, 30.0)
                ) AS t(ts, service, cpu_percent, memory_percent)
            ) TO '{target}' (FORMAT PARQUET)
            """
        )
    finally:
        connection.close()

    rows = list(iter_telemetry_aggregates(
        parquet,
        timestamp_column="ts",
        group_column="service",
        metric_columns=["cpu_percent", "memory_percent"],
        bucket_minutes=60,
        batch_size=1,
        max_aggregates=10,
    ))
    assert len(rows) == 2
    api = next(row for row in rows if row["group"] == "api")
    assert api["row_count"] == 2
    assert api["metrics"]["cpu_percent"]["avg"] == pytest.approx(85.0)
    assert api["metrics"]["cpu_percent"]["max"] == pytest.approx(90.0)


def test_parquet_telemetry_rejects_unsafe_identifiers_and_limits(tmp_path: Path):
    pytest.importorskip("duckdb")
    parquet = tmp_path / "empty.parquet"
    import duckdb

    connection = duckdb.connect()
    try:
        target = str(parquet).replace("'", "''")
        connection.execute(f"COPY (SELECT 1 AS id) TO '{target}' (FORMAT PARQUET)")
    finally:
        connection.close()

    with pytest.raises(ValueError, match="simple SQL identifier"):
        list(iter_telemetry_aggregates(
            parquet,
            timestamp_column="ts; DROP TABLE x",
            group_column="service",
            metric_columns=["cpu"],
        ))
