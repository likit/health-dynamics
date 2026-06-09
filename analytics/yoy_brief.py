from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy.orm import aliased

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app import db
from app.models import (
    DimDate,
    DimMeasure,
    DimPerson,
    FactCheckupMeasurement,
    FactDataQuality,
    FactHealthTrajectory,
    FactPersonCheckupSnapshot,
)
from analytics.risk_rules import THRESHOLD_RULES, classify_status


MEASURE_CODES = [code for code in THRESHOLD_RULES if code != "BMI"]
YOY_MEASURE_CODES = ["BMI", *MEASURE_CODES]


def generate_yoy_brief() -> dict:
    """
    Build an aggregated year-over-year briefing payload.

    The brief separates three concepts explicitly:
    1. Cross-sectional status comparison:
       all available people in each year, even when the year populations differ.
    2. Matched cohort status comparison:
       only people with the same measure in both years.
    3. Trajectory movement:
       explicit previous-year to latest-year transitions from fact_health_trajectory.

    This separation prevents the dashboard from mixing cross-sectional prevalence
    changes with true matched-person movement.
    """

    years = load_available_years()
    if len(years) < 2:
        return {
            "available": False,
            "reason": "Year-over-year briefing requires at least two checkup years.",
        }

    latest_year = years[-1]
    previous_year = years[-2]

    previous_snapshot_people = load_snapshot_population(previous_year)
    latest_snapshot_people = load_snapshot_population(latest_year)
    matched_snapshot_people = previous_snapshot_people & latest_snapshot_people

    previous_measure_maps = load_year_measure_maps(previous_year)
    latest_measure_maps = load_year_measure_maps(latest_year)

    cross_sectional_comparison = build_cross_sectional_comparison(
        previous_measure_maps,
        latest_measure_maps,
    )
    matched_cohort_comparison = build_matched_cohort_comparison(
        previous_measure_maps,
        latest_measure_maps,
    )

    trajectory_rows = load_year_pair_trajectory_rows(previous_year, latest_year)
    trajectory_movement = summarize_trajectory_movement(trajectory_rows)
    trajectory_measure_summary = summarize_trajectory_by_measure(
        trajectory_rows,
        matched_cohort_comparison,
    )
    department_attention = summarize_department_attention(trajectory_rows)
    data_caveats = build_data_caveats(
        cross_sectional_comparison,
        matched_cohort_comparison,
    )
    positive_signals = build_positive_signals(
        cross_sectional_comparison,
        matched_cohort_comparison,
        trajectory_movement,
    )
    concern_signals = build_concern_signals(
        cross_sectional_comparison,
        matched_cohort_comparison,
        trajectory_rows,
        department_attention,
    )
    suggested_explorations = build_suggested_explorations(
        cross_sectional_comparison,
        matched_cohort_comparison,
        trajectory_rows,
        data_caveats,
    )

    return {
        "available": True,
        "latest_year": latest_year,
        "previous_year": previous_year,
        "population_summary": {
            "latest_person_count": len(latest_snapshot_people),
            "previous_person_count": len(previous_snapshot_people),
            "matched_person_count": len(matched_snapshot_people),
        },
        "cross_sectional_comparison": cross_sectional_comparison,
        "matched_cohort_comparison": matched_cohort_comparison,
        "trajectory_movement": trajectory_movement,
        "trajectory_measure_summary": trajectory_measure_summary,
        "department_attention": department_attention,
        "positive_signals": positive_signals,
        "concern_signals": concern_signals,
        "data_caveats": data_caveats,
        "suggested_explorations": suggested_explorations,
    }


def load_available_years() -> list[int]:
    rows = (
        db.session.query(DimDate.checkup_year)
        .distinct()
        .order_by(DimDate.checkup_year.asc())
        .all()
    )
    return [year for (year,) in rows]


def load_snapshot_population(year: int) -> set[int]:
    rows = (
        db.session.query(FactPersonCheckupSnapshot.person_key)
        .join(DimDate, FactPersonCheckupSnapshot.date_key == DimDate.date_key)
        .filter(DimDate.checkup_year == year)
        .distinct()
        .all()
    )
    return {person_key for (person_key,) in rows}


