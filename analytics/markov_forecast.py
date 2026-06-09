from __future__ import annotations

import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app import create_app, db
from app.models import (
    DimDate,
    DimMeasure,
    FactHealthArchetype,
    FactHealthTrajectory,
    FactPopulationForecast,
)

STATES = ["normal", "borderline", "abnormal"]
TRANSITIONS = {
    ("normal", "normal"): "normal_to_normal_prob",
    ("normal", "borderline"): "normal_to_borderline_prob",
    ("normal", "abnormal"): "normal_to_abnormal_prob",
    ("borderline", "normal"): "borderline_to_normal_prob",
    ("borderline", "borderline"): "borderline_to_borderline_prob",
    ("borderline", "abnormal"): "borderline_to_abnormal_prob",
    ("abnormal", "normal"): "abnormal_to_normal_prob",
    ("abnormal", "borderline"): "abnormal_to_borderline_prob",
    ("abnormal", "abnormal"): "abnormal_to_abnormal_prob",
}


@dataclass
class ForecastStats:
    measures_seen: int = 0
    forecasts_loaded: int = 0
    measures_skipped: int = 0


def load_measure_groups() -> dict[int, list[FactHealthTrajectory]]:
    rows = (
        FactHealthTrajectory.query.join(DimDate, FactHealthTrajectory.to_date_key == DimDate.date_key)
        .order_by(
            FactHealthTrajectory.measure_key.asc(),
            DimDate.checkup_date.asc(),
            FactHealthTrajectory.trajectory_key.asc(),
        )
        .all()
    )

    grouped: dict[int, list[FactHealthTrajectory]] = defaultdict(list)
    for row in rows:
        grouped[row.measure_key].append(row)
    return grouped


def build_transition_probabilities(rows: list[FactHealthTrajectory]) -> dict[str, float] | None:
    transition_counts: dict[str, Counter[str]] = {
        state: Counter() for state in STATES
    }

    for row in rows:
        if row.previous_status not in STATES or row.current_status not in STATES:
            continue
        transition_counts[row.previous_status][row.current_status] += 1

    probabilities: dict[str, float] = {}
    for from_state in STATES:
        row_total = sum(transition_counts[from_state].values())
        if row_total == 0:
            # A Markov row with no observed outgoing transitions cannot produce an
            # empirical probability estimate. We treat that as insufficient data
            # rather than inventing a smoothing rule silently.
            return None

        for to_state in STATES:
            probabilities[TRANSITIONS[(from_state, to_state)]] = transition_counts[from_state][to_state] / row_total

    return probabilities


def load_current_state_counts(rows: list[FactHealthTrajectory]) -> tuple[int, int, int, int] | None:
    latest_by_person: dict[int, FactHealthTrajectory] = {}
    for row in rows:
        current = latest_by_person.get(row.person_key)
        if current is None:
            latest_by_person[row.person_key] = row
            continue

        current_date = current.to_date.checkup_date if current.to_date else None
        row_date = row.to_date.checkup_date if row.to_date else None
        if row_date is not None and current_date is not None and row_date > current_date:
            latest_by_person[row.person_key] = row
        elif row.to_date_key > current.to_date_key:
            latest_by_person[row.person_key] = row

    base_date_key = max((row.to_date_key for row in latest_by_person.values()), default=None)
    if base_date_key is None:
        return None

    status_counts = Counter(
        row.current_status for row in latest_by_person.values() if row.current_status in STATES
    )
    return (
        base_date_key,
        status_counts.get("normal", 0),
        status_counts.get("borderline", 0),
        status_counts.get("abnormal", 0),
    )


def forecast_next_state(
    current_counts: tuple[int, int, int],
    probabilities: dict[str, float],
) -> tuple[float, float, float]:
    normal_count, borderline_count, abnormal_count = current_counts

    # This is the one-step Markov forecast:
    #   next_state = current_state x transition_matrix
    # where current_state is the population count vector and the matrix contains
    # empirical one-year transition probabilities estimated from prior trajectories.
    forecast_normal = (
        normal_count * probabilities["normal_to_normal_prob"]
        + borderline_count * probabilities["borderline_to_normal_prob"]
        + abnormal_count * probabilities["abnormal_to_normal_prob"]
    )
    forecast_borderline = (
        normal_count * probabilities["normal_to_borderline_prob"]
        + borderline_count * probabilities["borderline_to_borderline_prob"]
        + abnormal_count * probabilities["abnormal_to_borderline_prob"]
    )
    forecast_abnormal = (
        normal_count * probabilities["normal_to_abnormal_prob"]
        + borderline_count * probabilities["borderline_to_abnormal_prob"]
        + abnormal_count * probabilities["abnormal_to_abnormal_prob"]
    )
    return (forecast_normal, forecast_borderline, forecast_abnormal)


