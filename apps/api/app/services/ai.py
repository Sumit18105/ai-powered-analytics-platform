from __future__ import annotations

import json
import os
from typing import Literal

import numpy as np
import polars as pl
from pydantic import BaseModel, Field



Operation = Literal["summary", "trend", "group", "correlation", "top", "forecast", "anomalies", "regression"]


class AnalysisPlan(BaseModel):
    operation: Operation
    column: str | None = None
    group_by: str | None = None
    target: str | None = None
    feature: str | None = None
    periods: int = Field(default=6, ge=1, le=24)
    limit: int = Field(default=10, ge=1, le=50)


def _schema(df: pl.DataFrame) -> dict:
    return {
        "columns": [
            {"name": c, "dtype": str(df.schema[c]), "sample": df.get_column(c).head(3).to_list()}
            for c in df.columns
        ]
    }


def _numeric(df: pl.DataFrame) -> list[str]:
    return [c for c, t in df.schema.items() if t.is_numeric()]


def _dates(df: pl.DataFrame) -> list[str]:
    return [c for c, t in df.schema.items() if t in (pl.Date, pl.Datetime)]


def _categorical(df: pl.DataFrame) -> list[str]:
    return [c for c, t in df.schema.items() if t == pl.String and df.get_column(c).n_unique() <= 100]


def _pick(prompt: str, names: list[str]) -> str | None:
    low = prompt.lower()
    for name in sorted(names, key=len, reverse=True):
        if name.lower() in low:
            return name
    return None


def fallback_plan(prompt: str, df: pl.DataFrame) -> AnalysisPlan:
    low = prompt.lower()
    nums, dates, cats = _numeric(df), _dates(df), _categorical(df)
    if any(x in low for x in ("forecast", "predict", "next month", "future")):
        return AnalysisPlan(operation="forecast", column=_pick(prompt, nums) or (nums[0] if nums else None), periods=6)
    if any(x in low for x in ("anomal", "outlier", "unusual")):
        return AnalysisPlan(operation="anomalies", column=_pick(prompt, nums) or (nums[0] if nums else None))
    if any(x in low for x in ("correlation", "relationship", "related")):
        return AnalysisPlan(operation="correlation")
    if any(x in low for x in ("regression", "effect of", "predict from", "predict ")):
        return AnalysisPlan(operation="regression", target=_pick(prompt, nums) or (nums[0] if nums else None), feature=_pick(prompt, nums[1:]) or (nums[1] if len(nums) > 1 else None))
    if any(x in low for x in ("top", "highest", "best", "largest")):
        return AnalysisPlan(operation="top", column=_pick(prompt, nums) or (nums[0] if nums else None), group_by=_pick(prompt, cats) or (cats[0] if cats else None))
    if any(x in low for x in ("by ", "group", "breakdown", "per ")) and cats and nums:
        return AnalysisPlan(operation="group", column=_pick(prompt, nums) or nums[0], group_by=_pick(prompt, cats) or cats[0])
    if any(x in low for x in ("trend", "over time", "monthly", "daily", "weekly")) and dates and nums:
        return AnalysisPlan(operation="trend", column=_pick(prompt, nums) or nums[0])
    return AnalysisPlan(operation="summary")


def llm_plan(prompt: str, df: pl.DataFrame) -> AnalysisPlan | None:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key)
        response = client.responses.parse(
            model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
            instructions=(
                "You are the planning layer of a data analytics application. "
                "Return only an AnalysisPlan. Choose one safe operation. "
                "Never invent column names; use only the provided schema. "
                "The application, not the model, performs calculations."
            ),
            input=json.dumps({"user_prompt": prompt, "schema": _schema(df)}, default=str),
            text_format=AnalysisPlan,
        )
        return response.output_parsed
    except Exception:
        return None


def make_plan(prompt: str, df: pl.DataFrame) -> AnalysisPlan:
    return llm_plan(prompt, df) or fallback_plan(prompt, df)


def validate_plan(plan: AnalysisPlan, df: pl.DataFrame) -> AnalysisPlan:
    numeric, dates, cats = _numeric(df), _dates(df), _categorical(df)
    for field in ("column", "target", "feature", "group_by"):
        value = getattr(plan, field)
        if value is not None and value not in df.columns:
            setattr(plan, field, None)
    if plan.operation in {"trend", "forecast"}:
        plan.column = plan.column or (numeric[0] if numeric else None)
        if not dates or not plan.column:
            plan.operation = "summary"
    if plan.operation in {"group", "top"}:
        plan.column = plan.column or (numeric[0] if numeric else None)
        plan.group_by = plan.group_by or (cats[0] if cats else None)
        if not plan.column or not plan.group_by:
            plan.operation = "summary"
    if plan.operation in {"anomalies"}:
        plan.column = plan.column or (numeric[0] if numeric else None)
        if not plan.column:
            plan.operation = "summary"
    if plan.operation == "regression":
        plan.target = plan.target or (numeric[0] if numeric else None)
        plan.feature = plan.feature or (numeric[1] if len(numeric) > 1 else None)
        if not plan.target or not plan.feature or plan.target == plan.feature:
            plan.operation = "summary"
    return plan


