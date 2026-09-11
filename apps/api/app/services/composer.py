from __future__ import annotations


import polars as pl
from sqlalchemy import create_engine, inspect, text
from simpleeval import simple_eval

from app.services.profiling import profile_dataset

SUPPORTED_DB_KINDS = {"postgresql", "mysql", "sqlite"}


def normalize_db_url(kind: str, url: str) -> str:
    kind = kind.lower().strip()
    if kind not in SUPPORTED_DB_KINDS:
        raise ValueError(f"Unsupported database type: {kind}")
    value = url.strip()
    if kind == "postgresql":
        if value.startswith("postgres://"):
            value = "postgresql+psycopg://" + value[len("postgres://"):]
        elif value.startswith("postgresql://"):
            value = "postgresql+psycopg://" + value[len("postgresql://"):]
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("PostgreSQL URL must start with postgresql://")
    elif kind == "mysql":
        if value.startswith("mysql://"):
            value = "mysql+pymysql://" + value[len("mysql://"):]
        if not value.startswith("mysql+pymysql://"):
            raise ValueError("MySQL URL must start with mysql://")
    elif kind == "sqlite":
        if not value.startswith("sqlite://"):
            value = f"sqlite:///{value}"
    return value


def database_engine(kind: str, url: str):
    return create_engine(normalize_db_url(kind, url), pool_pre_ping=True)


def list_tables(kind: str, url: str) -> list[str]:
    engine = database_engine(kind, url)
    try:
        return inspect(engine).get_table_names()
    finally:
        engine.dispose()


def read_table(kind: str, url: str, table: str, limit: int | None = None) -> pl.DataFrame:
    engine = database_engine(kind, url)
    try:
        tables = set(inspect(engine).get_table_names())
        if table not in tables:
            raise ValueError("Table not found")
        quoted = engine.dialect.identifier_preparer.quote(table)
        sql = f"SELECT * FROM {quoted}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with engine.connect() as conn:
            return pl.read_database(query=text(sql), connection=conn)
    finally:
        engine.dispose()


def merge_frames(frames: list[pl.DataFrame], mode: str, left_on: str | None = None, right_on: str | None = None) -> pl.DataFrame:
    if not frames:
        raise ValueError("Select at least one dataset")
    if mode == "append":
        return pl.concat(frames, how="diagonal_relaxed")
    if mode == "join":
        if len(frames) < 2 or not left_on or not right_on:
            raise ValueError("A join needs at least two datasets and join columns")
        result = frames[0]
        for other in frames[1:]:
            if left_on not in result.columns or right_on not in other.columns:
                raise ValueError("Join column not found in one of the selected datasets")
            result = result.join(other, left_on=left_on, right_on=right_on, how="left", suffix="_right")
        return result
    raise ValueError("Merge mode must be append or join")



def apply_filters(frame: pl.DataFrame, filters: dict[str, object] | None = None) -> pl.DataFrame:
    """Apply exact-match dashboard filters without evaluating user code."""
    if not filters:
        return frame
    result = frame
    for column, value in filters.items():
        if column not in result.columns or value in (None, "", "__all__"):
            continue
        values = value if isinstance(value, list) else [value]
        if not values:
            continue
        result = result.filter(pl.col(column).cast(pl.String).is_in([str(v) for v in values]))
    return result

def evaluate_metric(frame: pl.DataFrame, expression: str) -> float | None:
    """Evaluate safe KPI arithmetic such as SUM("Revenue")/SUM("Cost")."""
    expression = expression.strip()
    if not expression or len(expression) > 1000:
        raise ValueError("Metric expression is required and must be at most 1000 characters")

    def aggregate(function: str, column: object) -> float:
        if not isinstance(column, str) or column not in frame.columns:
            raise ValueError(f"Unknown metric column: {column}")
        series = frame.get_column(column).drop_nulls()
        if function == "COUNT":
            return float(series.len())
        if series.len() == 0:
            return 0.0
        if not series.dtype.is_numeric():
            raise ValueError(f"{function} requires a numeric column")
        return float({"SUM": series.sum(), "AVG": series.mean(), "MIN": series.min(), "MAX": series.max()}[function])

    allowed = {
        "SUM": lambda c: aggregate("SUM", c),
        "AVG": lambda c: aggregate("AVG", c),
        "COUNT": lambda c: aggregate("COUNT", c),
        "MIN": lambda c: aggregate("MIN", c),
        "MAX": lambda c: aggregate("MAX", c),
        "ABS": abs,
        "ROUND": round,
    }
    result = simple_eval(expression, functions=allowed, names={})
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ValueError("Metric expression must return a number")
    value = float(result)
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value

