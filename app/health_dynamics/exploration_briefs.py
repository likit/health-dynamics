from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from app import db
from app.health_dynamics.brief_diagnostics import classify_brief_diagnostic
from app.health_dynamics.explorations import humanize_pattern
from app.health_dynamics.insight_rules import METRIC_LABELS, humanize_drilldown_labels
from app.models import ExplorationBrief
from analytics.llm_client import LocalLLMError, generate_exploration_brief


UNAVAILABLE_EXPLORATION_BRIEF = {
    "available": False,
    "key_finding": "The dashboard does not contain enough information to generate an Exploration Brief for this view.",
    "interpretation": "",
    "suggested_next_investigation": "",
    "generation_status": "failed",
    "validation_status": "failed",
    "model_name": None,
    "data_signature": None,
}


def ensure_exploration_brief_table() -> None:
    db.metadata.create_all(bind=db.engine, tables=[ExplorationBrief.__table__])
    existing_columns = {
        row[1]
        for row in db.session.execute(text("PRAGMA table_info(exploration_brief)"))
    }
    if "diagnostic_code" not in existing_columns:
        db.session.execute(text("ALTER TABLE exploration_brief ADD COLUMN diagnostic_code VARCHAR(64)"))
    if "diagnostic_message" not in existing_columns:
        db.session.execute(text("ALTER TABLE exploration_brief ADD COLUMN diagnostic_message TEXT"))
    db.session.commit()


def generate_exploration_insight_packet(exploration: dict) -> dict | None:
    if not exploration.get("available"):
        return None

    results = exploration.get("results", [])
    if not results:
        return None

    highest_persistent = max(results, key=lambda row: (row["persistent_abnormal_pct"], row["paired_n"], row["group"]))
    highest_deterioration = max(results, key=lambda row: (row["deteriorating_pct"], row["paired_n"], row["group"]))
    highest_improvement = max(results, key=lambda row: (row["improving_pct"], row["paired_n"], row["group"]))

    return {
        "metric": exploration["metric"],
        "metric_label": exploration["metric_label"],
        "dimension": exploration["dimension"],
        "dimension_label": exploration["dimension_label"],
        "pattern": exploration.get("pattern"),
        "pattern_label": humanize_pattern(exploration.get("pattern")),
        "highest_persistent_group": highest_persistent["group"],
        "highest_persistent_pct": highest_persistent["persistent_abnormal_pct"],
        "highest_deterioration_group": highest_deterioration["group"],
        "highest_deterioration_pct": highest_deterioration["deteriorating_pct"],
        "highest_improvement_group": highest_improvement["group"],
        "highest_improvement_pct": highest_improvement["improving_pct"],
        "total_groups": len(results),
        "total_paired_n": sum(row["paired_n"] for row in results),
        "year": exploration["year"],
        "previous_year": exploration["previous_year"],
    }


