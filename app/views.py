from collections import Counter, defaultdict
from datetime import datetime, UTC
import re

from sqlalchemy import desc, func, or_

from flask import Blueprint, current_app, jsonify, render_template, request

from app import db
from app.health_dynamics.exploration_briefs import load_or_create_exploration_brief
from app.health_dynamics.executive_briefs import load_or_create_executive_brief
from app.health_dynamics.explorations import build_exploration_context
from app.health_dynamics.insight_rules import (
    build_verified_insight_payload,
    build_verified_insight_packets,
    select_top_priority_insights,
)
from analytics.llm_client import LocalLLMError, answer_dashboard_question, generate_dashboard_summary
from analytics.narrative_generator import generate_executive_summary
from analytics.risk_rules import classify_status
from analytics.yoy_brief import generate_yoy_brief
from app.models import (
    DimDate,
    DimMeasure,
    DimPerson,
    FactCheckupMeasurement,
    FactDataQuality,
    FactHealthArchetype,
    FactPopulationForecast,
    FactHealthTrajectory,
    FactPersonCheckupSnapshot,
)

main_bp = Blueprint("main", __name__)

STATUS_ORDER = ["normal", "borderline", "abnormal", "unknown"]
RISK_MEASURE_CODES = ["Gluc", "LDL", "TG", "HDL"]
MOVEMENT_TRANSITIONS = [
    "normal_to_normal",
    "normal_to_borderline",
    "normal_to_abnormal",
    "borderline_to_normal",
    "borderline_to_borderline",
    "borderline_to_abnormal",
    "abnormal_to_normal",
    "abnormal_to_borderline",
    "abnormal_to_abnormal",
]


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
    dashboard_context = build_dashboard_context()
    yoy_brief = generate_yoy_brief()
    persisted_executive_brief = load_or_create_executive_brief(
        yoy_brief=yoy_brief,
        base_url=current_app.config.get("LOCAL_LLM_BASE_URL"),
        model=current_app.config.get("LOCAL_LLM_MODEL"),
        api_key=current_app.config.get("LOCAL_LLM_API_KEY"),
        force_regenerate=request.args.get("regenerate_brief") == "1",
    )
    executive_brief = build_executive_brief_summary(yoy_brief)
    return render_template(
        "dashboard.html",
        yoy_brief=yoy_brief,
        executive_brief=executive_brief,
        persisted_executive_brief=persisted_executive_brief,
        **dashboard_context,
    )


@main_bp.route("/dashboard/executive-brief/regenerate", methods=["POST"])
def dashboard_regenerate_executive_brief():
    yoy_brief = generate_yoy_brief()
    persisted_executive_brief = load_or_create_executive_brief(
        yoy_brief=yoy_brief,
        base_url=current_app.config.get("LOCAL_LLM_BASE_URL"),
        model=current_app.config.get("LOCAL_LLM_MODEL"),
        api_key=current_app.config.get("LOCAL_LLM_API_KEY"),
        force_regenerate=True,
    )
    return jsonify(
        {
            "success": persisted_executive_brief.get("available", False),
            "brief": persisted_executive_brief,
        }
    )


@main_bp.route("/dashboard/explore")
def dashboard_explore():
    exploration = build_exploration_context(
        year=request.args.get("year", type=int),
        metric=request.args.get("metric", type=str),
        dimension=request.args.get("dimension", type=str),
        pattern=request.args.get("pattern", type=str),
    )
    exploration_brief = load_or_create_exploration_brief(
        exploration=exploration,
        base_url=current_app.config.get("LOCAL_LLM_BASE_URL"),
        model=current_app.config.get("LOCAL_LLM_MODEL"),
        api_key=current_app.config.get("LOCAL_LLM_API_KEY"),
        force_regenerate=request.args.get("regenerate_brief") == "1",
    )
    return render_template(
        "dashboard_explore.html",
        exploration=exploration,
        exploration_brief=exploration_brief,
    )


@main_bp.route("/dashboard/ai-summary")
def dashboard_ai_summary():
    dashboard_context = build_dashboard_context()
    yoy_brief = generate_yoy_brief()
    verified_insights = select_top_priority_insights(
        build_verified_insight_packets(
            matched_cohort_comparison=yoy_brief.get("matched_cohort_comparison", []),
            trajectory_measure_summary=yoy_brief.get("trajectory_measure_summary", []),
        ),
        limit=3,
    )
    verified_payload = build_verified_insight_payload(
        verified_insights,
        latest_year=yoy_brief.get("latest_year"),
        previous_year=yoy_brief.get("previous_year"),
    )

    try:
        summary, model = generate_dashboard_summary(
            verified_payload,
            base_url=current_app.config.get("LOCAL_LLM_BASE_URL"),
            model=current_app.config.get("LOCAL_LLM_MODEL"),
            api_key=current_app.config.get("LOCAL_LLM_API_KEY"),
        )
    except LocalLLMError:
        current_app.logger.exception("Local LLM dashboard summary generation failed")
        return jsonify(
            {
                "success": False,
                "error": "LLM service unavailable. Please start Ollama and try again.",
            }
        )
    except Exception:
        current_app.logger.exception("Unexpected error during dashboard AI summary generation")
        return jsonify(
            {
                "success": False,
                "error": "LLM service unavailable. Please start Ollama and try again.",
            }
        )

    return jsonify(
        {
            "success": True,
            "summary": summary,
            "model": model,
            "generated_at": datetime.now(UTC).isoformat(),
        }
    )


