from pathlib import Path
from typing import Any

import polars as pl

from app.services.profiling import _json_value, read_dataset


def _safe(v: Any) -> Any:
    return _json_value(v)


def _numeric_summary(df: pl.DataFrame, columns: list[str]) -> list[dict]:
    out = []
    for name in columns[:20]:
        s = df.get_column(name).drop_nulls()
        if not len(s):
            continue
        out.append({"column": name, "count": len(s), "min": _safe(s.min()), "max": _safe(s.max()), "mean": _safe(s.mean()), "median": _safe(s.median()), "std": _safe(s.std())})
    return out

def _categorical(df: pl.DataFrame, columns: list[str]) -> list[dict]:
    out = []
    for name in columns[:10]:
        counts = df.get_column(name).drop_nulls().value_counts(sort=True).head(20)
        out.append({"column": name, "items": [{"value": _safe(r[0]), "count": int(r[1])} for r in counts.rows()]})
    return out

def _correlations(df: pl.DataFrame, columns: list[str]) -> list[dict]:
    cols = columns[:20]
    if len(cols) < 2:
        return []
    values = df.select(cols).corr()
    result = []
    for i, a in enumerate(cols):
        for j in range(i + 1, len(cols)):
            result.append({"x": a, "y": cols[j], "correlation": _safe(values[i, j])})
    return sorted(result, key=lambda x: abs(x["correlation"] or 0), reverse=True)[:30]

def _time_series(df: pl.DataFrame, date_columns: list[str], numeric_columns: list[str]) -> dict | None:
    if not date_columns or not numeric_columns:
        return None
    date_col, value_col = date_columns[0], numeric_columns[0]
    work = df.select([date_col, value_col]).drop_nulls()
    if not len(work):
        return None
    dtype = work.schema[date_col]
    if dtype == pl.Date:
        work = work.with_columns(pl.col(date_col).cast(pl.Datetime))
    work = work.sort(date_col)
    grouped = (work.group_by_dynamic(date_col, every="1mo", closed="left")
        .agg(pl.col(value_col).sum().alias("value"))
        .sort(date_col)
        .tail(120))
    return {"date_column": date_col, "value_column": value_col, "items": [{"date": _safe(r[0]), "value": _safe(r[1])} for r in grouped.rows()]}

def build_eda(path: Path) -> dict:
    df = read_dataset(path)
    profile = {"rows": df.height, "columns": df.width}
    numeric = [c for c, t in zip(df.columns, df.dtypes) if t.is_numeric()]
    categorical = [c for c, t in zip(df.columns, df.dtypes) if t == pl.String and df.get_column(c).n_unique() <= 50]
    dates = [c for c, t in zip(df.columns, df.dtypes) if t in (pl.Date, pl.Datetime)]
    missing = [{"column": c, "count": int(df.get_column(c).null_count())} for c in df.columns if df.get_column(c).null_count()]
    return {
        "profile": profile,
        "numeric_summary": _numeric_summary(df, numeric),
        "categorical_counts": _categorical(df, categorical),
        "correlations": _correlations(df, numeric),
        "time_series": _time_series(df, dates, numeric),
        "missing": missing,
    }

def recommend_charts(path: Path) -> list[dict]:
    """Recommend varied visuals from detected semantic column types.

    The recommender deliberately returns different visual families for different
    data shapes instead of using the same four charts for every dataset.
    """
    df = read_dataset(path)
    numeric = [c for c, t in zip(df.columns, df.dtypes) if t.is_numeric()]
    categorical = [c for c, t in zip(df.columns, df.dtypes)
                   if t == pl.String and 1 < df.get_column(c).n_unique() <= 50]
    dates = [c for c, t in zip(df.columns, df.dtypes) if t in (pl.Date, pl.Datetime)]
    recs: list[dict] = []

    if dates and numeric:
        d, n = dates[0], numeric[0]
        recs += [
            {"type": "line", "title": f"{n} trend over time", "x": d, "y": n, "reason": "time series"},
            {"type": "area", "title": f"{n} volume over time", "x": d, "y": n, "reason": "time series"},
        ]

    if categorical and numeric:
        c, n = categorical[0], numeric[0]
        recs += [
            {"type": "bar", "title": f"{n} by {c}", "x": c, "y": n, "reason": "category comparison"},
            {"type": "pareto", "title": f"{n} Pareto by {c}", "x": c, "y": n, "reason": "ranked contribution"},
            {"type": "donut", "title": f"{n} share by {c}", "x": c, "y": n, "reason": "part to whole"},
        ]

    for n in numeric[:4]:
        recs.append({"type": "histogram", "title": f"Distribution of {n}", "x": n, "reason": "distribution"})
        recs.append({"type": "boxplot", "title": f"Box plot of {n}", "x": n, "reason": "spread and outliers"})

    if len(numeric) >= 2:
        recs += [
            {"type": "scatter", "title": f"{numeric[0]} vs {numeric[1]}", "x": numeric[0], "y": numeric[1], "reason": "numeric relationship"},
            {"type": "heatmap", "title": "Numeric correlation heatmap", "x": numeric[0], "y": numeric[1], "reason": "correlation"},
        ]

    if not recs and df.width:
        recs.append({"type": "bar", "title": "Record count by first column", "x": df.columns[0], "reason": "fallback"})
    # de-duplicate by (type,x,y) while retaining the strongest ordering above.
    seen=set(); out=[]
    for r in recs:
        key=(r["type"],r.get("x"),r.get("y"))
        if key not in seen:
            seen.add(key); out.append(r)
    return out[:16]
