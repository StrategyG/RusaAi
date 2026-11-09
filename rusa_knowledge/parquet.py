"""Out-of-core Parquet telemetry aggregation for RAG indexing.

Raw telemetry rows are aggregated in DuckDB before embedding. This avoids
creating one vector per raw event and bounds Python memory by fetch batch size.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MEMORY_LIMIT = re.compile(r"^[1-9][0-9]*(?:KB|MB|GB|TB)$", re.IGNORECASE)

_NUMERIC_TYPES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
    "FLOAT", "DOUBLE", "REAL", "DECIMAL",
)


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a simple SQL identifier")
    return f'"{value}"'


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def iter_telemetry_aggregates(
    file_path: str | Path,
    *,
    timestamp_column: str,
    group_column: str,
    metric_columns: list[str],
    bucket_minutes: int = 60,
    batch_size: int = 1024,
    memory_limit: str = "2GB",
    threads: int = 4,
    temp_directory: str | None = None,
    max_aggregates: int = 1_000_000,
) -> Iterator[dict[str, Any]]:
    """Yield grouped time-window summaries without materializing the Parquet file in RAM."""
    import duckdb

    path = Path(file_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Parquet input must be a regular local file, not a symlink")
    if path.suffix.lower() not in {".parquet", ".pq"} or path.stat().st_size < 1:
        raise ValueError("input must be a non-empty .parquet or .pq file")
    timestamp_sql = _identifier(timestamp_column, "timestamp column")
    group_sql = _identifier(group_column, "group column")
    if not isinstance(metric_columns, list) or not 1 <= len(metric_columns) <= 16:
        raise ValueError("metric_columns must contain between 1 and 16 columns")
    if len(set(metric_columns)) != len(metric_columns):
        raise ValueError("metric_columns must be unique")
    metric_sql = [_identifier(name, "metric column") for name in metric_columns]
    if timestamp_column == group_column or timestamp_column in metric_columns:
        raise ValueError("timestamp, group, and metric columns must be distinct")
    if group_column in metric_columns:
        raise ValueError("group column cannot also be a metric")
    if not 1 <= bucket_minutes <= 10080:
        raise ValueError("bucket_minutes must be between 1 and 10080")
    if not 1 <= batch_size <= 10000:
        raise ValueError("batch_size must be between 1 and 10000")
    if not isinstance(memory_limit, str) or not _MEMORY_LIMIT.fullmatch(memory_limit):
        raise ValueError("memory_limit must look like 2GB, 512MB, or 1TB")
    if isinstance(threads, bool) or not 1 <= threads <= 64:
        raise ValueError("threads must be between 1 and 64")
    if isinstance(max_aggregates, bool) or not 1 <= max_aggregates <= 10_000_000:
        raise ValueError("max_aggregates must be between 1 and 10000000")

    resolved = path.resolve(strict=True)
    path_literal = "'" + str(resolved).replace("'", "''") + "'"
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute(f"SET memory_limit = '{memory_limit.upper()}'")
        connection.execute(f"SET threads = {threads}")
        connection.execute("SET preserve_insertion_order = false")
        if temp_directory:
            temp_path = Path(temp_directory)
            if temp_path.is_symlink():
                raise ValueError("DuckDB temp directory must not be a symlink")
            temp_path.mkdir(parents=True, exist_ok=True)
            if not temp_path.is_dir():
                raise ValueError("DuckDB temp directory must be a directory")
            temp_literal = "'" + str(temp_path.resolve()).replace("'", "''") + "'"
            connection.execute(f"SET temp_directory = {temp_literal}")

        schema = connection.execute(
            f"DESCRIBE SELECT * FROM read_parquet({path_literal}, union_by_name=true)"
        ).fetchall()
        column_types = {row[0]: str(row[1]).upper() for row in schema}
        if timestamp_column not in column_types or group_column not in column_types:
            raise ValueError("timestamp or group column was not found in the Parquet schema")
        timestamp_type = column_types[timestamp_column]
        if "TIMESTAMP" not in timestamp_type and timestamp_type != "DATE":
            raise ValueError("timestamp column must have a DATE or TIMESTAMP type")
        for name in metric_columns:
            if name not in column_types:
                raise ValueError(f"metric column {name!r} was not found in the Parquet schema")
            if not column_types[name].startswith(_NUMERIC_TYPES):
                raise ValueError(f"metric column {name!r} must have a numeric type")

        expressions = []
        for name, column in zip(metric_columns, metric_sql, strict=True):
            numeric = f"TRY_CAST({column} AS DOUBLE)"
            expressions.extend([
                f"AVG({numeric}) AS \"{name}__avg\"",
                f"MAX({numeric}) AS \"{name}__max\"",
                f"COALESCE(STDDEV_SAMP({numeric}), 0.0) AS \"{name}__stddev\"",
                f"APPROX_QUANTILE({numeric}, 0.95) AS \"{name}__p95\"",
            ])
        query = f"""
            SELECT
                COALESCE(CAST({group_sql} AS VARCHAR), 'unknown') AS group_key,
                TIME_BUCKET(INTERVAL '{bucket_minutes} minutes',
                    TRY_CAST({timestamp_sql} AS TIMESTAMP)) AS bucket_start,
                COUNT(*)::BIGINT AS row_count,
                {", ".join(expressions)}
            FROM read_parquet({path_literal}, union_by_name=true)
            WHERE TRY_CAST({timestamp_sql} AS TIMESTAMP) IS NOT NULL
            GROUP BY 1, 2
            ORDER BY 2, 1
            LIMIT {max_aggregates + 1}
        """
        cursor = connection.execute(query)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                metrics: dict[str, dict[str, float | None]] = {}
                offset = 3
                for name in metric_columns:
                    metrics[name] = {
                        "avg": _number(row[offset]),
                        "max": _number(row[offset + 1]),
                        "stddev": _number(row[offset + 2]),
                        "p95_approx": _number(row[offset + 3]),
                    }
                    offset += 4
                bucket = row[1]
                yield {
                    "group": row[0],
                    "bucket_start": bucket.isoformat() if bucket is not None else None,
                    "row_count": int(row[2]),
                    "metrics": metrics,
                }
    finally:
        connection.close()
