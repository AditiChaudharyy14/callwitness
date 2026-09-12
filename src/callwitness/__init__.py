"""Callwitness — a control layer between AI agents and the systems they can reach.

Stage one observes and blocks nothing. You cannot write good policy against
failures nobody has measured, and nobody has measured these yet.
"""

# Read from the installed package metadata rather than written here, because a
# hand-maintained copy drifts: this said 0.1.0 for the whole of the 0.1.1
# release, so `--version` lied and every contributed record would have been
# filed under a version that was not running. For a tool whose product is
# provenance, that is the worst field to get wrong. pyproject.toml is now the
# only place the number exists.
try:
    from importlib.metadata import PackageNotFoundError, version as _installed

    try:
        __version__ = _installed("callwitness")
    except PackageNotFoundError:      # running from a checkout, not installed
        __version__ = "0.0.0+source"
except ImportError:                   # pragma: no cover -- Python < 3.8
    __version__ = "0.0.0+source"

from .analyze import extract_entities, extract_signals, shape_only
from .proxy import Proxy
from .record import Recorder

__all__ = ["Proxy", "Recorder", "extract_entities", "extract_signals",
           "shape_only", "__version__"]