@main_bp.route("/dashboard/ask-ai", methods=["POST"])
def dashboard_ask_ai():
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()

    if not question:
        return jsonify({"success": False, "error": "Please enter a question."}), 400

    if is_sql_style_question(question):
        return jsonify(
            {
                "success": False,
                "error": "Ask about dashboard findings or population trends. Direct SQL or database-query requests are not supported.",
            }
        ), 400

    dashboard_context = build_dashboard_context()
    aggregated_context = build_dashboard_qa_payload(dashboard_context)

    try:
        answer, model = answer_dashboard_question(
            question,
            aggregated_context,
            base_url=current_app.config.get("LOCAL_LLM_BASE_URL"),
            model=current_app.config.get("LOCAL_LLM_MODEL"),
            api_key=current_app.config.get("LOCAL_LLM_API_KEY"),
        )
    except LocalLLMError:
        current_app.logger.exception("Local LLM dashboard question answering failed")
        return jsonify(
            {
                "success": False,
                "error": "LLM service unavailable. Please start Ollama and try again.",
            }
        )
    except Exception:
        current_app.logger.exception("Unexpected error during dashboard AI question answering")
        return jsonify(
            {
                "success": False,
                "error": "LLM service unavailable. Please start Ollama and try again.",
            }
        )

    return jsonify(
        {
            "success": True,
            "answer": answer,
            "model": model,
            "generated_at": datetime.now(UTC).isoformat(),
        }
    )


@main_bp.route("/explore/query", methods=["POST"])
def explore_query():
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    intent = (payload.get("intent") or "").strip()

    if not question or not intent:
        return jsonify(
            {
                "answer": "Please provide both a question and an intent.",
                "chart_spec": None,
                "table_data": None,
                "evidence": [],
                "suggested_next_questions": [],
            }
        ), 400

    yoy_brief = generate_yoy_brief()
    dashboard_context = build_dashboard_context()
    aggregated_context = build_dashboard_qa_payload(dashboard_context)
    aggregated_context["year_over_year_brief"] = yoy_brief

    rule_based_response = build_rule_based_exploration_response(intent, question, yoy_brief)
    if rule_based_response is None:
        return jsonify(
            {
                "answer": "The dashboard does not contain enough information for that exploration request.",
                "chart_spec": None,
                "table_data": None,
                "evidence": [],
                "suggested_next_questions": [],
            }
        ), 400

    try:
        llm_answer, model = answer_dashboard_question(
            question,
            aggregated_context,
            base_url=current_app.config.get("LOCAL_LLM_BASE_URL"),
            model=current_app.config.get("LOCAL_LLM_MODEL"),
            api_key=current_app.config.get("LOCAL_LLM_API_KEY"),
        )
        current_app.logger.info(
            "Exploration query LLM call succeeded for intent=%s with response_length=%s",
            intent,
            len(llm_answer),
        )
        response_payload = {
            **rule_based_response,
            "answer": llm_answer,
            "model": model,
            "generated_at": datetime.now(UTC).isoformat(),
        }
    except LocalLLMError as exc:
        current_app.logger.warning(
            "Exploration query LLM enhancement failed for intent=%s: %s",
            intent,
            exc,
        )
        response_payload = {
            **rule_based_response,
            "answer": "The system could not generate an AI interpretation. Please review the aggregated dashboard evidence below.",
            "warning": str(exc),
        }
    except Exception:
        current_app.logger.exception("Unexpected exploration query failure")
        response_payload = {
            **rule_based_response,
            "answer": "The system could not generate an AI interpretation. Please review the aggregated dashboard evidence below.",
            "warning": "LLM service unavailable. Showing rule-based exploration result.",
        }

    return jsonify(response_payload)


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


@main_bp.route("/forecast")
def forecast():
    forecast_rows = (
        FactPopulationForecast.query.join(DimMeasure, FactPopulationForecast.measure_key == DimMeasure.measure_key)
        .join(DimDate, FactPopulationForecast.base_date_key == DimDate.date_key)
        .order_by(DimMeasure.measure_name.asc(), DimDate.checkup_date.desc())
        .all()
    )

    if not forecast_rows:
        return render_template("forecast.html", forecast_rows=[], selected_forecast=None, selected_measure_key=None)

    latest_by_measure: dict[int, FactPopulationForecast] = {}
    for row in forecast_rows:
        latest_by_measure.setdefault(row.measure_key, row)

    available_forecasts = list(latest_by_measure.values())
    selected_measure_key = request.args.get("measure_key", type=int)
    if selected_measure_key is None or selected_measure_key not in latest_by_measure:
        selected_forecast = available_forecasts[0]
        selected_measure_key = selected_forecast.measure_key
    else:
        selected_forecast = latest_by_measure[selected_measure_key]

    return render_template(
        "forecast.html",
        forecast_rows=available_forecasts,
        selected_forecast=selected_forecast,
        selected_measure_key=selected_measure_key,
    )


@main_bp.route("/executive-summary")
def executive_summary():
    summary = generate_executive_summary()
    return render_template("executive_summary.html", summary=summary)