def load_year_measure_maps(year: int) -> dict[str, dict[int, float]]:
    """
    Load one latest value per person and measure for a single year.

    BMI comes from the snapshot fact table. Lab measures come from the long-table
    measurement fact. The result is keyed by measure code, then person key.
    """

    measure_maps: dict[str, dict[int, float]] = {measure_code: {} for measure_code in YOY_MEASURE_CODES}

    bmi_rows = (
        db.session.query(
            FactPersonCheckupSnapshot.person_key,
            FactPersonCheckupSnapshot.bmi,
        )
        .join(DimDate, FactPersonCheckupSnapshot.date_key == DimDate.date_key)
        .filter(DimDate.checkup_year == year)
        .order_by(
            FactPersonCheckupSnapshot.person_key.asc(),
            DimDate.checkup_date.desc(),
            FactPersonCheckupSnapshot.snapshot_key.desc(),
        )
        .all()
    )
    for person_key, bmi in bmi_rows:
        measure_maps["BMI"].setdefault(person_key, bmi)

    measurement_rows = (
        db.session.query(
            FactCheckupMeasurement.person_key,
            DimMeasure.measure_code,
            FactCheckupMeasurement.value_numeric,
        )
        .join(DimMeasure, FactCheckupMeasurement.measure_key == DimMeasure.measure_key)
        .join(DimDate, FactCheckupMeasurement.date_key == DimDate.date_key)
        .filter(
            DimDate.checkup_year == year,
            DimMeasure.measure_code.in_(MEASURE_CODES),
        )
        .order_by(
            FactCheckupMeasurement.person_key.asc(),
            DimMeasure.measure_code.asc(),
            DimDate.checkup_date.desc(),
            FactCheckupMeasurement.fact_key.desc(),
        )
        .all()
    )
    for person_key, measure_code, value_numeric in measurement_rows:
        measure_maps[measure_code].setdefault(person_key, value_numeric)

    return measure_maps


def build_cross_sectional_comparison(
    previous_measure_maps: dict[str, dict[int, float]],
    latest_measure_maps: dict[str, dict[int, float]],
) -> list[dict]:
    comparison: list[dict] = []
    for measure_code in YOY_MEASURE_CODES:
        previous_values = list(previous_measure_maps.get(measure_code, {}).values())
        latest_values = list(latest_measure_maps.get(measure_code, {}).values())
        previous_stats = calculate_abnormal_stats(measure_code, previous_values)
        latest_stats = calculate_abnormal_stats(measure_code, latest_values)
        if previous_stats is None or latest_stats is None:
            continue

        delta = round(
            calculate_percentage_point_delta(
                previous_stats["abnormal_percent"],
                latest_stats["abnormal_percent"],
            ),
            2,
        )
        comparison.append(
            {
                "label": "cross-sectional",
                "measure": measure_code,
                "denominator_previous": previous_stats["denominator"],
                "denominator_latest": latest_stats["denominator"],
                "abnormal_percent_previous": round(previous_stats["abnormal_percent"], 2),
                "abnormal_percent_latest": round(latest_stats["abnormal_percent"], 2),
                "delta_percentage_points": delta,
                "direction": describe_percentage_direction(delta),
            }
        )

    return sorted(comparison, key=lambda row: abs(row["delta_percentage_points"]), reverse=True)