def chart_data(frame: pl.DataFrame, chart_type: str, x: str, y: str | None, aggregation: str = "sum", limit: int = 30, filters: dict[str, object] | None = None) -> dict:
    allowed = {"bar", "line", "area", "pie", "donut", "scatter", "histogram", "boxplot", "pareto", "heatmap", "funnel", "treemap", "waterfall"}
    if chart_type not in allowed:
        raise ValueError(f"Unsupported chart type: {chart_type}")
    if x not in frame.columns:
        raise ValueError(f"Unknown x column: {x}")
    if y and y not in frame.columns:
        raise ValueError(f"Unknown y column: {y}")
    limit = max(1, min(int(limit), 500))
    frame = apply_filters(frame, filters)
    if frame.height == 0:
        return {"chartType": chart_type, "xKey": x, "sourceX": x, "sourceY": y, "aggregation": aggregation, "series": [{"dataKey": "value", "label": y or "Count"}], "data": []}

    if chart_type in {"histogram", "boxplot"}:
        if not frame.schema[x].is_numeric():
            raise ValueError(f"{chart_type.title()} needs a numeric column")
        vals = frame.select(x).drop_nulls().to_series().to_list()
        if not vals:
            return {"chartType": chart_type, "xKey": "bin" if chart_type == "histogram" else "name", "data": []}
        if chart_type == "boxplot":
            q1, median, q3, lo, hi = frame.select(
                pl.col(x).quantile(0.25).alias("q1"),
                pl.col(x).quantile(0.5).alias("median"),
                pl.col(x).quantile(0.75).alias("q3"),
                pl.col(x).min().alias("min"),
                pl.col(x).max().alias("max"),
            ).row(0)
            # ECharts expects [min, Q1, median, Q3, max].
            return {
                "chartType": "boxplot", "xKey": "name", "sourceX": x, "sourceY": None,
                "data": [{"name": x, "value": [safe(lo), safe(q1), safe(median), safe(q3), safe(hi)]}],
            }
        lo, hi = float(min(vals)), float(max(vals))
        bins = min(20, max(5, int(len(vals) ** 0.5)))
        if lo == hi:
            return {"chartType": "histogram", "xKey": "bin", "series": [{"dataKey": "count", "label": "Count"}], "data": [{"bin": safe(lo), "count": len(vals)}]}
        width = (hi - lo) / bins
        counts = [0] * bins
        for value in vals:
            index = min(bins - 1, int((float(value) - lo) / width))
            counts[index] += 1
        return {
            "chartType": "histogram", "xKey": "bin", "series": [{"dataKey": "count", "label": "Count"}],
            "data": [{"bin": round(lo + (i + 0.5) * width, 4), "count": count} for i, count in enumerate(counts)],
        }

    if chart_type == "scatter":
        if not y:
            raise ValueError("Scatter chart needs x and y")
        if not frame.schema[x].is_numeric() or not frame.schema[y].is_numeric():
            raise ValueError("Scatter chart needs two numeric columns")
        data = frame.select([x, y]).drop_nulls().head(limit)
        return {"chartType": "scatter", "xKey": x, "series": [{"dataKey": y, "label": y}], "data": [{x: safe(a), y: safe(b)} for a, b in data.rows()]}

    if chart_type == "heatmap":
        nums = [c for c, dtype in frame.schema.items() if dtype.is_numeric()]
        if len(nums) < 2:
            raise ValueError("Heatmap needs at least two numeric columns")
        cols = nums[:12]
        corr = frame.select(cols).corr()
        data = [{"x": a, "y": b, "value": safe(corr[i, j])} for i, a in enumerate(cols) for j, b in enumerate(cols)]
        return {"chartType": "heatmap", "xKey": "x", "yKey": "y", "valueKey": "value", "sourceX": cols[0], "sourceY": cols[1], "xCategories": cols, "yCategories": cols, "data": data}

    if not y and aggregation != "count" and chart_type not in {"funnel"}:
        raise ValueError(f"{chart_type.title()} needs a value column unless aggregation is count")
    if not y or aggregation == "count":
        agg = frame.select(x).drop_nulls().group_by(x).len().rename({"len": "value"})
    else:
        if not frame.schema[y].is_numeric():
            raise ValueError(f"{aggregation.title()} aggregation requires a numeric value column")
        agg = aggregate(frame, x, y, aggregation)

    # Ranking charts sort by value; time-series charts sort by their axis when temporal/numeric.
    if chart_type in {"bar", "pie", "donut", "pareto", "treemap", "funnel"}:
        agg = agg.sort("value", descending=True).head(limit)
    elif chart_type in {"line", "area"} and (frame.schema[x].is_temporal() or frame.schema[x].is_numeric()):
        agg = agg.sort(x).head(limit)
    else:
        agg = agg.head(limit)

    rows = [{x: safe(a), "value": safe(b)} for a, b in agg.rows()]
    if chart_type in {"pie", "donut", "funnel", "treemap", "pareto"}:
        if any((r["value"] is not None and float(r["value"]) < 0) for r in rows):
            raise ValueError(f"{chart_type.title()} charts require non-negative values")

    if chart_type in {"pie", "donut"}:
        return {"chartType": chart_type, "nameKey": x, "valueKey": "value", "sourceX": x, "sourceY": y, "aggregation": aggregation, "data": rows}
    if chart_type == "treemap":
        return {"chartType": "treemap", "nameKey": x, "valueKey": "value", "sourceX": x, "sourceY": y, "aggregation": aggregation, "data": rows}
    if chart_type == "funnel":
        return {"chartType": "funnel", "nameKey": x, "valueKey": "value", "sourceX": x, "sourceY": y, "aggregation": aggregation, "data": rows}
    if chart_type == "pareto":
        total = sum(float(r["value"] or 0) for r in rows)
        running = 0.0
        for row in rows:
            running += float(row["value"] or 0)
            row["percent"] = round(running / total * 100, 2) if total else 0
        return {"chartType": "pareto", "xKey": x, "sourceX": x, "sourceY": y, "aggregation": aggregation, "series": [{"dataKey": "value", "label": y or "Count"}, {"dataKey": "percent", "label": "Cumulative %"}], "data": rows}
    if chart_type == "waterfall":
        cumulative = 0.0
        out = []
        for row in rows:
            value = float(row["value"] or 0)
            start, end = cumulative, cumulative + value
            out.append({x: row[x], "value": value, "start": min(start, end), "end": end})
            cumulative = end
        return {"chartType": "waterfall", "xKey": x, "sourceX": x, "sourceY": y, "aggregation": aggregation, "series": [{"dataKey": "value", "label": y or "Value"}], "data": out}
    return {"chartType": chart_type, "xKey": x, "sourceX": x, "sourceY": y, "aggregation": aggregation, "series": [{"dataKey": "value", "label": y or "Count"}], "data": rows}

def aggregate(frame: pl.DataFrame, x: str, y: str, aggregation: str) -> pl.DataFrame:
    expr = pl.col(y)
    funcs = {"sum": expr.sum(), "mean": expr.mean(), "count": expr.count(), "min": expr.min(), "max": expr.max()}
    if aggregation not in funcs:
        raise ValueError("Aggregation must be sum, mean, count, min, or max")
    return frame.select([x, y]).drop_nulls().group_by(x).agg(funcs[aggregation].alias("value"))


def safe(v):
    if hasattr(v, "item"):
        try: v = v.item()
        except Exception: pass
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    return v