@main_bp.route("/data-quality")
def data_quality():
    quality_rows = FactDataQuality.query.join(
        DimMeasure,
        FactDataQuality.measure_key == DimMeasure.measure_key,
        isouter=True,
    ).order_by(FactDataQuality.check_type.asc(), DimMeasure.measure_name.asc()).all()

    if not quality_rows:
        return render_template(
            "data_quality.html",
            summary=None,
            missingness_rows=[],
            invalid_rows=[],
            trajectory_rows=[],
            forecast_rows=[],
        )

    summary = {
        "total_checks": len(quality_rows),
        "ok_count": len([row for row in quality_rows if row.warning_level == "ok"]),
        "caution_count": len([row for row in quality_rows if row.warning_level == "caution"]),
        "high_risk_count": len([row for row in quality_rows if row.warning_level == "high_risk"]),
    }
    missingness_rows = [row for row in quality_rows if row.check_type == "missing_values_by_measure"]
    invalid_rows = [
        row
        for row in quality_rows
        if row.check_type in {
            "invalid_numeric_values",
            "implausible_bmi_values",
            "implausible_blood_pressure_values",
            "missing_person_identifiers",
            "duplicate_person_year_checkups",
        }
    ]
    trajectory_rows = [row for row in quality_rows if row.check_type == "trajectory_readiness"]
    forecast_rows = [row for row in quality_rows if row.check_type == "forecast_readiness"]

    return render_template(
        "data_quality.html",
        summary=summary,
        missingness_rows=missingness_rows,
        invalid_rows=invalid_rows,
        trajectory_rows=trajectory_rows,
        forecast_rows=forecast_rows,
    )


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


def load_latest_snapshots() -> dict[int, FactPersonCheckupSnapshot]:
    rows = (
        FactPersonCheckupSnapshot.query.join(DimDate, FactPersonCheckupSnapshot.date_key == DimDate.date_key)
        .order_by(
            FactPersonCheckupSnapshot.person_key.asc(),
            desc(DimDate.checkup_date),
            desc(FactPersonCheckupSnapshot.snapshot_key),
        )
        .all()
    )
    latest: dict[int, FactPersonCheckupSnapshot] = {}
    for row in rows:
        latest.setdefault(row.person_key, row)
    return latest


def load_latest_measurements(measure_codes: list[str]) -> dict[tuple[int, str], float]:
    rows = (
        db.session.query(
            FactCheckupMeasurement.person_key,
            DimMeasure.measure_code,
            FactCheckupMeasurement.value_numeric,
        )
        .join(DimMeasure, FactCheckupMeasurement.measure_key == DimMeasure.measure_key)
        .join(DimDate, FactCheckupMeasurement.date_key == DimDate.date_key)
        .filter(
            DimMeasure.measure_code.in_(measure_codes),
            FactCheckupMeasurement.value_numeric.isnot(None),
        )
        .order_by(
            FactCheckupMeasurement.person_key.asc(),
            DimMeasure.measure_code.asc(),
            desc(DimDate.checkup_date),
            desc(FactCheckupMeasurement.fact_key),
        )
        .all()
    )
    latest: dict[tuple[int, str], float] = {}
    for person_key, measure_code, value_numeric in rows:
        latest.setdefault((person_key, measure_code), value_numeric)
    return latest


def build_population_profiles(
    latest_snapshots: dict[int, FactPersonCheckupSnapshot],
    latest_measurements: dict[tuple[int, str], float],
) -> list[dict]:
    profiles: list[dict] = []

    for person in DimPerson.query.order_by(DimPerson.full_name.asc()).all():
        snapshot = latest_snapshots.get(person.person_key)
        bmi = snapshot.bmi if snapshot else None
        glucose = latest_measurements.get((person.person_key, "Gluc"))
        ldl = latest_measurements.get((person.person_key, "LDL"))
        tg = latest_measurements.get((person.person_key, "TG"))
        hdl = latest_measurements.get((person.person_key, "HDL"))

        profile = {
            "person": person,
            "age": snapshot.age if snapshot else None,
            "bmi": bmi,
            "glucose": glucose,
            "ldl": ldl,
            "tg": tg,
            "hdl": hdl,
            "bmi_status": classify_status("BMI", bmi),
            "glucose_status": classify_status("Gluc", glucose),
            "ldl_status": classify_status("LDL", ldl),
            "tg_status": classify_status("TG", tg),
            "hdl_status": classify_status("HDL", hdl),
        }
        profile["abnormal_marker_count"] = sum(
            profile[status_key] == "abnormal"
            for status_key in ["bmi_status", "glucose_status", "ldl_status", "tg_status", "hdl_status"]
        )
        profiles.append(profile)

    return profiles


def percentage_for_status(profiles: list[dict], status_key: str, target_status: str) -> float:
    known_profiles = [profile for profile in profiles if profile[status_key] != "unknown"]
    if not known_profiles:
        return 0.0
    numerator = len([profile for profile in known_profiles if profile[status_key] == target_status])
    return (numerator / len(known_profiles)) * 100.0


def count_for_status(profiles: list[dict], status_key: str, target_status: str) -> int:
    return len([profile for profile in profiles if profile[status_key] == target_status])


def percentage_with_minimum_abnormal_markers(profiles: list[dict], minimum_count: int) -> float:
    if not profiles:
        return 0.0
    numerator = len([profile for profile in profiles if profile["abnormal_marker_count"] >= minimum_count])
    return (numerator / len(profiles)) * 100.0


def summarize_ncd_risk_burden(profiles: list[dict]) -> list[dict[str, int | float | str]]:
    buckets = {
        "0 abnormal markers": 0,
        "1 abnormal marker": 0,
        "2 abnormal markers": 0,
        "3 or more abnormal markers": 0,
    }

    for profile in profiles:
        abnormal_count = profile["abnormal_marker_count"]
        if abnormal_count == 0:
            buckets["0 abnormal markers"] += 1
        elif abnormal_count == 1:
            buckets["1 abnormal marker"] += 1
        elif abnormal_count == 2:
            buckets["2 abnormal markers"] += 1
        else:
            buckets["3 or more abnormal markers"] += 1

    total_people = len(profiles) or 1
    return [
        {
            "group": label,
            "count": count,
            "percentage": (count / total_people) * 100.0,
        }
        for label, count in buckets.items()
    ]


