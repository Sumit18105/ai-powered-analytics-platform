from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import accuracy_score, mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


def _numeric(df: pl.DataFrame) -> list[str]:
    return [c for c, t in df.schema.items() if t.is_numeric()]


def _categorical(df: pl.DataFrame) -> list[str]:
    return [c for c, t in df.schema.items() if t == pl.String and df.get_column(c).n_unique() <= 100]


def _dates(df: pl.DataFrame) -> list[str]:
    return [c for c, t in df.schema.items() if t in (pl.Date, pl.Datetime)]


def _safe(value: Any):
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def executive_insights(df: pl.DataFrame) -> dict:
    nums = _numeric(df)
    cats = _categorical(df)
    dates = _dates(df)
    insights: list[dict] = []

    for col in nums[:20]:
        s = df.get_column(col).drop_nulls()
        if not len(s):
            continue
        arr = np.asarray(s.to_list(), dtype=float)
        mean = float(arr.mean())
        std = float(arr.std())
        if std > 0:
            z = np.abs((arr - mean) / std)
            count = int((z >= 3).sum())
            if count:
                insights.append({"kind": "anomaly", "severity": "warning", "title": f"Unusual values in {col}", "message": f"{count:,} values are at least 3 standard deviations from the mean.", "details": {"column": col, "count": count}})

    if len(nums) >= 2:
        cols = nums[:20]
        corr = df.select(cols).corr()
        pairs = []
        for i, a in enumerate(cols):
            for j in range(i + 1, len(cols)):
                v = corr[i, j]
                if v is not None and np.isfinite(v):
                    pairs.append((abs(float(v)), float(v), a, cols[j]))
        for _, value, a, b in sorted(pairs, reverse=True)[:3]:
            if abs(value) >= 0.7:
                direction = "positively" if value > 0 else "negatively"
                insights.append({"kind": "relationship", "severity": "info", "title": f"Strong relationship: {a} and {b}", "message": f"{a} and {b} are {direction} correlated ({value:.2f}).", "details": {"x": a, "y": b, "correlation": round(value, 4)}})

    if cats and nums:
        for cat in cats[:5]:
            if df.get_column(cat).n_unique() <= 20:
                value_col = nums[0]
                grouped = df.select([cat, value_col]).drop_nulls().group_by(cat).agg(pl.col(value_col).mean().alias("value")).sort("value", descending=True)
                if grouped.height >= 2:
                    hi = grouped.row(0)
                    lo = grouped.row(grouped.height - 1)
                    insights.append({"kind": "segment", "severity": "info", "title": f"Segment gap in {cat}", "message": f"Average {value_col} is highest for {hi[0]} ({float(hi[1]):,.2f}) and lowest for {lo[0]} ({float(lo[1]):,.2f}).", "details": {"dimension": cat, "metric": value_col, "highest": {"category": str(hi[0]), "value": _safe(hi[1])}, "lowest": {"category": str(lo[0]), "value": _safe(lo[1])}}})
                    break

    if dates and nums:
        date, metric = dates[0], nums[0]
        work = df.select([date, metric]).drop_nulls().sort(date)
        if work.schema[date] == pl.Date:
            work = work.with_columns(pl.col(date).cast(pl.Datetime))
        grouped = work.group_by_dynamic(date, every="1mo", closed="left").agg(pl.col(metric).sum().alias("value")).sort(date)
        if grouped.height >= 2:
            prev, last = float(grouped["value"][-2]), float(grouped["value"][-1])
            change = ((last - prev) / abs(prev) * 100) if prev else None
            if change is not None:
                severity = "warning" if abs(change) >= 20 else "info"
                insights.append({"kind": "trend", "severity": severity, "title": f"Latest {metric} movement", "message": f"{metric} changed {change:+.1f}% versus the previous month.", "details": {"metric": metric, "change_percent": round(change, 2)}})

    insights.sort(key=lambda x: {"critical": 0, "warning": 1, "info": 2}.get(x["severity"], 3))
    return {"summary": f"Generated {len(insights)} evidence-based insights from {df.height:,} rows and {df.width} columns.", "items": insights[:20]}