def build_matched_cohort_comparison(
    previous_measure_maps: dict[str, dict[int, float]],
    latest_measure_maps: dict[str, dict[int, float]],
) -> list[dict]:
    comparison: list[dict] = []
    for measure_code in YOY_MEASURE_CODES:
        previous_people = set(previous_measure_maps.get(measure_code, {}))
        latest_people = set(latest_measure_maps.get(measure_code, {}))
        matched_people = previous_people & latest_people

        previous_values = [previous_measure_maps[measure_code][person_key] for person_key in matched_people]
        latest_values = [latest_measure_maps[measure_code][person_key] for person_key in matched_people]
        previous_stats = calculate_abnormal_stats(measure_code, previous_values)
        latest_stats = calculate_abnormal_stats(measure_code, latest_values)
        if previous_stats is None or latest_stats is None:
            continue

        matched_denominator = len(matched_people)
        delta = round(
            calculate_percentage_point_delta(
                previous_stats["abnormal_percent"],
                latest_stats["abnormal_percent"],
            ),
            2,
        )
        comparison.append(
            {
                "label": "matched cohort",
                "measure": measure_code,
                "denominator_matched": matched_denominator,
                "denominator_previous_matched": matched_denominator,
                "denominator_latest_matched": matched_denominator,
                "abnormal_percent_previous_matched": round(previous_stats["abnormal_percent"], 2),
                "abnormal_percent_latest_matched": round(latest_stats["abnormal_percent"], 2),
                "delta_percentage_points_matched": delta,
                "direction": describe_percentage_direction(delta),
            }
        )

    return sorted(comparison, key=lambda row: abs(row["delta_percentage_points_matched"]), reverse=True)


def calculate_abnormal_stats(measure_code: str, values: list[float]) -> dict | None:
    if not values:
        return None

    statuses = [classify_status(measure_code, value) for value in values]
    abnormal_count = len([status for status in statuses if status == "abnormal"])
    denominator = len(values)
    return {
        "abnormal_count": abnormal_count,
        "denominator": denominator,
        "abnormal_percent": calculate_percentage(abnormal_count, denominator),
    }


def calculate_percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return (numerator / denominator) * 100.0


def calculate_percentage_point_delta(previous_percent: float, latest_percent: float) -> float:
    return latest_percent - previous_percent


def describe_percentage_direction(delta_percentage_points: float) -> str:
    if delta_percentage_points > 0.1:
        return "abnormal percentage increased"
    if delta_percentage_points < -0.1:
        return "abnormal percentage decreased"
    return "abnormal percentage stable"


def filter_trajectory_rows_for_year_pair(rows: list[dict], previous_year: int, latest_year: int) -> list[dict]:
    return [
        row
        for row in rows
        if row["from_year"] == previous_year and row["to_year"] == latest_year
    ]


def load_year_pair_trajectory_rows(previous_year: int, latest_year: int) -> list[dict]:
    from_date = aliased(DimDate)
    to_date = aliased(DimDate)
    rows = (
        db.session.query(
            FactHealthTrajectory.status_transition,
            FactHealthTrajectory.trajectory_class,
            FactHealthTrajectory.risk_direction,
            DimPerson.department,
            DimMeasure.measure_code,
            from_date.checkup_year,
            to_date.checkup_year,
        )
        .join(DimPerson, FactHealthTrajectory.person_key == DimPerson.person_key)
        .join(DimMeasure, FactHealthTrajectory.measure_key == DimMeasure.measure_key)
        .join(from_date, FactHealthTrajectory.from_date_key == from_date.date_key)
        .join(to_date, FactHealthTrajectory.to_date_key == to_date.date_key)
        .all()
    )
    row_dicts = [
        {
            "status_transition": row.status_transition,
            "trajectory_class": row.trajectory_class,
            "risk_direction": row.risk_direction,
            "department": (row.department or "").strip(),
            "measure_code": row.measure_code,
            "from_year": row[5],
            "to_year": row[6],
        }
        for row in rows
    ]
    return filter_trajectory_rows_for_year_pair(row_dicts, previous_year, latest_year)


def summarize_trajectory_movement(rows: list[dict]) -> dict:
    return {
        "improving_count": len([row for row in rows if row["trajectory_class"] == "improving"]),
        "stable_count": len([row for row in rows if row["trajectory_class"] == "stable"]),
        "worsening_count": len([row for row in rows if row["trajectory_class"] == "worsening"]),
        "higher_risk_transition_count": len([row for row in rows if row["risk_direction"] == "higher_risk"]),
        "lower_risk_transition_count": len([row for row in rows if row["risk_direction"] == "lower_risk"]),
        "persistent_abnormal_count": len([row for row in rows if row["status_transition"] == "abnormal_to_abnormal"]),
    }


