"""skyline_pano — automated perspective panorama pipeline for facade sweeps."""
__version__ = "1.0.0"

from .config import Config, Line, load  # noqa: F401
from .pipeline import Pipeline  # noqa: F401
from .remote import Pool  # noqa: F401

__all__ = ["Config", "Line", "load", "Pipeline", "Pool", "__version__"]
