from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import delete

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from analytics.risk_rules import classify_status
from app import create_app, db
from app.models import (
    DimDate,
    DimMeasure,
    FactCheckupMeasurement,
    FactHealthTrajectory,
    FactPersonCheckupSnapshot,
)

TRAJECTORY_MEASURES = {
    "Gluc": {"measure_name": "Glucose", "category": "Laboratory", "unit": None, "source": "measurement"},
    "LDL": {"measure_name": "Low-Density Lipoprotein", "category": "Laboratory", "unit": None, "source": "measurement"},
    "TG": {"measure_name": "Triglycerides", "category": "Laboratory", "unit": None, "source": "measurement"},
    "Chol": {"measure_name": "Cholesterol", "category": "Laboratory", "unit": None, "source": "measurement"},
    "BMI": {"measure_name": "Body Mass Index", "category": "Anthropometric", "unit": None, "source": "snapshot"},
    "AST": {"measure_name": "Aspartate Aminotransferase", "category": "Laboratory", "unit": None, "source": "measurement"},
    "ALT": {"measure_name": "Alanine Aminotransferase", "category": "Laboratory", "unit": None, "source": "measurement"},
    "CRE": {"measure_name": "Creatinine", "category": "Laboratory", "unit": None, "source": "measurement"},
    "Uric": {"measure_name": "Uric Acid", "category": "Laboratory", "unit": None, "source": "measurement"},
    "HDL": {"measure_name": "High-Density Lipoprotein", "category": "Laboratory", "unit": None, "source": "measurement"},
}
HIGHER_IS_BETTER = {"HDL"}
STATUS_RANK = {"normal": 0, "borderline": 1, "abnormal": 2}


@dataclass
class Observation:
    person_key: int
    measure_key: int
    date_key: int
    checkup_date: date
    checkup_year: int
    value: float


@dataclass
class TrajectoryStats:
    measures_prepared: int = 0
    observations_seen: int = 0
    persons_loaded: int = 0
    trajectories_loaded: int = 0


def get_or_create_measure(measure_code: str) -> DimMeasure:
    measure = DimMeasure.query.filter_by(measure_code=measure_code).one_or_none()
    if measure is None:
        metadata = TRAJECTORY_MEASURES[measure_code]
        measure = DimMeasure(
            measure_code=measure_code,
            measure_name=metadata["measure_name"],
            category=metadata["category"],
            unit=metadata["unit"],
        )
        db.session.add(measure)
        db.session.flush()
    return measure


def load_measure_observations(measure_code: str, measure_key: int) -> list[Observation]:
    source = TRAJECTORY_MEASURES[measure_code]["source"]
    observations: list[Observation] = []

    if source == "snapshot":
        rows = (
            db.session.query(
                FactPersonCheckupSnapshot.person_key,
                FactPersonCheckupSnapshot.date_key,
                FactPersonCheckupSnapshot.bmi,
                DimDate.checkup_date,
                DimDate.checkup_year,
            )
            .join(DimDate, FactPersonCheckupSnapshot.date_key == DimDate.date_key)
            .filter(FactPersonCheckupSnapshot.bmi.isnot(None))
            .order_by(
                FactPersonCheckupSnapshot.person_key.asc(),
                DimDate.checkup_date.asc(),
                FactPersonCheckupSnapshot.snapshot_key.asc(),
            )
            .all()
        )
        for row in rows:
            observations.append(
                Observation(
                    person_key=row.person_key,
                    measure_key=measure_key,
                    date_key=row.date_key,
                    checkup_date=row.checkup_date,
                    checkup_year=row.checkup_year,
                    value=float(row.bmi),
                )
            )
        return observations

    rows = (
        db.session.query(
            FactCheckupMeasurement.person_key,
            FactCheckupMeasurement.date_key,
            FactCheckupMeasurement.value_numeric,
            DimDate.checkup_date,
            DimDate.checkup_year,
        )
        .join(DimDate, FactCheckupMeasurement.date_key == DimDate.date_key)
        .join(DimMeasure, FactCheckupMeasurement.measure_key == DimMeasure.measure_key)
        .filter(
            DimMeasure.measure_code == measure_code,
            FactCheckupMeasurement.value_numeric.isnot(None),
        )
        .order_by(
            FactCheckupMeasurement.person_key.asc(),
            DimDate.checkup_date.asc(),
            FactCheckupMeasurement.fact_key.asc(),
        )
        .all()
    )
    for row in rows:
        observations.append(
            Observation(
                person_key=row.person_key,
                measure_key=measure_key,
                date_key=row.date_key,
                checkup_date=row.checkup_date,
                checkup_year=row.checkup_year,
                value=float(row.value_numeric),
            )
        )
    return observations


