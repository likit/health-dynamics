from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import text

from app import db
from app.health_dynamics.brief_diagnostics import classify_brief_diagnostic
from app.models import ExecutiveBrief
from app.health_dynamics.explorations import build_exploration_actions
from app.health_dynamics.insight_rules import (
    build_fallback_executive_brief,
    build_verified_insight_payload,
    build_verified_insight_packets,
    select_top_priority_insights,
)
from analytics.llm_client import LocalLLMError, generate_dashboard_summary


UNAVAILABLE_BRIEF = {
    "available": False,
    "headline": "The dashboard does not contain enough information to generate an Executive Brief for this year.",
    "executive_summary": "",
    "why_it_matters": "",
    "suggested_next_exploration": [],
    "generation_status": "failed",
    "validation_status": "failed",
    "model_name": None,
    "data_signature": None,
}


@dataclass
class ExecutiveBriefResult:
    available: bool
    headline: str
    executive_summary: str
    why_it_matters: str
    suggested_next_exploration: list[str]
    generation_status: str
    validation_status: str
    model_name: str | None
    data_signature: str | None


def ensure_executive_brief_table() -> None:
    db.metadata.create_all(bind=db.engine, tables=[ExecutiveBrief.__table__])
    existing_columns = {
        row[1]
        for row in db.session.execute(text("PRAGMA table_info(executive_brief)"))
    }
    if "diagnostic_code" not in existing_columns:
        db.session.execute(text("ALTER TABLE executive_brief ADD COLUMN diagnostic_code VARCHAR(64)"))
    if "diagnostic_message" not in existing_columns:
        db.session.execute(text("ALTER TABLE executive_brief ADD COLUMN diagnostic_message TEXT"))
    db.session.commit()


def build_dashboard_verified_insights(yoy_brief: dict) -> list[dict]:
    return select_top_priority_insights(
        build_verified_insight_packets(
            matched_cohort_comparison=yoy_brief.get("matched_cohort_comparison", []),
            trajectory_measure_summary=yoy_brief.get("trajectory_measure_summary", []),
        ),
        limit=3,
    )


