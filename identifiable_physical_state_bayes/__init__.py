"""Identifiable physical state-space Bayes probability kernel.

The package is deliberately pure: no database, network, market or wall-clock
dependency is allowed below this boundary.
"""

from .contracts import *  # noqa: F401,F403
from .revision import replace_future_forcing_generation
