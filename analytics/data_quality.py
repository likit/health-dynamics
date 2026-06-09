from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app import create_app, db
from app.models import (
    DimDate,
    DimMeasure,
    DimPerson,
    FactCheckupMeasurement,
    FactDataQuality,
    FactHealthArchetype,
    FactHealthTrajectory,
    FactPersonCheckupSnapshot,
    FactPopulationForecast,
)

TRAJECTORY_READY_THRESHOLD = 20
FORECAST_READY_THRESHOLD = 15


@dataclass
class QualityStats:
    checks_loaded: int = 0
    ok_count: int = 0
    caution_count: int = 0
    high_risk_count: int = 0


def safe_percent(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((count / total) * 100.0, 2)


def level_from_percent(percent: float) -> str:
    if percent >= 20:
        return "high_risk"
    if percent >= 5:
        return "caution"
    return "ok"


def level_from_readiness(total_records: int, required_threshold: int, missing_states: int = 0) -> str:
    if total_records < required_threshold or missing_states >= 2:
        return "high_risk"
    if missing_states >= 1 or total_records < (required_threshold * 2):
        return "caution"
    return "ok"


def add_quality_row(
    stats: QualityStats,
    *,
    check_type: str,
    target_table: str,
    target_field: str,
    measure_key: int | None,
    total_records: int,
    missing_count: int,
    invalid_count: int,
    warning_level: str,
    interpretation: str,
) -> None:
    row = FactDataQuality(
        check_type=check_type,
        target_table=target_table,
        target_field=target_field,
        measure_key=measure_key,
        total_records=total_records,
        missing_count=missing_count,
        missing_percent=safe_percent(missing_count, total_records),
        invalid_count=invalid_count,
        invalid_percent=safe_percent(invalid_count, total_records),
        warning_level=warning_level,
        interpretation=interpretation,
    )
    db.session.add(row)
    stats.checks_loaded += 1
    if warning_level == "ok":
        stats.ok_count += 1
    elif warning_level == "caution":
        stats.caution_count += 1
    else:
        stats.high_risk_count += 1


def rebuild_data_quality() -> QualityStats:
    stats = QualityStats()
    db.session.execute(delete(FactDataQuality))
    db.session.flush()

    measure_rows = DimMeasure.query.order_by(DimMeasure.measure_name.asc()).all()
    for measure in measure_rows:
        total_measure_records = (
            db.session.query(func.count(FactCheckupMeasurement.fact_key))
            .filter(FactCheckupMeasurement.measure_key == measure.measure_key)
            .scalar()
            or 0
        )
        missing_measure_count = (
            db.session.query(func.count(FactCheckupMeasurement.fact_key))
            .filter(
                FactCheckupMeasurement.measure_key == measure.measure_key,
                FactCheckupMeasurement.value_numeric.is_(None),
            )
            .scalar()
            or 0
        )
        missing_level = level_from_percent(safe_percent(missing_measure_count, total_measure_records))
        add_quality_row(
            stats,
            check_type="missing_values_by_measure",
            target_table="fact_checkup_measurement",
            target_field="value_numeric",
            measure_key=measure.measure_key,
            total_records=total_measure_records,
            missing_count=missing_measure_count,
            invalid_count=0,
            warning_level=missing_level,
            interpretation=(
                f"{measure.measure_name} has {missing_measure_count} missing numeric values out of "
                f"{total_measure_records} measurement records."
            ),
        )

        invalid_measure_count = (
            db.session.query(func.count(FactCheckupMeasurement.fact_key))
            .filter(
                FactCheckupMeasurement.measure_key == measure.measure_key,
                FactCheckupMeasurement.value_numeric.is_not(None),
                FactCheckupMeasurement.value_numeric < 0,
            )
            .scalar()
            or 0
        )
        invalid_level = level_from_percent(safe_percent(invalid_measure_count, total_measure_records))
        add_quality_row(
            stats,
            check_type="invalid_numeric_values",
            target_table="fact_checkup_measurement",
            target_field="value_numeric",
            measure_key=measure.measure_key,
            total_records=total_measure_records,
            missing_count=0,
            invalid_count=invalid_measure_count,
            warning_level=invalid_level,
            interpretation=(
                f"{measure.measure_name} has {invalid_measure_count} negative numeric values that should be reviewed."
            ),
        )

        trajectory_total = (
            db.session.query(func.count(FactHealthTrajectory.trajectory_key))
            .filter(FactHealthTrajectory.measure_key == measure.measure_key)
            .scalar()
            or 0
        )
        trajectory_level = level_from_readiness(trajectory_total, TRAJECTORY_READY_THRESHOLD)
        add_quality_row(
            stats,
            check_type="trajectory_readiness",
            target_table="fact_health_trajectory",
            target_field="measure_key",
            measure_key=measure.measure_key,
            total_records=trajectory_total,
            missing_count=0,
            invalid_count=0,
            warning_level=trajectory_level,
            interpretation=(
                f"{measure.measure_name} has {trajectory_total} transition records available for trajectory analysis."
            ),
        )

        state_coverage = {
            state for (state,) in db.session.query(FactHealthTrajectory.previous_status)
            .filter(FactHealthTrajectory.measure_key == measure.measure_key)
            .group_by(FactHealthTrajectory.previous_status)
            .all()
            if state in {"normal", "borderline", "abnormal"}
        }
        missing_states = 3 - len(state_coverage)
        forecast_level = level_from_readiness(trajectory_total, FORECAST_READY_THRESHOLD, missing_states)
        add_quality_row(
            stats,
            check_type="forecast_readiness",
            target_table="fact_population_forecast",
            target_field="status_transition",
            measure_key=measure.measure_key,
            total_records=trajectory_total,
            missing_count=0,
            invalid_count=missing_states,
            warning_level=forecast_level,
            interpretation=(
                f"{measure.measure_name} has {trajectory_total} transition records and is missing "
                f"{missing_states} transition-state rows needed for a robust forecast matrix."
            ),
        )

    bmi_total = (
        db.session.query(func.count(FactPersonCheckupSnapshot.snapshot_key))
        .filter(FactPersonCheckupSnapshot.bmi.is_not(None))
        .scalar()
        or 0
    )
    bmi_invalid = (
        db.session.query(func.count(FactPersonCheckupSnapshot.snapshot_key))
        .filter(
            FactPersonCheckupSnapshot.bmi.is_not(None),
            ((FactPersonCheckupSnapshot.bmi < 10) | (FactPersonCheckupSnapshot.bmi > 60)),
        )
        .scalar()
        or 0
    )
    add_quality_row(
        stats,
        check_type="implausible_bmi_values",
        target_table="fact_person_checkup_snapshot",
        target_field="bmi",
        measure_key=None,
        total_records=bmi_total,
        missing_count=0,
        invalid_count=bmi_invalid,
        warning_level=level_from_percent(safe_percent(bmi_invalid, bmi_total)),
        interpretation=f"{bmi_invalid} BMI values fall outside the plausibility range of 10 to 60.",
    )

    bp_total = (
        db.session.query(func.count(FactPersonCheckupSnapshot.snapshot_key))
        .filter(
            FactPersonCheckupSnapshot.systolic_bp.is_not(None),
            FactPersonCheckupSnapshot.diastolic_bp.is_not(None),
        )
        .scalar()
        or 0
    )
    bp_invalid = (
        db.session.query(func.count(FactPersonCheckupSnapshot.snapshot_key))
        .filter(
            FactPersonCheckupSnapshot.systolic_bp.is_not(None),
            FactPersonCheckupSnapshot.diastolic_bp.is_not(None),
            (
                (FactPersonCheckupSnapshot.systolic_bp < 70)
                | (FactPersonCheckupSnapshot.systolic_bp > 250)
                | (FactPersonCheckupSnapshot.diastolic_bp < 40)
                | (FactPersonCheckupSnapshot.diastolic_bp > 150)
                | (FactPersonCheckupSnapshot.systolic_bp <= FactPersonCheckupSnapshot.diastolic_bp)
            ),
        )
        .scalar()
        or 0
    )
    add_quality_row(
        stats,
        check_type="implausible_blood_pressure_values",
        target_table="fact_person_checkup_snapshot",
        target_field="blood_pressure",
        measure_key=None,
        total_records=bp_total,
        missing_count=0,
        invalid_count=bp_invalid,
        warning_level=level_from_percent(safe_percent(bp_invalid, bp_total)),
        interpretation=(
            f"{bp_invalid} blood pressure records are implausible based on clinical range checks or "
            f"systolic/diastolic ordering."
        ),
    )

    person_total = DimPerson.query.count()
    missing_identifier_count = (
        db.session.query(func.count(DimPerson.person_key))
        .filter(
            (DimPerson.cms_code.is_(None))
            | (DimPerson.cms_code == "")
            | (DimPerson.employee_id.is_(None))
            | (DimPerson.employee_id == "")
        )
        .scalar()
        or 0
    )
    add_quality_row(
        stats,
        check_type="missing_person_identifiers",
        target_table="dim_person",
        target_field="cms_code_employee_id",
        measure_key=None,
        total_records=person_total,
        missing_count=missing_identifier_count,
        invalid_count=0,
        warning_level=level_from_percent(safe_percent(missing_identifier_count, person_total)),
        interpretation=(
            f"{missing_identifier_count} person records are missing either CMS code or employee identifier fields."
        ),
    )

    duplicate_groups = (
        db.session.query(
            FactPersonCheckupSnapshot.person_key,
            DimDate.checkup_year,
            func.count(FactPersonCheckupSnapshot.snapshot_key).label("row_count"),
        )
        .join(DimDate, FactPersonCheckupSnapshot.date_key == DimDate.date_key)
        .group_by(FactPersonCheckupSnapshot.person_key, DimDate.checkup_year)
        .having(func.count(FactPersonCheckupSnapshot.snapshot_key) > 1)
        .all()
    )
    person_year_total = (
        db.session.query(
            FactPersonCheckupSnapshot.person_key,
            DimDate.checkup_year,
        )
        .join(DimDate, FactPersonCheckupSnapshot.date_key == DimDate.date_key)
        .group_by(FactPersonCheckupSnapshot.person_key, DimDate.checkup_year)
        .count()
    )
    add_quality_row(
        stats,
        check_type="duplicate_person_year_checkups",
        target_table="fact_person_checkup_snapshot",
        target_field="person_key_checkup_year",
        measure_key=None,
        total_records=person_year_total,
        missing_count=0,
        invalid_count=len(duplicate_groups),
        warning_level=level_from_percent(safe_percent(len(duplicate_groups), person_year_total)),
        interpretation=(
            f"{len(duplicate_groups)} person-year groups contain duplicate snapshot records and should be reviewed."
        ),
    )

    db.session.commit()
    return stats


def print_report(stats: QualityStats) -> None:
    print("Data Quality Build Report")
    print("=========================")
    print(f"Checks loaded: {stats.checks_loaded}")
    print(f"OK: {stats.ok_count}")
    print(f"Caution: {stats.caution_count}")
    print(f"High risk: {stats.high_risk_count}")


def main() -> int:
    app = create_app()
    with app.app_context():
        db.create_all()
        stats = rebuild_data_quality()
        print_report(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