def load_trajectory_rows() -> list[dict]:
    rows = (
        db.session.query(
            FactHealthTrajectory.person_key,
            FactHealthTrajectory.status_transition,
            FactHealthTrajectory.trajectory_class,
            FactHealthTrajectory.risk_direction,
            DimPerson.department,
            DimMeasure.measure_code,
            DimMeasure.measure_name,
        )
        .join(DimPerson, FactHealthTrajectory.person_key == DimPerson.person_key)
        .join(DimMeasure, FactHealthTrajectory.measure_key == DimMeasure.measure_key)
        .all()
    )
    return [
        {
            "person_key": row.person_key,
            "status_transition": row.status_transition,
            "trajectory_class": row.trajectory_class,
            "risk_direction": row.risk_direction,
            "department": row.department or "Unassigned",
            "measure_code": row.measure_code,
            "measure_name": row.measure_name,
        }
        for row in rows
    ]


def summarize_population_movement(rows: list[dict]) -> list[dict[str, float | int | str]]:
    counter = Counter(row["status_transition"] for row in rows)
    total = len(rows) or 1
    return [
        {
            "transition": transition,
            "count": counter.get(transition, 0),
            "percentage": (counter.get(transition, 0) / total) * 100.0,
        }
        for transition in MOVEMENT_TRANSITIONS
    ]


def interpret_movement_patterns(movement_summary: list[dict[str, float | int | str]]) -> list[str]:
    counts = {row["transition"]: row["count"] for row in movement_summary}
    total = sum(int(row["count"]) for row in movement_summary) or 1
    messages: list[str] = []

    deterioration_count = int(counts.get("normal_to_borderline", 0)) + int(counts.get("normal_to_abnormal", 0))
    recovery_count = int(counts.get("abnormal_to_normal", 0)) + int(counts.get("abnormal_to_borderline", 0))
    persistent_count = int(counts.get("abnormal_to_abnormal", 0))

    if deterioration_count / total >= 0.15:
        messages.append("Early deterioration signal: people are moving from normal to higher-risk states.")
    if recovery_count / total >= 0.15:
        messages.append("Recovery signal: some high-risk individuals are improving.")
    if persistent_count / total >= 0.15:
        messages.append("Persistent risk signal: abnormal results remain abnormal across years.")

    if not messages:
        messages.append("Movement is mixed across the population; no single transition pattern is currently dominant.")

    return messages


def summarize_department_movement(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["department"]].append(row)

    summary: list[dict] = []
    for department, department_rows in grouped.items():
        total = len(department_rows) or 1
        person_count = len({row["person_key"] for row in department_rows})
        summary.append(
            {
                "department": department,
                "person_count": person_count,
                "improving_pct": percentage_from_rows(department_rows, "trajectory_class", "improving"),
                "stable_pct": percentage_from_rows(department_rows, "trajectory_class", "stable"),
                "worsening_pct": percentage_from_rows(department_rows, "trajectory_class", "worsening"),
                "higher_risk_pct": percentage_from_rows(department_rows, "risk_direction", "higher_risk"),
                "remaining_abnormal_pct": (
                    len([row for row in department_rows if row["status_transition"] == "abnormal_to_abnormal"]) / total
                ) * 100.0,
            }
        )

    return sorted(summary, key=lambda row: (-row["worsening_pct"], -row["higher_risk_pct"], row["department"].lower()))


def summarize_measure_movement(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["measure_name"]].append(row)

    summary: list[dict] = []
    for measure_name, measure_rows in grouped.items():
        transitions = Counter(row["status_transition"] for row in measure_rows)
        summary.append(
            {
                "measure_name": measure_name,
                "normal_to_borderline": transitions.get("normal_to_borderline", 0),
                "normal_to_abnormal": transitions.get("normal_to_abnormal", 0),
                "borderline_to_abnormal": transitions.get("borderline_to_abnormal", 0),
                "abnormal_to_abnormal": transitions.get("abnormal_to_abnormal", 0),
                "abnormal_to_normal": transitions.get("abnormal_to_normal", 0),
            }
        )

    return sorted(summary, key=lambda row: row["measure_name"].lower())


def rank_priority_departments(department_summary: list[dict]) -> list[dict]:
    return sorted(
        department_summary,
        key=lambda row: (-row["worsening_pct"], -row["higher_risk_pct"], -row["remaining_abnormal_pct"], row["department"].lower()),
    )


def percentage_from_rows(rows: list[dict], field_name: str, target_value: str) -> float:
    if not rows:
        return 0.0
    numerator = len([row for row in rows if row[field_name] == target_value])
    return (numerator / len(rows)) * 100.0


def coalesce_numeric(value: float | None) -> float:
    return value if value is not None else -1.0


