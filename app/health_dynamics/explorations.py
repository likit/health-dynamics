from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import aliased

from app import db
from app.health_dynamics.insight_rules import METRIC_LABELS, humanize_drilldown_labels
from app.models import DimDate, DimMeasure, DimPerson, FactHealthTrajectory, FactPersonCheckupSnapshot
from analytics.yoy_brief import calculate_percentage, load_available_years

VALID_DIMENSIONS = {"department", "age_group", "sex"}
VALID_PATTERNS = {"persistent_abnormal", "deteriorating", "improving", "mixed"}


def humanize_pattern(pattern: str | None) -> str:
    if not pattern:
        return "All Patterns"
    return pattern.replace("_", " ").title()


def build_exploration_actions(insight_packets: list[dict], year: int | None) -> list[dict]:
    if year is None:
        return []

    lead_packets = insight_packets[:1]
    actions: list[dict] = []
    seen: set[tuple[int, str, str, str]] = set()
    for packet in lead_packets:
        metric = packet.get("metric")
        pattern = packet.get("pattern")
        if not metric or pattern not in VALID_PATTERNS:
            continue

        for dimension in packet.get("recommended_drilldown", []):
            if dimension not in VALID_DIMENSIONS:
                continue

            key = (year, metric, dimension, pattern)
            if key in seen:
                continue
            seen.add(key)

            dimension_label = humanize_drilldown_labels([dimension])[0]
            metric_label = METRIC_LABELS.get(metric, metric)
            actions.append(
                {
                    "label": f"Explore {metric_label} by {dimension_label}",
                    "metric": metric,
                    "metric_label": metric_label,
                    "year": year,
                    "dimension": dimension,
                    "dimension_label": dimension_label,
                    "pattern": pattern,
                    "pattern_label": humanize_pattern(pattern),
                }
            )

    return actions


def build_exploration_context(year: int | None, metric: str | None, dimension: str | None, pattern: str | None) -> dict:
    if year is None or not metric or not dimension:
        return {"available": False, "message": "The dashboard does not contain enough information to perform this exploration."}

    if dimension not in VALID_DIMENSIONS or metric not in METRIC_LABELS:
        return {"available": False, "message": "The dashboard does not contain enough information to perform this exploration."}

    if pattern and pattern not in VALID_PATTERNS:
        return {"available": False, "message": "The dashboard does not contain enough information to perform this exploration."}

    years = load_available_years()
    if year not in years:
        return {"available": False, "message": "The dashboard does not contain enough information to perform this exploration."}

    year_index = years.index(year)
    if year_index == 0:
        return {"available": False, "message": "The dashboard does not contain enough information to perform this exploration."}

    previous_year = years[year_index - 1]
    grouped_results = load_grouped_exploration_results(
        year=year,
        previous_year=previous_year,
        metric=metric,
        dimension=dimension,
    )
    if not grouped_results:
        return {"available": False, "message": "The dashboard does not contain enough information to perform this exploration."}

    top_persistent = max(grouped_results, key=lambda row: (row["persistent_abnormal_pct"], row["paired_n"], row["group"]))
    top_deteriorating = max(grouped_results, key=lambda row: (row["deteriorating_pct"], row["paired_n"], row["group"]))
    total_matched = sum(row["paired_n"] for row in grouped_results)

    metric_label = METRIC_LABELS.get(metric, metric)
    dimension_label = humanize_drilldown_labels([dimension])[0]
    pattern_label = humanize_pattern(pattern)

    return {
        "available": True,
        "year": year,
        "previous_year": previous_year,
        "metric": metric,
        "metric_label": metric_label,
        "dimension": dimension,
        "dimension_label": dimension_label,
        "pattern": pattern,
        "pattern_label": pattern_label,
        "title": f"{metric_label} Exploration by {dimension_label}",
        "explanation": f"This page shows matched-year {metric_label} patterns grouped by {dimension_label}.",
        "summary": {
            "top_persistent_group": top_persistent["group"],
            "top_persistent_pct": top_persistent["persistent_abnormal_pct"],
            "top_deteriorating_group": top_deteriorating["group"],
            "top_deteriorating_pct": top_deteriorating["deteriorating_pct"],
            "total_matched_employees": total_matched,
        },
        "results": grouped_results,
    }


