"""pc2sql — PowerCenter repository export -> SQL Server instruction generator.

Pipeline:  parser (XML -> IR)  ->  IR (object-per-object graph)  ->  emitter (IR -> T-SQL).
"""

from .parser import parse
from .emitter import emit
from . import ir, expressions

__all__ = ["parse", "emit", "ir", "expressions"]


def convert(xml_path: str) -> str:
    """Parse a PowerCenter export and return the generated SQL Server script."""
    return emit(parse(xml_path))