def build_dashboard_context() -> dict:
    totals = {
        "persons": DimPerson.query.count(),
        "snapshots": FactPersonCheckupSnapshot.query.count(),
        "measurements": FactCheckupMeasurement.query.count(),
        "trajectories": FactHealthTrajectory.query.count(),
    }
    latest_snapshots = load_latest_snapshots()
    latest_measurements = load_latest_measurements(RISK_MEASURE_CODES)
    population_profiles = build_population_profiles(latest_snapshots, latest_measurements)

    key_risk_signals = [
        {
            "label": "Abnormal Glucose",
            "percentage": percentage_for_status(population_profiles, "glucose_status", "abnormal"),
            "numerator": count_for_status(population_profiles, "glucose_status", "abnormal"),
        },
        {
            "label": "Abnormal BMI",
            "percentage": percentage_for_status(population_profiles, "bmi_status", "abnormal"),
            "numerator": count_for_status(population_profiles, "bmi_status", "abnormal"),
        },
        {
            "label": "Abnormal LDL",
            "percentage": percentage_for_status(population_profiles, "ldl_status", "abnormal"),
            "numerator": count_for_status(population_profiles, "ldl_status", "abnormal"),
        },
        {
            "label": "Abnormal Triglyceride",
            "percentage": percentage_for_status(population_profiles, "tg_status", "abnormal"),
            "numerator": count_for_status(population_profiles, "tg_status", "abnormal"),
        },
        {
            "label": "Multiple Abnormal Markers",
            "percentage": percentage_with_minimum_abnormal_markers(population_profiles, 2),
            "numerator": len([profile for profile in population_profiles if profile["abnormal_marker_count"] >= 2]),
        },
    ]

    ncd_risk_burden = summarize_ncd_risk_burden(population_profiles)
    high_risk_persons = sorted(
        population_profiles,
        key=lambda profile: (
            -profile["abnormal_marker_count"],
            -coalesce_numeric(profile["bmi"]),
            profile["person"].cms_code.lower(),
        ),
    )[:20]
    has_trajectory_data = totals["trajectories"] > 0

    trajectory_rows = load_trajectory_rows() if has_trajectory_data else []
    movement_summary = summarize_population_movement(trajectory_rows) if has_trajectory_data else []
    movement_interpretations = interpret_movement_patterns(movement_summary) if has_trajectory_data else []
    department_movement_summary = summarize_department_movement(trajectory_rows) if has_trajectory_data else []
    measure_movement_summary = summarize_measure_movement(trajectory_rows) if has_trajectory_data else []
    priority_intervention_list = rank_priority_departments(department_movement_summary) if has_trajectory_data else []
    archetype_summary = build_archetype_qa_summary()
    forecast_summary = build_forecast_qa_summary()
    data_quality_summary = build_data_quality_qa_summary()

    return {
        "totals": totals,
        "key_risk_signals": key_risk_signals,
        "ncd_risk_burden": ncd_risk_burden,
        "high_risk_persons": high_risk_persons,
        "has_trajectory_data": has_trajectory_data,
        "movement_summary": movement_summary,
        "movement_interpretations": movement_interpretations,
        "department_movement_summary": department_movement_summary,
        "measure_movement_summary": measure_movement_summary,
        "priority_intervention_list": priority_intervention_list,
        "archetype_summary": archetype_summary,
        "forecast_summary": forecast_summary,
        "data_quality_summary": data_quality_summary,
    }


def build_executive_brief_summary(yoy_brief: dict) -> dict:
    if not yoy_brief.get("available"):
        return {
            "available": False,
            "overall_assessment": "Year-over-year population movement cannot be assessed yet.",
            "main_findings": [],
            "positive_signals": [],
            "priority_areas": [],
            "recommended_next_exploration": [],
        }

    matched_rows = yoy_brief["matched_cohort_comparison"]
    trajectory = yoy_brief["trajectory_movement"]
    top_increase = next((row for row in matched_rows if row["delta_percentage_points_matched"] > 0.1), None)
    top_decrease = next((row for row in matched_rows if row["delta_percentage_points_matched"] < -0.1), None)
    worsening_gap = trajectory["worsening_count"] - trajectory["improving_count"]
    total_movement = (
        trajectory["improving_count"] +
        trajectory["stable_count"] +
        trajectory["worsening_count"]
    ) or 1
    persistent_ratio = trajectory["persistent_abnormal_count"] / total_movement

    if worsening_gap > 150 or (top_increase and top_increase["delta_percentage_points_matched"] >= 3 and persistent_ratio >= 0.07):
        overall_assessment = "Population health deteriorated significantly."
    elif worsening_gap > 0 or (top_increase and top_increase["delta_percentage_points_matched"] >= 1):
        overall_assessment = "Population health deteriorated slightly."
    elif trajectory["improving_count"] > trajectory["worsening_count"] and top_decrease and abs(top_decrease["delta_percentage_points_matched"]) >= 1:
        overall_assessment = "Population health improved."
    else:
        overall_assessment = "Population health remained stable."

    main_findings: list[str] = []
    if top_increase:
        main_findings.append(
            f"{top_increase['measure']} increased by {top_increase['delta_percentage_points_matched']:.2f} percentage points in the matched population."
        )
    if top_decrease:
        main_findings.append(
            f"{top_decrease['measure']} decreased by {abs(top_decrease['delta_percentage_points_matched']):.2f} percentage points in the matched population."
        )
    if trajectory["persistent_abnormal_count"] > 0:
        main_findings.append(
            f"{trajectory['persistent_abnormal_count']} matched trajectories remained abnormal across both years."
        )

    priority_areas = list(yoy_brief["concern_signals"][:3])
    if yoy_brief["department_attention"]:
        top_department = yoy_brief["department_attention"][0]
        priority_areas.append(
            f"{top_department['department']} requires attention for worsening and higher-risk movement."
        )

    return {
        "available": True,
        "overall_assessment": overall_assessment,
        "main_findings": main_findings[:3],
        "positive_signals": yoy_brief["positive_signals"][:3],
        "priority_areas": priority_areas[:4],
        "recommended_next_exploration": yoy_brief["suggested_explorations"][:4],
    }