def calculate_exploration_data_signature(packet: dict) -> str:
    serialized = json.dumps(packet, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def parse_exploration_brief_sections(summary_text: str) -> dict:
    sections = {
        "key_finding": "",
        "interpretation": "",
        "suggested_next_investigation": "",
    }
    current_key: str | None = None
    buffers: dict[str, list[str]] = {
        "key_finding": [],
        "interpretation": [],
        "suggested_next_investigation": [],
    }

    for raw_line in summary_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Key Finding:"):
            current_key = "key_finding"
            buffers[current_key].append(line.removeprefix("Key Finding:").strip())
            continue
        if line.startswith("Interpretation:"):
            current_key = "interpretation"
            buffers[current_key].append(line.removeprefix("Interpretation:").strip())
            continue
        if line.startswith("Suggested Next Investigation:"):
            current_key = "suggested_next_investigation"
            buffers[current_key].append(line.removeprefix("Suggested Next Investigation:").strip())
            continue
        if current_key is not None:
            buffers[current_key].append(line.removeprefix("-").strip())

    for key in sections:
        sections[key] = " ".join(buffers[key]).strip()
    return sections


def generate_exploration_fallback_brief(packet: dict) -> str:
    metric_label = packet["metric_label"]
    dimension_label = packet["dimension_label"]
    return (
        f"Key Finding: The highest level of persistent {metric_label} concern was observed in {packet['highest_persistent_group']}.\n\n"
        f"Interpretation: {metric_label}-related health risk appears concentrated in this {dimension_label.lower()} group. "
        f"The strongest deterioration was observed in {packet['highest_deterioration_group']}, while the strongest improvement was observed in {packet['highest_improvement_group']}. "
        f"This suggests the pattern is not limited to a single isolated group.\n\n"
        f"Suggested Next Investigation: Review {dimension_label}-level {metric_label} patterns for the groups that stand out most, especially {packet['highest_persistent_group']} and {packet['highest_deterioration_group']}."
    )


def serialize_exploration_brief_record(record: ExplorationBrief) -> dict:
    generation_status, validation_status = normalize_exploration_brief_statuses(
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
    return {
        "available": True,
        "key_finding": record.key_finding or "",
        "interpretation": record.interpretation or "",
        "suggested_next_investigation": record.suggested_next_investigation or "",
        "generation_status": generation_status,
        "validation_status": validation_status,
        "model_name": record.model_name,
        "diagnostic_code": diagnostic_code,
        "diagnostic_message": diagnostic_message,
        "data_signature": record.data_signature,
    }


def normalize_exploration_brief_statuses(
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


def load_or_create_exploration_brief(
    *,
    exploration: dict,
    base_url: str | None,
    model: str | None,
    api_key: str | None,
    force_regenerate: bool = False,
) -> dict:
    ensure_exploration_brief_table()

    packet = generate_exploration_insight_packet(exploration)
    if packet is None:
        return dict(UNAVAILABLE_EXPLORATION_BRIEF)

    data_signature = calculate_exploration_data_signature(packet)

    diagnostic_code = None
    diagnostic_message = None

    if not force_regenerate:
        existing = ExplorationBrief.query.filter_by(
            year=packet["year"],
            metric=packet["metric"],
            dimension=packet["dimension"],
            pattern=packet.get("pattern"),
            data_signature=data_signature,
        ).first()
        if existing is not None:
            return serialize_exploration_brief_record(existing)

    try:
        summary_text, model_name = generate_exploration_brief(
            packet,
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
        summary_text = generate_exploration_fallback_brief(packet)
        model_name = "deterministic-fallback"
        generation_status = "fallback"
        validation_status = "passed"
        diagnostic_code, diagnostic_message = classify_brief_diagnostic(
            generation_status=generation_status,
            model_name=model_name,
            error_message=str(exc),
        )
    except Exception as exc:
        summary_text = generate_exploration_fallback_brief(packet)
        model_name = "deterministic-fallback"
        generation_status = "fallback"
        validation_status = "passed"
        diagnostic_code, diagnostic_message = classify_brief_diagnostic(
            generation_status=generation_status,
            model_name=model_name,
            error_message=str(exc),
        )

    sections = parse_exploration_brief_sections(summary_text)
    if not sections["key_finding"]:
        sections = parse_exploration_brief_sections(generate_exploration_fallback_brief(packet))
        generation_status = "fallback"
        validation_status = "passed"
        model_name = "deterministic-fallback"
        diagnostic_code, diagnostic_message = classify_brief_diagnostic(
            generation_status=generation_status,
            model_name=model_name,
            parse_failed=True,
        )

    generation_status, validation_status = normalize_exploration_brief_statuses(
        model_name=model_name,
        generation_status=generation_status,
        validation_status=validation_status,
    )

    record = ExplorationBrief.query.filter_by(
        year=packet["year"],
        metric=packet["metric"],
        dimension=packet["dimension"],
        pattern=packet.get("pattern"),
    ).first()
    if record is None:
        record = ExplorationBrief(
            year=packet["year"],
            metric=packet["metric"],
            dimension=packet["dimension"],
            pattern=packet.get("pattern"),
        )
        db.session.add(record)

    record.key_finding = sections["key_finding"]
    record.interpretation = sections["interpretation"]
    record.suggested_next_investigation = sections["suggested_next_investigation"]
    record.insight_packet_json = json.dumps(packet, sort_keys=True)
    record.model_name = model_name
    record.generation_status = generation_status
    record.validation_status = validation_status
    record.diagnostic_code = diagnostic_code
    record.diagnostic_message = diagnostic_message
    record.data_signature = data_signature
    db.session.commit()

    return serialize_exploration_brief_record(record)
