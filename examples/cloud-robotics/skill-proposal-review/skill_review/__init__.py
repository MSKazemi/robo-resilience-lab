"""Simulated skill-proposal review (issues #64 and #86). SYNTHETIC, non-actuating, stdlib only."""

from .catalogue import CATALOGUE, PROPOSAL_VERSION  # noqa: F401
from .reviewer import ACCEPT, NEEDS_HUMAN, REJECT, review  # noqa: F401
