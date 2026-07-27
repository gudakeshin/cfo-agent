"""Synthetic ERP package."""

from cfo_agent.synthetic.adapter import SourceAdapter, SqlAlchemySourceAdapter
from cfo_agent.synthetic.generator import generate_erp, wipe_erp
from cfo_agent.synthetic.scenarios import ScenarioFlags, SCENARIO_IDS, scenario_catalog

__all__ = [
    "generate_erp",
    "wipe_erp",
    "ScenarioFlags",
    "SourceAdapter",
    "SqlAlchemySourceAdapter",
    "SCENARIO_IDS",
    "scenario_catalog",
]