def summarize_trajectory_by_measure(
    rows: list[dict],
    matched_cohort_comparison: list[dict],
) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    matched_lookup = {row["measure"]: row for row in matched_cohort_comparison}
    for row in rows:
        grouped[row["measure_code"]].append(row)

    summary: list[dict] = []
    for measure_code, measure_rows in grouped.items():
        paired_n = matched_lookup.get(measure_code, {}).get("denominator_matched", len(measure_rows))
        denominator = len(measure_rows) or 1
        summary.append(
            {
                "metric": measure_code,
                "persistent_abnormal_pct": round(
                    calculate_percentage(
                        len([row for row in measure_rows if row["status_transition"] == "abnormal_to_abnormal"]),
                        denominator,
                    ),
                    2,
                ),
                "deteriorating_pct": round(
                    calculate_percentage(
                        len([row for row in measure_rows if row["trajectory_class"] == "worsening"]),
                        denominator,
                    ),
                    2,
                ),
                "improving_pct": round(
                    calculate_percentage(
                        len([row for row in measure_rows if row["trajectory_class"] == "improving"]),
                        denominator,
                    ),
                    2,
                ),
                "paired_n": paired_n,
            }
        )

    return sorted(summary, key=lambda row: row["metric"])


def summarize_department_attention(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["department"]:
            grouped[row["department"]].append(row)

    summary: list[dict] = []
    for department, department_rows in grouped.items():
        total = len(department_rows) or 1
        summary.append(
            {
                "department": department,
                "worsening_percent": round(
                    calculate_percentage(
                        len([row for row in department_rows if row["trajectory_class"] == "worsening"]),
                        total,
                    ),
                    2,
                ),
                "higher_risk_transition_percent": round(
                    calculate_percentage(
                        len([row for row in department_rows if row["risk_direction"] == "higher_risk"]),
                        total,
                    ),
                    2,
                ),
                "persistent_abnormal_percent": round(
                    calculate_percentage(
                        len([row for row in department_rows if row["status_transition"] == "abnormal_to_abnormal"]),
                        total,
                    ),
                    2,
                ),
            }
        )

    return sorted(
        summary,
        key=lambda row: (
            -row["worsening_percent"],
            -row["higher_risk_transition_percent"],
            row["department"].lower(),
        ),
    )[:10]


def build_positive_signals(
    cross_sectional_comparison: list[dict],
    matched_cohort_comparison: list[dict],
    trajectory_movement: dict,
) -> list[str]:
    signals: list[str] = []

    top_matched_decrease = next(
        (row for row in matched_cohort_comparison if row["delta_percentage_points_matched"] < -0.1),
        None,
    )
    if top_matched_decrease is not None:
        signals.append(
            f"{top_matched_decrease['measure']} abnormal percentage decreased in the matched cohort by "
            f"{abs(top_matched_decrease['delta_percentage_points_matched']):.2f} percentage points."
        )

    if trajectory_movement["lower_risk_transition_count"] > 0:
        signals.append("Some matched-person trajectories moved toward lower-risk status.")

    if trajectory_movement["improving_count"] > trajectory_movement["worsening_count"]:
        signals.append("Trajectory improvement exceeded trajectory worsening in the selected year pair.")

    return deduplicate_strings(signals)


def build_concern_signals(
    cross_sectional_comparison: list[dict],
    matched_cohort_comparison: list[dict],
    trajectory_rows: list[dict],
    department_attention: list[dict],
) -> list[str]:
    signals: list[str] = []

    top_matched_increase = next(
        (row for row in matched_cohort_comparison if row["delta_percentage_points_matched"] > 0.1),
        None,
    )
    if top_matched_increase is not None:
        signals.append(
            f"{top_matched_increase['measure']} abnormal percentage increased in the matched cohort by "
            f"{top_matched_increase['delta_percentage_points_matched']:.2f} percentage points."
        )

    ldl_persistent_count = len(
        [
            row
            for row in trajectory_rows
            if row["measure_code"] == "LDL" and row["status_transition"] == "abnormal_to_abnormal"
        ]
    )
    if ldl_persistent_count > 0:
        signals.append("Persistent abnormal LDL remains high in the matched trajectory population.")

    if department_attention:
        top_department = department_attention[0]
        if top_department["worsening_percent"] >= 20 or top_department["higher_risk_transition_percent"] >= 15:
            signals.append(f"Department {top_department['department']} shows elevated worsening trajectories.")

    return deduplicate_strings(signals)


def build_data_caveats(
    cross_sectional_comparison: list[dict],
    matched_cohort_comparison: list[dict],
) -> list[str]:
    caveats: list[str] = []

    if any(row["denominator_previous"] != row["denominator_latest"] for row in cross_sectional_comparison):
        caveats.append(
            "The cross-sectional comparison uses different yearly populations, so matched cohort results should be used to interpret true health movement."
        )

    for cross_row in cross_sectional_comparison:
        matched_row = next(
            (row for row in matched_cohort_comparison if row["measure"] == cross_row["measure"]),
            None,
        )
        if matched_row is None:
            continue
        matched_denominator = matched_row["denominator_matched"]
        if (
            (cross_row["denominator_previous"] > 0 and matched_denominator < 0.7 * cross_row["denominator_previous"])
            or
            (cross_row["denominator_latest"] > 0 and matched_denominator < 0.7 * cross_row["denominator_latest"])
        ):
            caveats.append("Matched cohort coverage is limited; trajectory interpretation should be cautious.")
            break

    quality_rows = (
        FactDataQuality.query.filter(FactDataQuality.warning_level.in_(["caution", "high_risk"]))
        .order_by(FactDataQuality.warning_level.desc(), FactDataQuality.check_type.asc())
        .limit(5)
        .all()
    )
    for row in quality_rows:
        caveats.append(row.interpretation)

    return deduplicate_strings(caveats)


def build_suggested_explorations(
    cross_sectional_comparison: list[dict],
    matched_cohort_comparison: list[dict],
    trajectory_rows: list[dict],
    data_caveats: list[str],
) -> list[str]:
    suggestions: list[str] = []

    top_matched_increase = next(
        (row for row in matched_cohort_comparison if row["delta_percentage_points_matched"] > 0.1),
        None,
    )
    if top_matched_increase is not None:
        suggestions.append(f"Explore {top_matched_increase['measure']} deterioration")

    if any(row["department"] for row in trajectory_rows):
        suggestions.append("Compare departments with high worsening trajectories")

    if any(row["measure_code"] == "LDL" and row["status_transition"] == "abnormal_to_abnormal" for row in trajectory_rows):
        suggestions.append("Review persistent abnormal LDL")

    if data_caveats:
        suggestions.append("Inspect data quality limitations")

    return deduplicate_strings(suggestions)


def deduplicate_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def run_self_checks() -> None:
    assert round(calculate_percentage(14, 100), 2) == 14.00
    assert round(calculate_percentage_point_delta(13.97, 9.07), 2) == -4.90

    previous_measure_maps = {"Gluc": {1: 100.0, 2: 90.0, 3: 80.0}}
    latest_measure_maps = {"Gluc": {2: 110.0, 3: 95.0, 4: 99.0}}
    matched_rows = build_matched_cohort_comparison(
        {"BMI": {}, **previous_measure_maps, **{code: {} for code in MEASURE_CODES if code != "Gluc"}},
        {"BMI": {}, **latest_measure_maps, **{code: {} for code in MEASURE_CODES if code != "Gluc"}},
    )
    gluc_matched = next(row for row in matched_rows if row["measure"] == "Gluc")
    assert gluc_matched["denominator_matched"] == 2

    filtered_rows = filter_trajectory_rows_for_year_pair(
        [
            {"from_year": 2024, "to_year": 2025},
            {"from_year": 2023, "to_year": 2024},
            {"from_year": 2024, "to_year": 2025},
        ],
        2024,
        2025,
    )
    assert len(filtered_rows) == 2


def main() -> int:
    from app import create_app

    run_self_checks()

    app = create_app()
    with app.app_context():
        brief = generate_yoy_brief()
    print(json.dumps(brief, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
