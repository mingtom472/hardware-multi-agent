from decimal import Decimal

import pytest
from app.hardware import analyze_bom_csv, calculate_buck_duty


def test_buck_duty_uses_decimal_and_reports_assumptions():
    result = calculate_buck_duty("12", "5")
    assert Decimal(result["duty_cycle"]) == Decimal(5) / Decimal(12)
    assert result["formula"] == "D = Vout / Vin"
    assert result["assumptions"]


@pytest.mark.parametrize(
    "vin,vout",
    [(0, 1), (5, 5), (5, 6), (12, -1), ("NaN", 1), (12, "Infinity"), (True, 0.5)],
)
def test_buck_rejects_invalid_operating_points(vin, vout):
    with pytest.raises(ValueError):
        calculate_buck_duty(vin, vout)


@pytest.mark.parametrize(
    "vin,vout",
    [
        ("1e999999999", "1"),
        ("1", "1e-999999999"),
        ("1", "1e-1000000"),
        ("1.00000000000000000000000000001", "1"),
    ],
)
def test_buck_rejects_unsupported_decimal_range_or_precision(vin, vout):
    with pytest.raises(ValueError):
        calculate_buck_duty(vin, vout)


def test_valid_bom_counts_components_and_quantities():
    result = analyze_bom_csv(
        'reference,value,footprint,quantity\n"R1 R2",10k,0603,2\nC1,100nF,0603,1\n'
    )
    assert result["valid"] is True
    assert result["total_rows"] == 2
    assert result["total_quantity"] == 3
    assert result["issues"] == []


def test_bom_reports_duplicates_missing_fields_and_invalid_quantity():
    result = analyze_bom_csv(
        "ref,value,footprint,qty\nR1,10k,0603,1\nr1,,0603,-2\nC1,1uF,,1.5\n"
    )
    codes = [issue["code"] for issue in result["issues"]]
    assert "duplicate_reference" in codes
    assert codes.count("missing_parameter") == 2
    assert codes.count("invalid_quantity") == 2
    assert result["total_quantity"] == 1
    assert result["valid"] is False


def test_bom_missing_columns_and_inconsistent_row_are_reported():
    result = analyze_bom_csv("reference,value\nR1,10k,extra\n")
    assert result["missing_columns"] == ["footprint", "quantity"]
    assert any(issue["code"] == "column_count" for issue in result["issues"])


@pytest.mark.parametrize(
    "content",
    [
        "",
        "reference,value,footprint,quantity\n",
        "value,footprint,quantity\n10k,0603,1\n",
        "reference,ref\nR1,R2\n",
        'reference,value,footprint,quantity\nR1,"unclosed\n',
        "reference,value\nR1,\x00\n",
    ],
)
def test_bom_rejects_malformed_or_empty_input(content):
    with pytest.raises(ValueError):
        analyze_bom_csv(content)
