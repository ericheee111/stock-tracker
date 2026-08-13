"""Research-grade quantitative contracts for :mod:`stock_tracker`.

The package is deliberately separated from the live Provider/API path. It
contains point-in-time data contracts, execution-aware labels, backtesting and
model-governance primitives. Importing it never starts data collection,
touches the production database, or enables automatic trading.
"""

from .core.fingerprint import canonical_json, fingerprint
from .core.point_in_time import PointInTimeStore

__all__ = ["PointInTimeStore", "canonical_json", "fingerprint"]
