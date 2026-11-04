"""The OddsIQ pipeline: orchestrator + focused services (decomposed scheduler).

See docs/Components/Pipeline/Pipeline Services.md.
"""

from oddsiq.pipeline.scheduler import Scheduler

__all__ = ["Scheduler"]
