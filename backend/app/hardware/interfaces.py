"""Extension contracts for later phases; no placeholder claims real analysis."""

from __future__ import annotations

from typing import Any, Protocol


class TopologyRecognizer(Protocol):
    async def identify_topology(self, image: bytes, media_type: str) -> dict[str, Any]:
        """Identify a power topology with supporting evidence and uncertainty."""
        ...


class ComponentParameterChecker(Protocol):
    async def check_parameters(
        self, components: list[dict[str, Any]], operating_conditions: dict[str, Any]
    ) -> dict[str, Any]:
        """Check ratings against operating conditions and derating rules."""
        ...


class DatasheetParser(Protocol):
    async def parse_datasheet(self, pdf: bytes) -> dict[str, Any]:
        """Extract sourced parameters, units and page references from a datasheet."""
        ...


class WaveformProcessor(Protocol):
    async def process_waveform(
        self, samples: list[float], sample_rate_hz: float
    ) -> dict[str, Any]:
        """Process sampled data with units, timing and measurement uncertainty."""
        ...


class EmbeddedSoftwareAgent(Protocol):
    async def analyze_firmware(
        self, source_files: dict[str, str], target: dict[str, Any]
    ) -> dict[str, Any]:
        """Future firmware analysis interface; no first-phase implementation."""
        ...


HARDWARE_CAPABILITIES: list[dict[str, str]] = [
    {"id": "buck_duty", "status": "implemented", "scope": "Ideal CCM buck duty formula"},
    {"id": "bom_csv", "status": "implemented", "scope": "Structural CSV BOM checks"},
    {"id": "power_topology", "status": "planned", "scope": "Interface only"},
    {"id": "component_parameters", "status": "planned", "scope": "Interface only"},
    {"id": "datasheet_parsing", "status": "planned", "scope": "Interface only"},
    {"id": "waveform_processing", "status": "planned", "scope": "Interface only"},
    {"id": "embedded_software", "status": "planned", "scope": "Interface only"},
]