def deep_analysis(df: pl.DataFrame) -> dict:
    """Run a deterministic multi-step analytical workflow in one pass."""
    insights = executive_insights(df)
    nums = _numeric(df)
    cats = _categorical(df)
    dates = _dates(df)
    sections: list[dict] = []

    if nums:
        metrics = []
        for col in nums[:12]:
            series = df.get_column(col).drop_nulls()
            if len(series):
                metrics.append({
                    "column": col,
                    "sum": _safe(series.sum()),
                    "mean": _safe(series.mean()),
                    "min": _safe(series.min()),
                    "max": _safe(series.max()),
                    "missing": int(df.get_column(col).null_count()),
                })
        sections.append({"step": "metrics", "title": "Key metrics", "items": metrics})

    if cats and nums:
        segments = []
        metric = nums[0]
        for cat in cats[:6]:
            if df.get_column(cat).n_unique() <= 30:
                grouped = (df.select([cat, metric]).drop_nulls()
                           .group_by(cat).agg(pl.col(metric).sum().alias("value"))
                           .sort("value", descending=True).head(10))
                segments.append({"dimension": cat, "metric": metric,
                                 "items": [{"category": str(r[0]), "value": _safe(r[1])} for r in grouped.rows()]})
        if segments:
            sections.append({"step": "segments", "title": "Segment performance", "items": segments})

    if dates and nums:
        date, metric = dates[0], nums[0]
        work = df.select([date, metric]).drop_nulls().sort(date)
        if work.schema[date] == pl.Date:
            work = work.with_columns(pl.col(date).cast(pl.Datetime))
        grouped = work.group_by_dynamic(date, every="1mo", closed="left").agg(pl.col(metric).sum().alias("value")).sort(date)
        trend = [{"date": _safe(r[0]), "value": _safe(r[1])} for r in grouped.tail(24).rows()]
        if trend:
            sections.append({"step": "trend", "title": f"Monthly {metric} trend", "items": trend})

    sections.append({"step": "insights", "title": "Evidence-based findings", "items": insights["items"]})
    return {
        "title": "Deep analysis",
        "summary": f"Completed {len(sections)} analytical steps across {df.height:,} rows and {df.width} columns.",
        "steps": sections,
        "next_actions": [
            "Review high-severity findings first.",
            "Use scenario analysis for material metric changes.",
            "Use AutoML only when a clearly defined prediction target is available.",
        ],
    }


def scenario_analysis(df: pl.DataFrame, column: str, change_percent: float, group_by: str | None = None) -> dict:
    if column not in df.columns or not df.schema[column].is_numeric():
        raise ValueError("Scenario metric must be a numeric column")
    if group_by and group_by not in df.columns:
        raise ValueError("Unknown grouping column")
    if group_by:
        work = df.select([group_by, column]).drop_nulls().group_by(group_by).agg(pl.col(column).sum().alias("baseline"))
        return {"column": column, "change_percent": change_percent, "items": [{"category": str(r[0]), "baseline": _safe(r[1]), "scenario": _safe(float(r[1]) * (1 + change_percent / 100))} for r in work.sort("baseline", descending=True).head(50).rows()]}
    baseline = float(df.get_column(column).drop_nulls().sum())
    return {"column": column, "change_percent": change_percent, "baseline": baseline, "scenario": baseline * (1 + change_percent / 100), "delta": baseline * change_percent / 100}


def automl(df: pl.DataFrame, target: str, task: str = "auto", test_size: float = 0.2) -> dict:
    if target not in df.columns:
        raise ValueError("Unknown target column")
    if task not in {"auto", "regression", "classification"}:
        raise ValueError("Task must be auto, regression, or classification")
    target_dtype = df.schema[target]
    if task == "auto":
        task = "regression" if target_dtype.is_numeric() else "classification"
    features = [c for c in df.columns if c != target and (df.schema[c].is_numeric() or c in _categorical(df))]
    if not features:
        raise ValueError("At least one numeric or low-cardinality categorical feature is required")
    work = df.select(features + [target]).drop_nulls(subset=[target]).to_pandas(use_pyarrow_extension_array=False)
    X = work[features]
    y = work[target]
    if len(work) < 20:
        raise ValueError("At least 20 labeled rows are required for AutoML")
    if task == "classification" and y.nunique() < 2:
        raise ValueError("Classification needs at least two target classes")
    stratify = y if task == "classification" and y.value_counts().min() >= 2 else None
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=test_size, random_state=42, stratify=stratify)
    numeric = [c for c in features if np.issubdtype(X[c].dtype, np.number)]
    categorical = [c for c in features if c not in numeric]
    pre = ColumnTransformer([
        ("num", Pipeline([("imputer", SimpleImputer(strategy="median")), ("scale", StandardScaler())]), numeric),
        ("cat", Pipeline([("imputer", SimpleImputer(strategy="most_frequent")), ("onehot", OneHotEncoder(handle_unknown="ignore"))]), categorical),
    ], remainder="drop")
    models = [
        ("ridge", Ridge()),
        ("random_forest", RandomForestRegressor(n_estimators=150, random_state=42, n_jobs=-1))
    ] if task == "regression" else [
        ("logistic_regression", LogisticRegression(max_iter=1000)),
        ("random_forest", RandomForestClassifier(n_estimators=150, random_state=42, n_jobs=-1))
    ]
    results = []
    fitted = []
    for name, model in models:
        pipe = Pipeline([("preprocess", pre), ("model", model)])
        pipe.fit(X_train, y_train)
        pred = pipe.predict(X_test)
        if task == "regression":
            metrics = {"r2": round(float(r2_score(y_test, pred)), 4), "mae": round(float(mean_absolute_error(y_test, pred)), 4), "rmse": round(float(np.sqrt(mean_squared_error(y_test, pred))), 4)}
            score = metrics["r2"]
        else:
            metrics = {"accuracy": round(float(accuracy_score(y_test, pred)), 4)}
            score = metrics["accuracy"]
        results.append({"model": name, "metrics": metrics, "score": score})
        fitted.append((score, name, pipe))
    fitted.sort(reverse=True, key=lambda x: x[0])
    best_score, best_name, best = fitted[0]
    importance = []
    model = best.named_steps["model"]
    try:
        names = best.named_steps["preprocess"].get_feature_names_out()
        values = model.feature_importances_ if hasattr(model, "feature_importances_") else np.abs(model.coef_[0] if getattr(model, "coef_", None) is not None and np.ndim(model.coef_) > 1 else model.coef_)
        importance = [{"feature": str(n), "importance": round(float(v), 6)} for n, v in sorted(zip(names, values), key=lambda x: abs(float(x[1])), reverse=True)[:15]]
    except Exception:
        pass
    return {"task": task, "target": target, "rows_used": len(work), "features": features, "best_model": best_name, "results": results, "feature_importance": importance}