def build_dashboard_llm_payload(dashboard_context: dict) -> dict:
    payload = {
        "totals": dashboard_context["totals"],
        "key_risk_signals": [
            {
                "label": item["label"],
                "percentage": round(item["percentage"], 2),
                "count": item["numerator"],
            }
            for item in dashboard_context["key_risk_signals"]
        ],
        "ncd_risk_burden": [
            {
                "group": row["group"],
                "count": row["count"],
                "percentage": round(row["percentage"], 2),
            }
            for row in dashboard_context["ncd_risk_burden"]
        ],
    }

    if dashboard_context["has_trajectory_data"]:
        payload["movement_summary"] = [
            {
                "transition": row["transition"],
                "count": row["count"],
                "percentage": round(row["percentage"], 2),
            }
            for row in dashboard_context["movement_summary"][:9]
        ]
        payload["movement_interpretations"] = dashboard_context["movement_interpretations"]
        payload["priority_intervention_departments"] = [
            {
                "department": row["department"],
                "worsening_pct": round(row["worsening_pct"], 2),
                "higher_risk_pct": round(row["higher_risk_pct"], 2),
                "persistent_abnormal_pct": round(row["remaining_abnormal_pct"], 2),
            }
            for row in dashboard_context["priority_intervention_list"][:5]
        ]
    else:
        payload["movement_summary"] = []
        payload["movement_interpretations"] = ["No trajectory records are available yet."]
        payload["priority_intervention_departments"] = []

    return payload


def build_dashboard_qa_payload(dashboard_context: dict) -> dict:
    return {
        "population_risk_summary": build_dashboard_llm_payload(dashboard_context),
        "department_risk_summary": build_department_risk_summary(),
        "trajectory_movement_summary": build_trajectory_qa_summary(dashboard_context),
        "archetype_distribution": build_archetype_qa_summary(),
        "forecast_summary": build_forecast_qa_summary(),
        "data_quality_warnings": build_data_quality_qa_summary(),
    }


def build_rule_based_exploration_response(intent: str, question: str, yoy_brief: dict) -> dict | None:
    if not yoy_brief.get("available"):
        return {
            "answer": yoy_brief["reason"],
            "chart_spec": None,
            "table_data": None,
            "evidence": [],
            "suggested_next_questions": [],
        }

    handlers = {
        "marker_trends": build_marker_trends_exploration,
        "department_deterioration": build_department_deterioration_exploration,
        "persistent_abnormal": build_persistent_abnormal_exploration,
        "data_quality": build_data_quality_exploration,
    }
    handler = handlers.get(intent)
    if handler is None:
        return None
    return handler(question, yoy_brief)


def build_marker_trends_exploration(question: str, yoy_brief: dict) -> dict:
    glucose_cross = next((row for row in yoy_brief["cross_sectional_comparison"] if row["measure"] == "Gluc"), None)
    glucose_matched = next((row for row in yoy_brief["matched_cohort_comparison"] if row["measure"] == "Gluc"), None)
    if glucose_cross is None or glucose_matched is None:
        answer = "The dashboard does not contain enough information to assess year-over-year glucose deterioration."
        table_data = None
        evidence = []
    else:
        if glucose_matched["delta_percentage_points_matched"] > 0:
            direction_text = "increased"
        elif glucose_matched["delta_percentage_points_matched"] < 0:
            direction_text = "decreased"
        else:
            direction_text = "remained stable"
        answer = (
            f"In the matched glucose cohort, abnormal percentage {direction_text} from "
            f"{glucose_matched['abnormal_percent_previous_matched']:.2f}% in {yoy_brief['previous_year']} to "
            f"{glucose_matched['abnormal_percent_latest_matched']:.2f}% in {yoy_brief['latest_year']}, a change of "
            f"{glucose_matched['delta_percentage_points_matched']:+.2f} percentage points. "
            "The cross-sectional comparison is shown separately and should not be interpreted as matched-person movement."
        )
        table_data = {
            "columns": [
                "Comparison",
                "Measure",
                "Previous Abnormal %",
                "Latest Abnormal %",
                "Delta (percentage points)",
                "Denominator",
            ],
            "rows": [
                [
                    "Cross-sectional",
                    "Gluc",
                    glucose_cross["abnormal_percent_previous"],
                    glucose_cross["abnormal_percent_latest"],
                    glucose_cross["delta_percentage_points"],
                    f"{glucose_cross['denominator_previous']} vs {glucose_cross['denominator_latest']}",
                ],
                [
                    "Matched cohort",
                    "Gluc",
                    glucose_matched["abnormal_percent_previous_matched"],
                    glucose_matched["abnormal_percent_latest_matched"],
                    glucose_matched["delta_percentage_points_matched"],
                    glucose_matched["denominator_matched"],
                ],
            ],
        }
        evidence = [
            f"Cross-sectional glucose abnormal percentage: {glucose_cross['abnormal_percent_previous']:.2f}% to {glucose_cross['abnormal_percent_latest']:.2f}%.",
            f"Matched-cohort glucose abnormal percentage: {glucose_matched['abnormal_percent_previous_matched']:.2f}% to {glucose_matched['abnormal_percent_latest_matched']:.2f}%.",
        ]

    return {
        "answer": answer,
        "chart_spec": None,
        "table_data": table_data,
        "evidence": evidence,
        "suggested_next_questions": [
            "How does glucose compare with BMI deterioration?",
            "Which other markers worsened in the same period?",
        ],
    }


