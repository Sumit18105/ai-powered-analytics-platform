from pathlib import Path
from typing import Any

import polars as pl

SUPPORTED = {".csv", ".parquet", ".xlsx", ".xls"}


def read_dataset(path: Path, sheet_name: str | None = None) -> pl.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pl.read_csv(path, try_parse_dates=True, infer_schema_length=2000)
    if suffix == ".parquet":
        return pl.read_parquet(path)
    if suffix in {".xlsx", ".xls"}:
        return pl.read_excel(path, sheet_name=sheet_name, engine="calamine")
    raise ValueError(f"Unsupported file type: {suffix}")


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def _column_kind(dtype: pl.DataType, series: pl.Series) -> str:
    if dtype.is_numeric():
        return "numeric"
    if dtype == pl.Boolean:
        return "boolean"
    if dtype in (pl.Date, pl.Datetime, pl.Time):
        return "datetime"
    if dtype == pl.String:
        unique = series.n_unique()
        non_null = len(series)
        if non_null and unique == non_null:
            return "id-like"
        if non_null and unique <= min(50, max(10, int(non_null * 0.05))):
            return "categorical"
        return "text"
    return "other"


def profile_frame(df: pl.DataFrame) -> dict:
    row_count = df.height
    columns = []
    for name, dtype in zip(df.columns, df.dtypes):
        series = df.get_column(name)
        null_count = series.null_count()
        non_null = series.drop_nulls()
        unique_count = non_null.n_unique()
        kind = _column_kind(dtype, non_null)
        detail: dict[str, Any] = {
            "name": name,
            "dtype": str(dtype),
            "kind": kind,
            "null_count": null_count,
            "null_percent": round(null_count / max(row_count, 1) * 100, 2),
            "unique_count": unique_count,
            "unique_percent": round(unique_count / max(row_count, 1) * 100, 2),
            "sample_values": [_json_value(v) for v in non_null.head(5).to_list()],
        }
        if dtype.is_numeric() and len(non_null):
            detail["statistics"] = {
                "min": _json_value(non_null.min()),
                "max": _json_value(non_null.max()),
                "mean": _json_value(non_null.mean()),
                "median": _json_value(non_null.median()),
                "std": _json_value(non_null.std()),
            }
        elif dtype in (pl.Date, pl.Datetime) and len(non_null):
            detail["statistics"] = {"min": _json_value(non_null.min()), "max": _json_value(non_null.max())}
        elif kind in {"categorical", "text"} and len(non_null):
            detail["top_values"] = [
                {"value": _json_value(row[0]), "count": int(row[1])}
                for row in non_null.value_counts(sort=True).head(5).rows()
            ]
        columns.append(detail)

    duplicate_rows = int(df.is_duplicated().sum()) if row_count else 0
    missing_cells = sum(item["null_count"] for item in columns)
    total_cells = row_count * df.width
    quality_score = 100.0 if total_cells == 0 else round(
        max(0.0, 100 - missing_cells / total_cells * 70 - duplicate_rows / max(row_count, 1) * 30), 2
    )
    return {
        "rows": row_count,
        "columns": df.width,
        "duplicate_rows": duplicate_rows,
        "missing_cells": missing_cells,
        "missing_percent": round(missing_cells / max(total_cells, 1) * 100, 2),
        "quality_score": quality_score,
        "column_details": columns,
        "numeric_columns": [item["name"] for item in columns if item["kind"] == "numeric"],
        "categorical_columns": [item["name"] for item in columns if item["kind"] == "categorical"],
        "datetime_columns": [item["name"] for item in columns if item["kind"] == "datetime"],
        "id_like_columns": [item["name"] for item in columns if item["kind"] == "id-like"],
    }


def profile_dataset(path: Path) -> dict:
    return profile_frame(read_dataset(path))


def preview_dataset(path: Path, limit: int = 25) -> dict:
    limit = max(1, min(limit, 500))
    df = read_dataset(path).head(limit)
    rows = [{column: _json_value(value) for column, value in zip(df.columns, row)} for row in df.rows()]
    return {"columns": df.columns, "rows": rows, "limit": limit}


def list_excel_sheets(path: Path) -> list[str]:
    if path.suffix.lower() not in {".xlsx", ".xls"}:
        return []
    sheets = pl.read_excel(path, sheet_id=0, engine="calamine")
    return list(sheets.keys()) if isinstance(sheets, dict) else []