def keep_latest_observation_per_year(observations: list[Observation]) -> dict[int, list[Observation]]:
    by_person_year: dict[tuple[int, int], Observation] = {}

    for observation in observations:
        key = (observation.person_key, observation.checkup_year)
        current = by_person_year.get(key)
        if current is None or observation.checkup_date > current.checkup_date:
            by_person_year[key] = observation
        elif observation.checkup_date == current.checkup_date and observation.date_key > current.date_key:
            by_person_year[key] = observation

    by_person: dict[int, list[Observation]] = {}
    for observation in by_person_year.values():
        by_person.setdefault(observation.person_key, []).append(observation)

    for person_key in by_person:
        by_person[person_key].sort(key=lambda item: (item.checkup_date, item.date_key))

    return by_person


def calculate_percent_change(previous_value: float, current_value: float) -> float | None:
    if previous_value == 0:
        return None
    return ((current_value - previous_value) / previous_value) * 100.0


def classify_trajectory_direction(measure_code: str, percent_change: float | None) -> str:
    if percent_change is None:
        return "stable"

    if measure_code in HIGHER_IS_BETTER:
        if percent_change > 5:
            return "improving"
        if percent_change < -5:
            return "worsening"
        return "stable"

    if percent_change > 5:
        return "worsening"
    if percent_change < -5:
        return "improving"
    return "stable"


def classify_risk_direction(previous_status: str, current_status: str) -> str:
    if previous_status == "unknown" or current_status == "unknown":
        return "unknown"

    previous_rank = STATUS_RANK[previous_status]
    current_rank = STATUS_RANK[current_status]

    if current_rank > previous_rank:
        return "higher_risk"
    if current_rank < previous_rank:
        return "lower_risk"
    return "unchanged_risk"


def build_interpretation(
    measure_code: str,
    previous_value: float,
    current_value: float,
    previous_status: str,
    current_status: str,
    trajectory_class: str,
    risk_direction: str,
    percent_change: float | None,
) -> str:
    if percent_change is None:
        return (
            f"{measure_code} changed from {previous_value:.2f} to {current_value:.2f}; "
            f"status moved from {previous_status} to {current_status} with {risk_direction}."
        )

    return (
        f"{measure_code} {trajectory_class} from {previous_value:.2f} to {current_value:.2f} "
        f"({percent_change:.2f}%), moving from {previous_status} to {current_status} with {risk_direction}."
    )


def rebuild_trajectories() -> TrajectoryStats:
    stats = TrajectoryStats()
    measures = {
        measure_code: get_or_create_measure(measure_code)
        for measure_code in TRAJECTORY_MEASURES
    }
    stats.measures_prepared = len(measures)

    db.session.execute(delete(FactHealthTrajectory))
    db.session.flush()

    people_with_trajectory: set[int] = set()

    for measure_code, measure in measures.items():
        observations = load_measure_observations(measure_code, measure.measure_key)
        stats.observations_seen += len(observations)

        observations_by_person = keep_latest_observation_per_year(observations)
        for person_key, person_observations in observations_by_person.items():
            if len(person_observations) < 2:
                continue

            for previous, current in zip(person_observations, person_observations[1:]):
                previous_value = previous.value
                current_value = current.value
                delta_value = current_value - previous_value
                percent_change = calculate_percent_change(previous_value, current_value)
                previous_status = classify_status(measure_code, previous_value)
                current_status = classify_status(measure_code, current_value)
                status_transition = f"{previous_status}_to_{current_status}"
                trajectory_class = classify_trajectory_direction(measure_code, percent_change)
                risk_direction = classify_risk_direction(previous_status, current_status)
                interpretation = build_interpretation(
                    measure_code=measure_code,
                    previous_value=previous_value,
                    current_value=current_value,
                    previous_status=previous_status,
                    current_status=current_status,
                    trajectory_class=trajectory_class,
                    risk_direction=risk_direction,
                    percent_change=percent_change,
                )

                db.session.add(
                    FactHealthTrajectory(
                        person_key=person_key,
                        measure_key=measure.measure_key,
                        from_date_key=previous.date_key,
                        to_date_key=current.date_key,
                        previous_value=previous_value,
                        current_value=current_value,
                        delta_value=delta_value,
                        percent_change=percent_change,
                        previous_status=previous_status,
                        current_status=current_status,
                        status_transition=status_transition,
                        trajectory_class=trajectory_class,
                        risk_direction=risk_direction,
                        interpretation=interpretation,
                    )
                )
                stats.trajectories_loaded += 1
                people_with_trajectory.add(person_key)

    stats.persons_loaded = len(people_with_trajectory)
    db.session.commit()
    return stats


def print_report(stats: TrajectoryStats) -> None:
    print("Health Trajectory Build Report")
    print("==============================")
    print(f"Measures prepared: {stats.measures_prepared}")
    print(f"Observations seen: {stats.observations_seen}")
    print(f"Persons loaded: {stats.persons_loaded}")
    print(f"Trajectories loaded: {stats.trajectories_loaded}")


def main() -> int:
    app = create_app()
    with app.app_context():
        db.create_all()
        stats = rebuild_trajectories()
        print_report(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