def build_department_deterioration_exploration(question: str, yoy_brief: dict) -> dict:
    departments = yoy_brief["department_attention"]
    if not departments:
        answer = "Department-level year-over-year deterioration cannot be summarized because department data is not populated in the current comparison set."
        table_data = None
        evidence = []
    else:
        top_department = departments[0]
        answer = (
            f"The highest-priority department is {top_department['department']}, with "
            f"{top_department['worsening_percent']:.2f}% worsening trajectories, "
            f"{top_department['higher_risk_transition_percent']:.2f}% higher-risk transitions, and "
            f"{top_department['persistent_abnormal_percent']:.2f}% persistent abnormal status. "
            "This highlights where management attention may be most urgent."
        )
        table_data = {
            "columns": ["Department", "Worsening %", "Higher-Risk Transition %", "Persistent Abnormal %"],
            "rows": [
                [
                    row["department"],
                    row["worsening_percent"],
                    row["higher_risk_transition_percent"],
                    row["persistent_abnormal_percent"],
                ]
                for row in departments[:5]
            ],
        }
        evidence = [
            f"{row['department']}: {row['worsening_percent']:.2f}% worsening trajectories."
            for row in departments[:3]
        ]

    return {
        "answer": answer,
        "chart_spec": None,
        "table_data": table_data,
        "evidence": evidence,
        "suggested_next_questions": [
            "Which markers are driving the worst department pattern?",
            "How large is the year-over-year matched population for these departments?",
        ],
    }


def build_persistent_abnormal_exploration(question: str, yoy_brief: dict) -> dict:
    persistent_count = yoy_brief["trajectory_movement"]["persistent_abnormal_count"]
    ldl_cross = next((row for row in yoy_brief["cross_sectional_comparison"] if row["measure"] == "LDL"), None)
    ldl_matched = next((row for row in yoy_brief["matched_cohort_comparison"] if row["measure"] == "LDL"), None)

    if ldl_cross is None or ldl_matched is None:
        answer = "The dashboard does not contain enough information to review persistent abnormal LDL patterns."
        table_data = None
        evidence = []
    else:
        answer = (
            f"Persistent abnormal status appears in {persistent_count} year-pair trajectory records overall. "
            f"For LDL, the matched cohort abnormal percentage moved from {ldl_matched['abnormal_percent_previous_matched']:.2f}% "
            f"to {ldl_matched['abnormal_percent_latest_matched']:.2f}% year over year. "
            "This matched-cohort percentage should be interpreted separately from trajectory persistence counts."
        )
        table_data = {
            "columns": [
                "Comparison",
                "Measure",
                "Previous Abnormal %",
                "Latest Abnormal %",
                "Delta (percentage points)",
                "Persistent Abnormal Count",
            ],
            "rows": [
                [
                    "Cross-sectional",
                    "LDL",
                    ldl_cross["abnormal_percent_previous"],
                    ldl_cross["abnormal_percent_latest"],
                    ldl_cross["delta_percentage_points"],
                    persistent_count,
                ],
                [
                    "Matched cohort",
                    "LDL",
                    ldl_matched["abnormal_percent_previous_matched"],
                    ldl_matched["abnormal_percent_latest_matched"],
                    ldl_matched["delta_percentage_points_matched"],
                    persistent_count,
                ],
            ],
        }
        evidence = [
            f"Persistent abnormal trajectory count: {persistent_count}.",
            f"Matched-cohort LDL abnormal percentage changed by {ldl_matched['delta_percentage_points_matched']:+.2f} percentage points.",
        ]

    return {
        "answer": answer,
        "chart_spec": None,
        "table_data": table_data,
        "evidence": evidence,
        "suggested_next_questions": [
            "Which departments show the highest persistent abnormal burden?",
            "How does persistent abnormal LDL compare with glucose persistence?",
        ],
    }


def build_data_quality_exploration(question: str, yoy_brief: dict) -> dict:
    caveats = yoy_brief.get("data_caveats", [])
    if not caveats:
        answer = "No material data quality caveats were surfaced in the current year-over-year briefing."
        evidence = []
    else:
        answer = (
            "The year-over-year briefing is affected by data quality caveats that may limit comparability or readiness for downstream analysis. "
            "Review the listed issues before drawing strong conclusions from smaller differences."
        )
        evidence = caveats[:5]

    return {
        "answer": answer,
        "chart_spec": None,
        "table_data": {
            "columns": ["Data Quality Caveat"],
            "rows": [[item] for item in caveats],
        } if caveats else None,
        "evidence": evidence,
        "suggested_next_questions": [
            "Which measures have limited trajectory readiness?",
            "How much matched-population coverage is available year over year?",
        ],
    }


def build_department_risk_summary() -> list[dict]:
    latest_snapshots = load_latest_snapshots()
    latest_measurements = load_latest_measurements(RISK_MEASURE_CODES)
    profiles = build_population_profiles(latest_snapshots, latest_measurements)

    grouped: dict[str, list[dict]] = defaultdict(list)
    for profile in profiles:
        department = (profile["person"].department or "").strip()
        if department:
            grouped[department].append(profile)

    summary: list[dict] = []
    for department, members in grouped.items():
        known_bmi = [profile["bmi"] for profile in members if profile["bmi"] is not None]
        total = len(members) or 1
        summary.append(
            {
                "department": department,
                "person_count": len(members),
                "average_bmi": round(sum(known_bmi) / len(known_bmi), 2) if known_bmi else None,
                "abnormal_glucose_pct": round(
                    (len([profile for profile in members if profile["glucose_status"] == "abnormal"]) / total) * 100.0,
                    2,
                ),
                "abnormal_ldl_pct": round(
                    (len([profile for profile in members if profile["ldl_status"] == "abnormal"]) / total) * 100.0,
                    2,
                ),
                "abnormal_tg_pct": round(
                    (len([profile for profile in members if profile["tg_status"] == "abnormal"]) / total) * 100.0,
                    2,
                ),
                "three_plus_abnormal_pct": round(
                    (len([profile for profile in members if profile["abnormal_marker_count"] >= 3]) / total) * 100.0,
                    2,
                ),
            }
        )

    return sorted(summary, key=lambda row: (-row["three_plus_abnormal_pct"], row["department"].lower()))[:10]