def execute_plan(df: pl.DataFrame, plan: AnalysisPlan) -> dict:
    plan = validate_plan(plan, df)
    nums = _numeric(df)
    if plan.operation == "summary":
        return {"type": "summary", "title": "Dataset summary", "data": {"rows": df.height, "columns": df.width, "numeric_columns": nums}}
    if plan.operation == "group":
        result = df.select([plan.group_by, plan.column]).drop_nulls().group_by(plan.group_by).agg(pl.col(plan.column).sum().alias("value")).sort("value", descending=True).head(plan.limit)
        return {"type": "bar", "title": f"{plan.column} by {plan.group_by}", "x": plan.group_by, "y": plan.column, "items": [{"category": str(r[0]), "value": float(r[1])} for r in result.rows()]}
    if plan.operation == "top":
        result = df.select([plan.group_by, plan.column]).drop_nulls().group_by(plan.group_by).agg(pl.col(plan.column).sum().alias("value")).sort("value", descending=True).head(plan.limit)
        return {"type": "bar", "title": f"Top {plan.group_by} by {plan.column}", "items": [{"category": str(r[0]), "value": float(r[1])} for r in result.rows()]}
    if plan.operation == "correlation":
        cols = nums[:20]
        if len(cols) < 2:
            return {"type": "text", "title": "Correlation", "message": "At least two numeric columns are required."}
        corr = df.select(cols).corr()
        pairs = []
        for i, a in enumerate(cols):
            for j in range(i + 1, len(cols)):
                value = corr[i, j]
                if value is not None and np.isfinite(value):
                    pairs.append({"x": a, "y": cols[j], "correlation": float(value)})
        pairs.sort(key=lambda x: abs(x["correlation"]), reverse=True)
        return {"type": "correlation", "title": "Strongest correlations", "items": pairs[:20]}
    if plan.operation == "trend":
        date = _dates(df)[0]
        work = df.select([date, plan.column]).drop_nulls().sort(date)
        if work.schema[date] == pl.Date:
            work = work.with_columns(pl.col(date).cast(pl.Datetime))
        grouped = work.group_by_dynamic(date, every="1mo", closed="left").agg(pl.col(plan.column).sum().alias("value")).sort(date).tail(120)
        return {"type": "line", "title": f"{plan.column} over time", "items": [{"date": str(r[0]), "value": float(r[1])} for r in grouped.rows()]}
    if plan.operation == "forecast":
        date = _dates(df)[0]
        work = df.select([date, plan.column]).drop_nulls().sort(date)
        if work.schema[date] == pl.Date:
            work = work.with_columns(pl.col(date).cast(pl.Datetime))
        grouped = work.group_by_dynamic(date, every="1mo", closed="left").agg(pl.col(plan.column).sum().alias("value")).sort(date)
        y = np.array(grouped.get_column("value").to_list(), dtype=float)
        if len(y) < 3:
            return {"type": "text", "title": "Forecast", "message": "At least three time points are required."}
        x = np.arange(len(y), dtype=float)
        slope, intercept = np.polyfit(x, y, 1)
        future_x = np.arange(len(y), len(y) + plan.periods, dtype=float)
        last = grouped.get_column(date)[-1]
        def add_months(value, months: int):
            from calendar import monthrange
            from datetime import date, datetime
            year, month = value.year + (value.month - 1 + months) // 12, (value.month - 1 + months) % 12 + 1
            day = min(value.day, monthrange(year, month)[1])
            if isinstance(value, datetime):
                return value.replace(year=year, month=month, day=day)
            return date(year, month, day)
        dates_out = [add_months(last, i) for i in range(1, plan.periods + 1)]
        return {"type": "forecast", "title": f"{plan.column} forecast", "history": [{"date": str(d), "value": float(v)} for d, v in zip(grouped.get_column(date), y)], "forecast": [{"date": str(d), "value": max(0.0, float(intercept + slope * xx))} for d, xx in zip(dates_out, future_x)]}
    if plan.operation == "anomalies":
        values = df.select([plan.column]).with_row_index("row_index").drop_nulls(subset=[plan.column])
        arr = np.asarray(values.get_column(plan.column).to_list(), dtype=float)
        if not len(arr):
            return {"type": "anomalies", "title": f"Anomalies in {plan.column}", "items": []}
        mean, std = float(arr.mean()), float(arr.std())
        if std == 0:
            return {"type": "anomalies", "title": f"Anomalies in {plan.column}", "items": []}
        z = np.abs((arr - mean) / std)
        items = [
            {"index": int(row_index), "value": float(value), "z_score": float(score)}
            for row_index, value, score in zip(values.get_column("row_index"), arr, z)
            if score >= 3
        ]
        return {"type": "anomalies", "title": f"Anomalies in {plan.column}", "items": items[:100]}
    if plan.operation == "regression":
        from sklearn.linear_model import LinearRegression
        work = df.select([plan.feature, plan.target]).drop_nulls()
        if work.height < 3:
            return {"type": "text", "title": "Regression", "message": "At least three complete observations are required."}
        X = np.array(work.get_column(plan.feature).to_list(), dtype=float).reshape(-1, 1)
        y = np.array(work.get_column(plan.target).to_list(), dtype=float)
        model = LinearRegression().fit(X, y)
        return {"type": "regression", "title": f"{plan.target} vs {plan.feature}", "feature": plan.feature, "target": plan.target, "coefficient": float(model.coef_[0]), "intercept": float(model.intercept_), "r2": float(model.score(X, y)), "observations": int(len(y))}
    return {"type": "text", "title": "Analysis", "message": "No supported analysis was selected."}
