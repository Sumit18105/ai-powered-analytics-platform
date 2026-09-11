from pathlib import Path
from typing import Any

import polars as pl

from app.services.profiling import profile_dataset, profile_frame, read_dataset


SUPPORTED_MISSING = {"drop", "mean", "median", "mode", "forward_fill", "backward_fill", "interpolate", "custom"}
SUPPORTED_OUTLIERS = {"iqr_drop", "iqr_winsorize"}


def _numeric_expr(column: str, operation: str) -> pl.Expr:
    values = pl.col(column).drop_nulls()
    q1 = values.quantile(0.25)
    q3 = values.quantile(0.75)
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    if operation == "iqr_drop":
        return ((pl.col(column) >= lower) & (pl.col(column) <= upper)) | pl.col(column).is_null()
    return pl.col(column).clip(lower, upper)


def apply_operations(df: pl.DataFrame, operations: list[dict[str, Any]]) -> pl.DataFrame:
    result = df
    for op in operations:
        kind = op.get("type")
        column = op.get("column")

        if kind == "drop_duplicates":
            result = result.unique(maintain_order=True)
            continue

        if kind == "missing":
            if not column or column not in result.columns:
                raise ValueError("A valid column is required for a missing-value operation")
            method = op.get("method")
            if method not in SUPPORTED_MISSING:
                raise ValueError(f"Unsupported missing-value method: {method}")
            if method == "drop":
                result = result.filter(pl.col(column).is_not_null())
            elif method == "mean":
                result = result.with_columns(pl.col(column).fill_null(pl.col(column).mean()))
            elif method == "median":
                result = result.with_columns(pl.col(column).fill_null(pl.col(column).median()))
            elif method == "mode":
                modes = result.get_column(column).drop_nulls().mode()
                if len(modes):
                    result = result.with_columns(pl.col(column).fill_null(modes[0]))
            elif method == "forward_fill":
                result = result.with_columns(pl.col(column).forward_fill())
            elif method == "backward_fill":
                result = result.with_columns(pl.col(column).backward_fill())
            elif method == "interpolate":
                result = result.with_columns(pl.col(column).interpolate())
            elif method == "custom":
                if "value" not in op:
                    raise ValueError("custom missing-value handling requires a value")
                result = result.with_columns(pl.col(column).fill_null(op["value"]))
            continue

        if kind == "outlier":
            if not column or column not in result.columns:
                raise ValueError("A valid numeric column is required for an outlier operation")
            if not result.get_column(column).dtype.is_numeric():
                raise ValueError(f"Outlier handling requires a numeric column: {column}")
            method = op.get("method")
            if method not in SUPPORTED_OUTLIERS:
                raise ValueError(f"Unsupported outlier method: {method}")
            if method == "iqr_drop":
                result = result.filter(_numeric_expr(column, method))
            else:
                result = result.with_columns(_numeric_expr(column, method).alias(column))
            continue

        if kind == "cast":
            if not column or column not in result.columns:
                raise ValueError("A valid column is required for type conversion")
            target = op.get("to")
            mapping = {"string": pl.String, "integer": pl.Int64, "float": pl.Float64, "boolean": pl.Boolean, "date": pl.Date, "datetime": pl.Datetime}
            if target not in mapping:
                raise ValueError(f"Unsupported target type: {target}")
            result = result.with_columns(pl.col(column).cast(mapping[target], strict=False))
            continue

        raise ValueError(f"Unsupported transformation: {kind}")

    return result


def suggest_operations(profile: dict) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    if profile.get("duplicate_rows", 0):
        suggestions.append({"type": "drop_duplicates", "reason": f"{profile['duplicate_rows']:,} duplicate rows detected"})
    for column in profile.get("column_details", []):
        if column.get("null_count", 0):
            method = "median" if column.get("kind") == "numeric" else "mode"
            suggestions.append({"type": "missing", "column": column["name"], "method": method, "reason": f"{column['null_percent']}% missing values"})
    return suggestions


def preview_operations(path: Path, operations: list[dict[str, Any]]) -> dict:
    original = read_dataset(path)
    transformed = apply_operations(original, operations)
    original_nulls = sum(s.null_count() for s in original)
    transformed_nulls = sum(s.null_count() for s in transformed)
    return {
        "before": {"rows": original.height, "columns": original.width, "missing_cells": original_nulls},
        "after": {"rows": transformed.height, "columns": transformed.width, "missing_cells": transformed_nulls},
        "rows_removed": original.height - transformed.height,
        "missing_cells_removed": original_nulls - transformed_nulls,
        "profile": profile_dataset_from_df(transformed),
    }



# Reuse the canonical profiler for previews so preview and persisted profiles stay identical.
profile_dataset_from_df = profile_frame