def dominant_archetype_name(measure_key: int) -> str | None:
    rows = (
        db.session.query(
            FactHealthArchetype.archetype_name,
            db.func.count(FactHealthArchetype.archetype_key),
        )
        .filter(FactHealthArchetype.measure_key == measure_key)
        .group_by(FactHealthArchetype.archetype_name)
        .order_by(db.func.count(FactHealthArchetype.archetype_key).desc())
        .all()
    )
    return rows[0][0] if rows else None


def build_interpretation(
    measure_name: str,
    current_counts: tuple[int, int, int],
    forecast_counts: tuple[float, float, float],
    probabilities: dict[str, float],
    dominant_archetype: str | None,
) -> str:
    _, _, current_abnormal = current_counts
    _, _, forecast_abnormal = forecast_counts

    abnormal_direction = "increase" if forecast_abnormal > current_abnormal else "decrease"
    anchor_transition = max(
        [
            ("normal_to_abnormal", probabilities["normal_to_abnormal_prob"]),
            ("borderline_to_abnormal", probabilities["borderline_to_abnormal_prob"]),
            ("abnormal_to_abnormal", probabilities["abnormal_to_abnormal_prob"]),
        ],
        key=lambda item: item[1],
    )[0]

    archetype_text = f" Dominant archetype: {dominant_archetype}." if dominant_archetype else ""
    return (
        f"One-year Markov forecast for {measure_name} suggests an {abnormal_direction} in the abnormal population. "
        f"The strongest transition driver is {anchor_transition}.{archetype_text}"
    )


def rebuild_population_forecasts() -> ForecastStats:
    stats = ForecastStats()
    groups = load_measure_groups()
    stats.measures_seen = len(groups)

    db.session.execute(delete(FactPopulationForecast))
    db.session.flush()

    if not groups:
        return stats

    for measure_key, rows in groups.items():
        probabilities = build_transition_probabilities(rows)
        current_state = load_current_state_counts(rows)
        if probabilities is None or current_state is None:
            stats.measures_skipped += 1
            continue

        base_date_key, current_normal, current_borderline, current_abnormal = current_state
        total_current = current_normal + current_borderline + current_abnormal
        if total_current == 0:
            stats.measures_skipped += 1
            continue

        forecast_counts = forecast_next_state(
            (current_normal, current_borderline, current_abnormal),
            probabilities,
        )
        measure = db.session.get(DimMeasure, measure_key)
        dominant_archetype = dominant_archetype_name(measure_key)
        interpretation = build_interpretation(
            measure_name=measure.measure_name if measure else f"Measure {measure_key}",
            current_counts=(current_normal, current_borderline, current_abnormal),
            forecast_counts=forecast_counts,
            probabilities=probabilities,
            dominant_archetype=dominant_archetype,
        )

        db.session.add(
            FactPopulationForecast(
                measure_key=measure_key,
                base_date_key=base_date_key,
                forecast_horizon_years=1,
                current_normal_count=current_normal,
                current_borderline_count=current_borderline,
                current_abnormal_count=current_abnormal,
                forecast_normal_count=round(forecast_counts[0], 2),
                forecast_borderline_count=round(forecast_counts[1], 2),
                forecast_abnormal_count=round(forecast_counts[2], 2),
                normal_to_normal_prob=round(probabilities["normal_to_normal_prob"], 4),
                normal_to_borderline_prob=round(probabilities["normal_to_borderline_prob"], 4),
                normal_to_abnormal_prob=round(probabilities["normal_to_abnormal_prob"], 4),
                borderline_to_normal_prob=round(probabilities["borderline_to_normal_prob"], 4),
                borderline_to_borderline_prob=round(probabilities["borderline_to_borderline_prob"], 4),
                borderline_to_abnormal_prob=round(probabilities["borderline_to_abnormal_prob"], 4),
                abnormal_to_normal_prob=round(probabilities["abnormal_to_normal_prob"], 4),
                abnormal_to_borderline_prob=round(probabilities["abnormal_to_borderline_prob"], 4),
                abnormal_to_abnormal_prob=round(probabilities["abnormal_to_abnormal_prob"], 4),
                interpretation=interpretation,
            )
        )
        stats.forecasts_loaded += 1

    db.session.commit()
    return stats


def print_report(stats: ForecastStats) -> None:
    print("Population Forecast Build Report")
    print("================================")
    print(f"Measures seen: {stats.measures_seen}")
    print(f"Forecasts loaded: {stats.forecasts_loaded}")
    print(f"Measures skipped: {stats.measures_skipped}")

    if stats.measures_seen == 0 or stats.forecasts_loaded == 0:
        print("Not enough trajectory data to generate population forecasts.")


def main() -> int:
    app = create_app()
    with app.app_context():
        db.create_all()
        stats = rebuild_population_forecasts()
        print_report(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
