from collections import Counter, defaultdict

from sqlalchemy import desc, func, or_

from flask import Blueprint, render_template, request

from app import db
from analytics.risk_rules import classify_status
from app.models import (
    DimDate,
    DimMeasure,
    DimPerson,
    FactCheckupMeasurement,
    FactHealthTrajectory,
    FactPersonCheckupSnapshot,
)

main_bp = Blueprint("main", __name__)

STATUS_ORDER = ["normal", "borderline", "abnormal", "unknown"]


@main_bp.route("/")
def home():
    totals = {
        "persons": DimPerson.query.count(),
        "measurements": FactCheckupMeasurement.query.count(),
        "snapshots": FactPersonCheckupSnapshot.query.count(),
    }
    return render_template("home.html", totals=totals)


@main_bp.route("/dashboard")
def dashboard():
    totals = {
        "persons": DimPerson.query.count(),
        "snapshots": FactPersonCheckupSnapshot.query.count(),
        "measurements": FactCheckupMeasurement.query.count(),
        "trajectories": FactHealthTrajectory.query.count(),
    }

    bmi_values = [
        value
        for (value,) in db.session.query(FactPersonCheckupSnapshot.bmi)
        .filter(FactPersonCheckupSnapshot.bmi.isnot(None))
        .all()
    ]
    bmi_distribution = summarize_status_counts("BMI", bmi_values)

    glucose_values = [
        value
        for (value,) in db.session.query(FactCheckupMeasurement.value_numeric)
        .join(DimMeasure, FactCheckupMeasurement.measure_key == DimMeasure.measure_key)
        .filter(
            DimMeasure.measure_code == "Gluc",
            FactCheckupMeasurement.value_numeric.isnot(None),
        )
        .all()
    ]
    glucose_distribution = summarize_status_counts("Gluc", glucose_values)

    lipid_rows = (
        db.session.query(DimMeasure.measure_code, FactCheckupMeasurement.value_numeric)
        .join(DimMeasure, FactCheckupMeasurement.measure_key == DimMeasure.measure_key)
        .filter(
            DimMeasure.measure_code.in_(["Chol", "TG", "HDL", "LDL"]),
            FactCheckupMeasurement.value_numeric.isnot(None),
        )
        .all()
    )
    lipid_distribution = summarize_measure_status_counts(lipid_rows)

    trajectory_class_distribution = (
        db.session.query(
            FactHealthTrajectory.trajectory_class,
            func.count(FactHealthTrajectory.trajectory_key),
        )
        .group_by(FactHealthTrajectory.trajectory_class)
        .order_by(func.count(FactHealthTrajectory.trajectory_key).desc())
        .all()
    )
    status_transition_distribution = (
        db.session.query(
            FactHealthTrajectory.status_transition,
            func.count(FactHealthTrajectory.trajectory_key),
        )
        .group_by(FactHealthTrajectory.status_transition)
        .order_by(func.count(FactHealthTrajectory.trajectory_key).desc())
        .all()
    )
    has_trajectory_data = totals["trajectories"] > 0

    return render_template(
        "dashboard.html",
        totals=totals,
        bmi_distribution=bmi_distribution,
        glucose_distribution=glucose_distribution,
        lipid_distribution=lipid_distribution,
        trajectory_class_distribution=trajectory_class_distribution,
        status_transition_distribution=status_transition_distribution,
        has_trajectory_data=has_trajectory_data,
    )


@main_bp.route("/persons")
def persons():
    search_query = request.args.get("q", "").strip()
    people_query = DimPerson.query

    if search_query:
        like_pattern = f"%{search_query}%"
        people_query = people_query.filter(
            or_(
                DimPerson.full_name.ilike(like_pattern),
                DimPerson.employee_id.ilike(like_pattern),
                DimPerson.cms_code.ilike(like_pattern),
            )
        )

    people = people_query.order_by(DimPerson.full_name.asc()).all()
    return render_template("persons.html", people=people, search_query=search_query)


@main_bp.route("/person/<int:person_id>")
def person_detail(person_id: int):
    person = DimPerson.query.get_or_404(person_id)

    latest_snapshot = (
        FactPersonCheckupSnapshot.query.join(DimDate, FactPersonCheckupSnapshot.date_key == DimDate.date_key)
        .filter(FactPersonCheckupSnapshot.person_key == person.person_key)
        .order_by(desc(DimDate.checkup_date), desc(FactPersonCheckupSnapshot.snapshot_key))
        .first()
    )

    measurements = (
        FactCheckupMeasurement.query.join(DimDate, FactCheckupMeasurement.date_key == DimDate.date_key)
        .filter(FactCheckupMeasurement.person_key == person.person_key)
        .order_by(
            desc(DimDate.checkup_date),
            FactCheckupMeasurement.measure_key.asc(),
            FactCheckupMeasurement.fact_key.asc(),
        )
        .all()
    )

    return render_template(
        "person_detail.html",
        person=person,
        latest_snapshot=latest_snapshot,
        measurements=measurements,
    )


def summarize_status_counts(measure_code: str, values: list[float]) -> list[tuple[str, int]]:
    counter = Counter(classify_status(measure_code, value) for value in values)
    return [(status, counter.get(status, 0)) for status in STATUS_ORDER]


def summarize_measure_status_counts(rows: list[tuple[str, float]]) -> list[dict[str, int | str]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for measure_code, value in rows:
        counters[measure_code][classify_status(measure_code, value)] += 1

    measure_order = ["Chol", "TG", "HDL", "LDL"]
    summary: list[dict[str, int | str]] = []
    for measure_code in measure_order:
        counter = counters.get(measure_code, Counter())
        summary.append(
            {
                "measure_code": measure_code,
                "normal": counter.get("normal", 0),
                "borderline": counter.get("borderline", 0),
                "abnormal": counter.get("abnormal", 0),
                "unknown": counter.get("unknown", 0),
            }
        )
    return summary