def executive_report(df: pl.DataFrame, dataset_name: str = "Dataset") -> dict:
    """Build a deterministic executive-ready report from the current dataset."""
    insights = executive_insights(df)
    nums = _numeric(df)
    cats = _categorical(df)
    dates = _dates(df)
    sections = []
    sections.append({"title": "Overview", "items": [
        f"{dataset_name} contains {df.height:,} rows and {df.width} columns.",
        f"{len(nums)} numeric, {len(cats)} low-cardinality categorical, and {len(dates)} date/time columns were detected."
    ]})
    if nums:
        stats = []
        for col in nums[:10]:
            series = df.get_column(col).drop_nulls()
            if len(series):
                stats.append({"metric": col, "sum": _safe(series.sum()), "mean": _safe(series.mean()), "missing": int(df.get_column(col).null_count())})
        sections.append({"title": "Key metrics", "items": stats})
    if insights["items"]:
        sections.append({"title": "Key findings", "items": [{"title": x["title"], "message": x["message"], "severity": x["severity"]} for x in insights["items"]]})
    return {"title": f"Executive report — {dataset_name}", "summary": insights["summary"], "sections": sections, "insights": insights["items"]}


def driver_analysis(df: pl.DataFrame, metric: str | None = None) -> dict:
    """Rank observable dataset dimensions that explain variation in a numeric metric."""
    nums = _numeric(df)
    cats = _categorical(df)
    if not nums:
        raise ValueError("Driver analysis requires at least one numeric column")
    metric = metric or nums[0]
    if metric not in nums:
        raise ValueError("Driver metric must be numeric")

    drivers: list[dict] = []
    for col in nums:
        if col == metric:
            continue
        work = df.select([metric, col]).drop_nulls()
        if work.height < 3:
            continue
        value = work.select(pl.corr(metric, col)).item()
        if value is not None and np.isfinite(float(value)):
            drivers.append({"type": "numeric", "dimension": col, "score": round(abs(float(value)), 4), "correlation": round(float(value), 4)})

    for col in cats[:12]:
        if df.get_column(col).n_unique() > 30:
            continue
        work = df.select([col, metric]).drop_nulls().group_by(col).agg(pl.col(metric).mean().alias("value"))
        if work.height < 2:
            continue
        vals = [float(v) for v in work["value"].to_list() if v is not None and np.isfinite(float(v))]
        if len(vals) < 2:
            continue
        mean = float(np.mean(vals))
        spread = float((max(vals) - min(vals)) / abs(mean)) if mean else float(max(vals) - min(vals))
        drivers.append({"type": "categorical", "dimension": col, "score": round(abs(spread), 4), "relative_spread": round(spread, 4)})

    drivers.sort(key=lambda x: x["score"], reverse=True)
    return {
        "metric": metric,
        "summary": f"Ranked {len(drivers)} observable drivers for {metric}.",
        "drivers": drivers[:12],
        "caveat": "Driver scores describe association or segment spread; they do not establish causation.",
    }