def calculate_data_signature(*, year: int, brief_type: str, insight_packets: list[dict]) -> str:
    serialized = json.dumps(
        {
            "year": year,
            "brief_type": brief_type,
            "insight_packets": insight_packets,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def parse_brief_sections(summary_text: str) -> dict:
    sections = {
        "headline": "",
        "executive_summary": "",
        "why_it_matters": "",
        "suggested_next_exploration": [],
    }
    current_key: str | None = None
    buffers: dict[str, list[str]] = {
        "headline": [],
        "executive_summary": [],
        "why_it_matters": [],
        "suggested_next_exploration": [],
    }

    for raw_line in summary_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Headline:"):
            current_key = "headline"
            buffers[current_key].append(line.removeprefix("Headline:").strip())
            continue
        if line.startswith("Executive Summary:"):
            current_key = "executive_summary"
            buffers[current_key].append(line.removeprefix("Executive Summary:").strip())
            continue
        if line.startswith("Why It Matters:"):
            current_key = "why_it_matters"
            buffers[current_key].append(line.removeprefix("Why It Matters:").strip())
            continue
        if line.startswith("Suggested Next Exploration:"):
            current_key = "suggested_next_exploration"
            continue
        if current_key == "suggested_next_exploration":
            buffers[current_key].append(line.removeprefix("-").strip())
        elif current_key is not None:
            buffers[current_key].append(line)

    sections["headline"] = " ".join(buffers["headline"]).strip()
    sections["executive_summary"] = " ".join(buffers["executive_summary"]).strip()
    sections["why_it_matters"] = " ".join(buffers["why_it_matters"]).strip()
    sections["suggested_next_exploration"] = [item for item in buffers["suggested_next_exploration"] if item]
    return sections


def serialize_brief_record(record: ExecutiveBrief) -> dict:
    generation_status, validation_status = normalize_executive_brief_statuses(
        model_name=record.model_name,
        generation_status=record.generation_status,
        validation_status=record.validation_status,
    )
    diagnostic_code, diagnostic_message = classify_brief_diagnostic(
        generation_status=generation_status,
        model_name=record.model_name,
        stored_code=record.diagnostic_code,
        stored_message=record.diagnostic_message,
    )
    suggestions = []
    insight_packets = []
    if record.suggested_next_exploration:
        try:
            suggestions = json.loads(record.suggested_next_exploration)
        except json.JSONDecodeError:
            suggestions = []
    if record.insight_packets_json:
        try:
            insight_packets = json.loads(record.insight_packets_json)
        except json.JSONDecodeError:
            insight_packets = []

    return {
        "available": True,
        "headline": record.headline or "",
        "executive_summary": record.executive_summary or "",
        "why_it_matters": record.why_it_matters or "",
        "suggested_next_exploration": suggestions,
        "insight_packets": insight_packets,
        "exploration_actions": build_exploration_actions(insight_packets, record.year),
        "generation_status": generation_status,
        "validation_status": validation_status,
        "model_name": record.model_name,
        "diagnostic_code": diagnostic_code,
        "diagnostic_message": diagnostic_message,
        "data_signature": record.data_signature,
    }


def normalize_executive_brief_statuses(
    *,
    model_name: str | None,
    generation_status: str,
    validation_status: str,
) -> tuple[str, str]:
    is_fallback = generation_status == "fallback" or (
        bool(model_name) and (model_name == "deterministic-fallback" or model_name.endswith("(fallback)"))
    )
    if is_fallback and validation_status == "failed":
        return "fallback", "passed"
    return generation_status, validation_status


def load_or_create_executive_brief(
    *,
    yoy_brief: dict,
    base_url: str | None,
    model: str | None,
    api_key: str | None,
    brief_type: str = "executive_dashboard",
    organization_id: str | None = None,
    force_regenerate: bool = False,
) -> dict:
    ensure_executive_brief_table()

    if not yoy_brief.get("available"):
        return dict(UNAVAILABLE_BRIEF)

    latest_year = yoy_brief["latest_year"]
    insight_packets = build_dashboard_verified_insights(yoy_brief)
    if not insight_packets:
        return dict(UNAVAILABLE_BRIEF)

    verified_payload = build_verified_insight_payload(
        insight_packets,
        latest_year=yoy_brief.get("latest_year"),
        previous_year=yoy_brief.get("previous_year"),
    )
    data_signature = calculate_data_signature(
        year=latest_year,
        brief_type=brief_type,
        insight_packets=insight_packets,
    )

    diagnostic_code = None
    diagnostic_message = None

    if not force_regenerate:
        existing = ExecutiveBrief.query.filter_by(
            organization_id=organization_id,
            year=latest_year,
            brief_type=brief_type,
            data_signature=data_signature,
        ).first()
        if existing is not None:
            return serialize_brief_record(existing)

    try:
        summary_text, model_name = generate_dashboard_summary(
            verified_payload,
            base_url=base_url,
            model=model,
            api_key=api_key,
        )
        generation_status = "fallback" if model_name and model_name.endswith("(fallback)") else "generated"
        validation_status = "passed"
        diagnostic_code, diagnostic_message = classify_brief_diagnostic(
            generation_status=generation_status,
            model_name=model_name,
        )
    except LocalLLMError as exc:
        summary_text = build_fallback_executive_brief(insight_packets)
        model_name = "deterministic-fallback"
        generation_status = "fallback"
        validation_status = "passed"
        diagnostic_code, diagnostic_message = classify_brief_diagnostic(
            generation_status=generation_status,
            model_name=model_name,
            error_message=str(exc),
        )
    except Exception as exc:
        summary_text = build_fallback_executive_brief(insight_packets)
        model_name = "deterministic-fallback"
        generation_status = "fallback"
        validation_status = "passed"
        diagnostic_code, diagnostic_message = classify_brief_diagnostic(
            generation_status=generation_status,
            model_name=model_name,
            error_message=str(exc),
        )

    sections = parse_brief_sections(summary_text)
    if not sections["headline"]:
        sections = parse_brief_sections(build_fallback_executive_brief(insight_packets))
        generation_status = "fallback"
        validation_status = "passed"
        model_name = "deterministic-fallback"
        diagnostic_code, diagnostic_message = classify_brief_diagnostic(
            generation_status=generation_status,
            model_name=model_name,
            parse_failed=True,
        )

    generation_status, validation_status = normalize_executive_brief_statuses(
        model_name=model_name,
        generation_status=generation_status,
        validation_status=validation_status,
    )

    record = ExecutiveBrief.query.filter_by(
        organization_id=organization_id,
        year=latest_year,
        brief_type=brief_type,
    ).first()
    if record is None:
        record = ExecutiveBrief(
            organization_id=organization_id,
            year=latest_year,
            brief_type=brief_type,
        )
        db.session.add(record)

    record.headline = sections["headline"]
    record.executive_summary = sections["executive_summary"]
    record.why_it_matters = sections["why_it_matters"]
    record.suggested_next_exploration = json.dumps(sections["suggested_next_exploration"])
    record.insight_packets_json = json.dumps(insight_packets, sort_keys=True)
    record.model_name = model_name
    record.generation_status = generation_status
    record.validation_status = validation_status
    record.diagnostic_code = diagnostic_code
    record.diagnostic_message = diagnostic_message
    record.data_signature = data_signature
    db.session.commit()

    return serialize_brief_record(record)
