from sqlalchemy import desc, or_

from flask import Blueprint, render_template, request

from app.models import DimDate, DimPerson, FactCheckupMeasurement, FactPersonCheckupSnapshot

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def home():
    totals = {
        "persons": DimPerson.query.count(),
        "measurements": FactCheckupMeasurement.query.count(),
        "snapshots": FactPersonCheckupSnapshot.query.count(),
    }
    return render_template("home.html", totals=totals)


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
