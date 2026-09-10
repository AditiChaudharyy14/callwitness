"""Callwitness — a control layer between AI agents and the systems they can reach.

Stage one observes and blocks nothing. You cannot write good policy against
failures nobody has measured, and nobody has measured these yet.
"""

__version__ = "0.1.0"

from .analyze import extract_entities, extract_signals, shape_only
from .proxy import Proxy
from .record import Recorder

__all__ = ["Proxy", "Recorder", "extract_entities", "extract_signals",
           "shape_only", "__version__"]
