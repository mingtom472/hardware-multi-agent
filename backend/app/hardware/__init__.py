"""Deterministic tools and extension contracts for hardware engineering."""

from .interfaces import (
    HARDWARE_CAPABILITIES,
    ComponentParameterChecker,
    DatasheetParser,
    EmbeddedSoftwareAgent,
    TopologyRecognizer,
    WaveformProcessor,
)
from .tools import analyze_bom_csv, calculate_buck_duty

__all__ = [
    "HARDWARE_CAPABILITIES",
    "ComponentParameterChecker",
    "DatasheetParser",
    "EmbeddedSoftwareAgent",
    "TopologyRecognizer",
    "WaveformProcessor",
    "analyze_bom_csv",
    "calculate_buck_duty",
]
