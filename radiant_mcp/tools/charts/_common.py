"""What the chart tools share."""

from fastmcp.utilities.types import Image

DAYS_PER_YEAR = 365.25


def image(text: str, png: bytes) -> list:
    """A chart tool result: FastMCP sends a text part with the values drawn, then the PNG."""
    return [text, Image(data=png, format="png")]
