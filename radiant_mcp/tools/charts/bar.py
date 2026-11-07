"""``bar_chart``: a bar chart of numbers the agent already computed."""

import json

import anyio
from typing import Any

from ... import charts
from ... import sql as _sql
from ._common import image as _image


MAX_BARS = 50


async def bar_chart(
    title: str,
    categories: list[str],
    values: list[float],
    x_label: str = "",
    y_label: str = "",
    horizontal: bool = False,
) -> Any:
    """Bar chart of a distribution you already computed with another tool
    (e.g. carriers per consequence, patients per diagnosis). Counts or
    proportions only: never patient names or identifiers as categories.

    Args:
        title: Chart title.
        categories: Bar labels (1 to 50).
        values: One number per category.
        x_label, y_label: Axis labels.
        horizontal: Horizontal bars, better for long labels.
    """
    if not categories or len(categories) > MAX_BARS:
        return {"error": f"give 1 to {MAX_BARS} categories"}
    if len(values) != len(categories):
        return {"error": "`values` must have one number per category"}
    try:
        numbers = [_sql.number(v) for v in values]
    except _sql.InvalidInput as exc:
        return {"error": str(exc)}
    labels = [str(c)[:60] for c in categories]
    duplicates = sorted({c for c in labels if labels.count(c) > 1})
    if duplicates:
        # The axis would stack them into one bar, and the text would keep the last value.
        return {"error": f"categories must be distinct (in their first 60 characters): {duplicates[:5]}"}
    # Rendering takes the global render lock: keep it off the event loop.
    png = await anyio.to_thread.run_sync(lambda: charts.bar_chart(
        labels, numbers, title[:120], x_label[:60], y_label[:60], bool(horizontal)))
    return _image(json.dumps({"title": title, "values": dict(zip(labels, numbers, strict=True))}), png)