def build_trajectory_qa_summary(dashboard_context: dict) -> dict:
    if not dashboard_context["has_trajectory_data"]:
        return {
            "available": False,
            "message": "No trajectory records are available yet.",
        }

    return {
        "available": True,
        "population_movement": [
            {
                "transition": row["transition"],
                "count": row["count"],
                "percentage": round(row["percentage"], 2),
            }
            for row in dashboard_context["movement_summary"]
        ],
        "movement_interpretations": dashboard_context["movement_interpretations"],
        "department_movement_summary": [
            {
                "department": row["department"],
                "person_count": row["person_count"],
                "improving_pct": round(row["improving_pct"], 2),
                "stable_pct": round(row["stable_pct"], 2),
                "worsening_pct": round(row["worsening_pct"], 2),
                "higher_risk_pct": round(row["higher_risk_pct"], 2),
                "remaining_abnormal_pct": round(row["remaining_abnormal_pct"], 2),
            }
            for row in dashboard_context["department_movement_summary"][:10]
        ],
    }


def build_archetype_qa_summary() -> dict:
    total_archetypes = FactHealthArchetype.query.count()
    if total_archetypes == 0:
        return {"available": False, "message": "No archetype records are available yet."}

    distribution_rows = (
        db.session.query(FactHealthArchetype.archetype_name, func.count(FactHealthArchetype.archetype_key))
        .group_by(FactHealthArchetype.archetype_name)
        .order_by(func.count(FactHealthArchetype.archetype_key).desc())
        .all()
    )
    progressive_rows = (
        db.session.query(DimMeasure.measure_name, func.count(FactHealthArchetype.archetype_key))
        .join(DimMeasure, FactHealthArchetype.measure_key == DimMeasure.measure_key)
        .filter(FactHealthArchetype.archetype_name == "Progressive Deterioration")
        .group_by(DimMeasure.measure_name)
        .order_by(func.count(FactHealthArchetype.archetype_key).desc())
        .all()
    )
    chronic_rows = (
        db.session.query(DimMeasure.measure_name, func.count(FactHealthArchetype.archetype_key))
        .join(DimMeasure, FactHealthArchetype.measure_key == DimMeasure.measure_key)
        .filter(FactHealthArchetype.archetype_name == "Chronic High Risk")
        .group_by(DimMeasure.measure_name)
        .order_by(func.count(FactHealthArchetype.archetype_key).desc())
        .all()
    )

    return {
        "available": True,
        "distribution": [{"archetype": name, "count": count} for name, count in distribution_rows],
        "progressive_deterioration_measures": [{"measure": name, "count": count} for name, count in progressive_rows[:5]],
        "chronic_high_risk_measures": [{"measure": name, "count": count} for name, count in chronic_rows[:5]],
    }


def build_forecast_qa_summary() -> dict:
    forecast_rows = (
        FactPopulationForecast.query.join(DimMeasure, FactPopulationForecast.measure_key == DimMeasure.measure_key)
        .order_by(DimMeasure.measure_name.asc(), FactPopulationForecast.base_date_key.desc())
        .all()
    )
    if not forecast_rows:
        return {"available": False, "message": "No forecast records are available yet."}

    latest_by_measure: dict[int, FactPopulationForecast] = {}
    for row in forecast_rows:
        latest_by_measure.setdefault(row.measure_key, row)

    summary: list[dict] = []
    for row in latest_by_measure.values():
        summary.append(
            {
                "measure": row.measure.measure_name,
                "current_normal_count": row.current_normal_count,
                "current_borderline_count": row.current_borderline_count,
                "current_abnormal_count": row.current_abnormal_count,
                "forecast_normal_count": round(row.forecast_normal_count, 2),
                "forecast_borderline_count": round(row.forecast_borderline_count, 2),
                "forecast_abnormal_count": round(row.forecast_abnormal_count, 2),
                "abnormal_delta": round(row.forecast_abnormal_count - row.current_abnormal_count, 2),
                "interpretation": row.interpretation,
            }
        )

    return {
        "available": True,
        "measures": sorted(summary, key=lambda row: row["measure"].lower()),
    }


def build_data_quality_qa_summary() -> dict:
    quality_rows = FactDataQuality.query.order_by(FactDataQuality.warning_level.desc()).all()
    if not quality_rows:
        return {"available": False, "message": "No data quality checks are available yet."}

    warning_rows = [row for row in quality_rows if row.warning_level in {"caution", "high_risk"}]
    return {
        "available": True,
        "summary": {
            "total_checks": len(quality_rows),
            "warning_checks": len(warning_rows),
            "high_risk_checks": len([row for row in quality_rows if row.warning_level == "high_risk"]),
        },
        "warnings": [
            {
                "check_type": row.check_type,
                "target_table": row.target_table,
                "target_field": row.target_field,
                "measure": row.measure.measure_name if row.measure else None,
                "missing_percent": round(row.missing_percent or 0.0, 2),
                "invalid_percent": round(row.invalid_percent or 0.0, 2),
                "warning_level": row.warning_level,
                "interpretation": row.interpretation,
            }
            for row in warning_rows[:10]
        ],
    }


def is_sql_style_question(question: str) -> bool:
    normalized = question.strip().lower()
    sql_patterns = [
        r"\bselect\b",
        r"\binsert\b",
        r"\bupdate\b",
        r"\bdelete\b",
        r"\bdrop\b",
        r"\balter\b",
        r"\bjoin\b",
        r"\bfrom\b.+\bwhere\b",
        r"\bwrite sql\b",
        r"\bsql query\b",
        r"\bdatabase query\b",
    ]
    return any(re.search(pattern, normalized) for pattern in sql_patterns)