def load_grouped_exploration_results(*, year: int, previous_year: int, metric: str, dimension: str) -> list[dict]:
    trajectory_rows = load_trajectory_dimension_rows(
        year=year,
        previous_year=previous_year,
        metric=metric,
    )
    if not trajectory_rows:
        return []

    if dimension == "age_group":
        age_groups = load_age_groups_for_year(year)
        for row in trajectory_rows:
            row["group"] = age_groups.get(row["person_key"], "Unknown")
    elif dimension == "sex":
        genders = load_genders()
        for row in trajectory_rows:
            row["group"] = genders.get(row["person_key"], "Unknown")
    else:
        for row in trajectory_rows:
            department = (row["department"] or "").strip()
            row["group"] = department or "Unassigned"

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in trajectory_rows:
        grouped[row["group"]].append(row)

    results: list[dict] = []
    for group_name, rows in grouped.items():
        paired_n = len(rows)
        results.append(
            {
                "group": group_name,
                "paired_n": paired_n,
                "persistent_abnormal_pct": round(
                    calculate_percentage(
                        len([row for row in rows if row["status_transition"] == "abnormal_to_abnormal"]),
                        paired_n,
                    ),
                    1,
                ),
                "deteriorating_pct": round(
                    calculate_percentage(
                        len([row for row in rows if row["trajectory_class"] == "worsening"]),
                        paired_n,
                    ),
                    1,
                ),
                "improving_pct": round(
                    calculate_percentage(
                        len([row for row in rows if row["trajectory_class"] == "improving"]),
                        paired_n,
                    ),
                    1,
                ),
            }
        )

    return sorted(
        results,
        key=lambda row: (-row["persistent_abnormal_pct"], -row["deteriorating_pct"], row["group"].lower()),
    )


def load_trajectory_dimension_rows(*, year: int, previous_year: int, metric: str) -> list[dict]:
    from_date = aliased(DimDate)
    to_date = aliased(DimDate)
    rows = (
        db.session.query(
            FactHealthTrajectory.person_key,
            FactHealthTrajectory.status_transition,
            FactHealthTrajectory.trajectory_class,
            DimPerson.department,
        )
        .join(DimMeasure, FactHealthTrajectory.measure_key == DimMeasure.measure_key)
        .join(DimPerson, FactHealthTrajectory.person_key == DimPerson.person_key)
        .join(from_date, FactHealthTrajectory.from_date_key == from_date.date_key)
        .join(to_date, FactHealthTrajectory.to_date_key == to_date.date_key)
        .filter(
            DimMeasure.measure_code == metric,
            from_date.checkup_year == previous_year,
            to_date.checkup_year == year,
        )
        .all()
    )
    return [
        {
            "person_key": row.person_key,
            "status_transition": row.status_transition,
            "trajectory_class": row.trajectory_class,
            "department": row.department,
        }
        for row in rows
    ]


def load_age_groups_for_year(year: int) -> dict[int, str]:
    rows = (
        db.session.query(
            FactPersonCheckupSnapshot.person_key,
            FactPersonCheckupSnapshot.age,
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

    age_groups: dict[int, str] = {}
    for person_key, age in rows:
        age_groups.setdefault(person_key, classify_age_group(age))
    return age_groups


def load_genders() -> dict[int, str]:
    rows = db.session.query(DimPerson.person_key, DimPerson.gender).all()
    return {
        person_key: ((gender or "").strip() or "Unknown")
        for person_key, gender in rows
    }


def classify_age_group(age: int | None) -> str:
    if age is None:
        return "Unknown"
    if age < 30:
        return "Under 30"
    if age < 40:
        return "30-39"
    if age < 50:
        return "40-49"
    if age < 60:
        return "50-59"
    return "60 and above"
