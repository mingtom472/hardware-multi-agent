"""Small deterministic calculations; these do not constitute design approval."""

from __future__ import annotations

import csv
import io
import re
from decimal import (
    Clamped,
    Decimal,
    DecimalException,
    InvalidOperation,
    Subnormal,
    Underflow,
    localcontext,
)
from typing import Any


def _positive_decimal(value: str | int | float | Decimal, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite positive number") from None
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def calculate_buck_duty(
    vin: str | int | float | Decimal, vout: str | int | float | Decimal
) -> dict[str, Any]:
    """Return D=Vout/Vin for an ideal buck converter in continuous conduction.

    Values are volts and must satisfy ``vin > vout > 0``. Decimal strings in
    the result avoid silently rounding intermediate values to binary floats.
    Ratios outside normal Decimal range, or rounded to an endpoint at the
    supported 28-digit precision, are rejected rather than returned as zero/one.
    Losses, transient response, magnetics and controller limits are excluded.
    """

    input_voltage = _positive_decimal(vin, "vin")
    output_voltage = _positive_decimal(vout, "vout")
    if output_voltage >= input_voltage:
        raise ValueError("An ideal buck converter requires vin > vout > 0")
    try:
        with localcontext() as context:
            context.prec = 28
            context.Emin = -999999
            context.Emax = 999999
            for signal in (Underflow, Subnormal, Clamped):
                context.traps[signal] = True
            duty = output_voltage / input_voltage
            percent = duty * 100
    except DecimalException:
        raise ValueError("Voltage ratio exceeds the supported Decimal arithmetic range") from None
    if not 0 < duty < 1:
        raise ValueError("Voltage ratio cannot be represented safely at 28-digit precision")
    return {
        "vin_v": str(input_voltage),
        "vout_v": str(output_voltage),
        "duty_cycle": str(duty),
        "duty_percent": str(percent),
        "formula": "D = Vout / Vin",
        "assumptions": [
            "Ideal buck converter in continuous conduction mode",
            "Semiconductor losses and controller limits are excluded",
        ],
    }


_COLUMN_ALIASES = {"ref": "reference", "designator": "reference", "qty": "quantity"}
_REQUIRED_COLUMNS = ("reference", "value", "footprint", "quantity")
MAX_BOM_ROWS = 10_000
MAX_BOM_CHARACTERS = 2_000_000


def analyze_bom_csv(content: str) -> dict[str, Any]:
    """Review CSV rows for missing parameters, duplicate references and quantity.

    Expected headers: ``reference,value,footprint,quantity``. ``ref`` or
    ``designator`` may replace ``reference`` and ``qty`` may replace
    ``quantity``. A quoted reference field may contain multiple designators
    separated by commas, semicolons or spaces. A quantity must be a positive
    integer. This performs structural checks, not electrical-rating checks.
    """

    if not isinstance(content, str) or not content.strip():
        raise ValueError("BOM CSV must be nonempty text")
    if len(content) > MAX_BOM_CHARACTERS:
        raise ValueError(f"BOM CSV exceeds {MAX_BOM_CHARACTERS} characters")
    if "\x00" in content:
        raise ValueError("BOM CSV contains a NUL byte")

    reader = csv.reader(io.StringIO(content.lstrip("\ufeff"), newline=""), strict=True)
    try:
        raw_headers = next(reader)
    except (StopIteration, csv.Error):
        raise ValueError("BOM CSV requires a valid header row") from None
    headers = [
        _COLUMN_ALIASES.get(header.strip().lower(), header.strip().lower())
        for header in raw_headers
    ]
    if not all(headers) or len(set(headers)) != len(headers):
        raise ValueError("BOM CSV headers must be nonempty and unique")
    if "reference" not in headers:
        raise ValueError("BOM CSV requires a reference column")

    missing_columns = [column for column in _REQUIRED_COLUMNS if column not in headers]
    components: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    total_quantity = 0

    def issue(code: str, row: int, reference: str, detail: str) -> None:
        issues.append(
            {"code": code, "row": row, "reference": reference, "detail": detail}
        )

    try:
        for row_number, values in enumerate(reader, start=2):
            if not values or not any(value.strip() for value in values):
                continue
            if len(components) >= MAX_BOM_ROWS:
                raise ValueError(f"BOM CSV exceeds {MAX_BOM_ROWS} component rows")
            if len(values) != len(headers):
                issue(
                    "column_count",
                    row_number,
                    "",
                    f"Expected {len(headers)} fields, received {len(values)}",
                )
            row = {
                header: values[index].strip() if index < len(values) else ""
                for index, header in enumerate(headers)
            }
            reference = row.get("reference", "")
            for column in _REQUIRED_COLUMNS:
                if not row.get(column):
                    issue(
                        "missing_parameter",
                        row_number,
                        reference,
                        f"Missing {column}",
                    )
            references = [ref for ref in re.split(r"[,;\s]+", reference) if ref]
            for ref in references:
                key = ref.casefold()
                if key in seen:
                    issue(
                        "duplicate_reference",
                        row_number,
                        ref,
                        f"Reference already appeared on row {seen[key]}",
                    )
                else:
                    seen[key] = row_number
            quantity: int | None = None
            raw_quantity = row.get("quantity", "")
            if raw_quantity:
                if not re.fullmatch(r"[0-9]+", raw_quantity) or len(raw_quantity) > 12:
                    issue(
                        "invalid_quantity",
                        row_number,
                        reference,
                        "Quantity must be a positive integer with at most 12 digits",
                    )
                else:
                    quantity = int(raw_quantity)
                    if quantity <= 0:
                        issue(
                            "invalid_quantity", row_number, reference,
                            "Quantity must be a positive integer",
                        )
                        quantity = None
                    else:
                        total_quantity += quantity
                        if len(references) > 1 and quantity != len(references):
                            issue(
                                "quantity_reference_mismatch",
                                row_number,
                                reference,
                                "Quantity differs from the listed reference count",
                            )
            components.append({"row": row_number, **row, "quantity": quantity})
    except csv.Error as exc:
        raise ValueError(f"Malformed BOM CSV: {exc}") from None

    if not components:
        raise ValueError("BOM CSV requires at least one component row")
    return {
        "total_rows": len(components),
        "total_quantity": total_quantity,
        "missing_columns": missing_columns,
        "components": components,
        "issues": issues,
        "valid": not issues,
        "scope": "Structural CSV checks; electrical-rating checks are not implemented",
    }
