from __future__ import annotations

from typing import Any

THRESHOLD_RULES = {
    "Gluc": {"normal_max": 99, "borderline_max": 125},
    "LDL": {"normal_max": 99, "borderline_max": 159},
    "TG": {"normal_max": 149, "borderline_max": 199},
    "Chol": {"normal_max": 199, "borderline_max": 239},
    "BMI": {"normal_max": 22.9, "borderline_max": 24.9},
    "AST": {"normal_max": 40, "borderline_max": 80},
    "ALT": {"normal_max": 40, "borderline_max": 80},
    "CRE": {"normal_max": 1.2, "borderline_max": 1.5},
    "Uric": {"normal_max": 7.0, "borderline_max": 8.5},
    "HDL": {"normal_min": 60, "borderline_min": 40},
}

HIGHER_IS_BETTER = {"HDL"}


def classify_status(measure_code: str, value: Any) -> str:
    if value is None:
        return "unknown"

    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return "unknown"

    rules = THRESHOLD_RULES.get(measure_code)
    if rules is None:
        return "unknown"

    if measure_code in HIGHER_IS_BETTER:
        normal_min = rules["normal_min"]
        borderline_min = rules["borderline_min"]
        if numeric_value >= normal_min:
            return "normal"
        if numeric_value >= borderline_min:
            return "borderline"
        return "abnormal"

    normal_max = rules["normal_max"]
    borderline_max = rules["borderline_max"]
    if numeric_value <= normal_max:
        return "normal"
    if numeric_value <= borderline_max:
        return "borderline"
    return "abnormal"
