from __future__ import annotations

from typing import Any

# This module acts as a governance layer. It converts already-calculated
# analytics into verified insight packets before the optional AI writing layer
# is allowed to describe them.

METRIC_LABELS = {
    "LDL": "LDL",
    "Gluc": "Glucose",
    "HbA1c": "HbA1c",
    "BMI": "BMI",
    "TG": "Triglyceride",
    "BP": "Blood Pressure",
}

DRILLDOWN_LABELS = {
    "department": "Department",
    "age_group": "Age Group",
    "sex": "Sex",
}

INSIGHT_RULES = {
    "LDL": {"persistent_high": 15.0, "deteriorating_notable": 10.0, "improving_notable": 10.0},
    "Gluc": {"persistent_high": 15.0, "deteriorating_notable": 10.0, "improving_notable": 10.0},
    "HbA1c": {"persistent_high": 15.0, "deteriorating_notable": 10.0, "improving_notable": 10.0},
    "BMI": {"persistent_high": 15.0, "deteriorating_notable": 10.0, "improving_notable": 10.0},
    "TG": {"persistent_high": 15.0, "deteriorating_notable": 10.0, "improving_notable": 10.0},
    "BP": {"persistent_high": 15.0, "deteriorating_notable": 10.0, "improving_notable": 10.0},
}


def build_verified_insight_packets(
    *,
    matched_cohort_comparison: list[dict],
    trajectory_measure_summary: list[dict],
) -> list[dict]:
    matched_lookup = {row["measure"]: row for row in matched_cohort_comparison}
    packets: list[dict] = []

    for measure_code, thresholds in INSIGHT_RULES.items():
        matched_row = matched_lookup.get(measure_code)
        trajectory_row = next((row for row in trajectory_measure_summary if row["metric"] == measure_code), None)
        if matched_row is None or trajectory_row is None:
            continue

        packet = classify_insight_packet(
            metric=measure_code,
            matched_row=matched_row,
            trajectory_row=trajectory_row,
            thresholds=thresholds,
        )
        packets.append(packet)

    return select_top_priority_insights(packets, limit=len(packets))


def classify_insight_packet(
    *,
    metric: str,
    matched_row: dict,
    trajectory_row: dict,
    thresholds: dict[str, float],
) -> dict:
    persistent_pct = float(trajectory_row.get("persistent_abnormal_pct", 0.0))
    deteriorating_pct = float(trajectory_row.get("deteriorating_pct", 0.0))
    improving_pct = float(trajectory_row.get("improving_pct", 0.0))
    paired_n = int(trajectory_row.get("paired_n", 0))

    if persistent_pct >= thresholds["persistent_high"]:
        pattern = "persistent_abnormal"
        severity = "high"
        message = f"Persistent {METRIC_LABELS.get(metric, metric)} abnormality is the leading concern."
        allowed_terms = ["persistent", "remained abnormal", "ongoing concern"]
        forbidden_terms = ["worsening", "deteriorating", "increased"]
    elif deteriorating_pct >= thresholds["deteriorating_notable"]:
        pattern = "deteriorating"
        severity = "high" if deteriorating_pct >= 15 else "medium"
        message = f"{METRIC_LABELS.get(metric, metric)} shows notable deterioration."
        allowed_terms = ["deteriorating", "worsening", "requires attention"]
        forbidden_terms = ["persistent", "remained abnormal"]
    elif improving_pct >= thresholds["improving_notable"]:
        pattern = "improving"
        severity = "medium"
        message = f"{METRIC_LABELS.get(metric, metric)} shows notable improvement."
        allowed_terms = ["improving", "decreased", "better than the previous year"]
        forbidden_terms = ["persistent", "worsening", "deteriorating"]
    else:
        pattern = "mixed"
        severity = "low"
        message = f"{METRIC_LABELS.get(metric, metric)} shows mixed year-over-year movement."
        allowed_terms = ["mixed", "requires attention"]
        forbidden_terms = []

    caveat = None
    if paired_n < 100:
        caveat = "Matched denominator is limited; interpretation should be cautious."

    priority_score = round((persistent_pct * 0.5) + (deteriorating_pct * 0.4) - (improving_pct * 0.2), 2)

    return {
        "metric": metric,
        "pattern": pattern,
        "severity": severity,
        "evidence": {
            "persistent_abnormal_pct": persistent_pct,
            "deteriorating_pct": deteriorating_pct,
            "improving_pct": improving_pct,
            "paired_n": paired_n,
            "matched_previous_abnormal_pct": matched_row.get("abnormal_percent_previous_matched"),
            "matched_latest_abnormal_pct": matched_row.get("abnormal_percent_latest_matched"),
            "matched_delta_percentage_points": matched_row.get("delta_percentage_points_matched"),
        },
        "message": message,
        "recommended_drilldown": ["department", "age_group"],
        "caveat": caveat,
        "priority_score": priority_score,
        "allowed_terms": allowed_terms,
        "forbidden_terms": forbidden_terms,
    }


def select_top_priority_insights(insight_packets: list[dict], *, limit: int = 3) -> list[dict]:
    return sorted(
        insight_packets,
        key=lambda packet: (-packet["priority_score"], packet["metric"]),
    )[:limit]


def build_verified_insight_payload(
    insight_packets: list[dict],
    *,
    latest_year: int | None = None,
    previous_year: int | None = None,
) -> dict[str, Any]:
    return {
        "latest_year": latest_year,
        "previous_year": previous_year,
        "verified_insights": insight_packets,
    }


def validate_generated_summary(summary: str, insight_packets: list[dict]) -> tuple[bool, list[str]]:
    issues: list[str] = []
    lowered = summary.lower()
    for packet in insight_packets:
        for forbidden_term in packet.get("forbidden_terms", []):
            if forbidden_term.lower() in lowered:
                issues.append(f"{packet['metric']}: forbidden term '{forbidden_term}' detected.")
    return (len(issues) == 0, issues)


def humanize_drilldown_labels(drilldowns: list[str]) -> list[str]:
    return [DRILLDOWN_LABELS.get(item, item.replace("_", " ").title()) for item in drilldowns]


def build_pattern_summary_lines(packet: dict) -> list[str]:
    evidence = packet["evidence"]
    lines: list[str] = []

    if evidence["persistent_abnormal_pct"] > 0:
        lines.append(f"{evidence['persistent_abnormal_pct']:.1f}% remained in an abnormal category.")
    if evidence["deteriorating_pct"] > 0:
        lines.append(f"{evidence['deteriorating_pct']:.1f}% moved toward a less favorable category.")
    if evidence["improving_pct"] > 0:
        lines.append(f"{evidence['improving_pct']:.1f}% moved toward a more favorable category.")

    return lines


def build_organizational_significance(packet: dict) -> str:
    metric_label = METRIC_LABELS.get(packet["metric"], packet["metric"])
    if packet["pattern"] == "persistent_abnormal":
        return (
            f"Because this analysis follows the same employees across both years, it shows that {metric_label.lower()}-related risk is not just an isolated annual fluctuation. "
            f"Instead, it remains an ongoing workforce health concern among returning employees."
        )
    if packet["pattern"] == "deteriorating":
        return (
            f"This suggests that {metric_label.lower()} risk is moving in a less favorable direction for part of the workforce. "
            "Because the analysis follows the same employees across years, leaders can treat this as a clearer signal than a one-year snapshot."
        )
    if packet["pattern"] == "improving":
        return (
            f"This suggests that {metric_label.lower()} risk is improving for a meaningful group of employees. "
            "Because the analysis follows the same employees across years, leaders can view this as a real positive shift rather than a temporary change in who participated."
        )
    return (
        f"This suggests that {metric_label.lower()} risk is mixed across returning employees. "
        "Because the analysis follows the same employees across years, it offers a clearer picture than a single annual snapshot alone."
    )


def build_executive_headline(packet: dict) -> str:
    metric_label = METRIC_LABELS.get(packet["metric"], packet["metric"])
    if packet["pattern"] == "persistent_abnormal":
        return f"Persistent {metric_label}-related health risk remains the leading workforce health concern."
    if packet["pattern"] == "deteriorating":
        return f"{metric_label} trends are moving in a less favorable direction and require attention."
    if packet["pattern"] == "improving":
        return f"{metric_label} trends show encouraging improvement for part of the workforce."
    return f"{metric_label} remains an important workforce health issue to monitor."


def build_executive_summary(packet: dict) -> str:
    evidence = packet["evidence"]
    metric_label = METRIC_LABELS.get(packet["metric"], packet["metric"])
    persistent_pct = evidence["persistent_abnormal_pct"]
    deteriorating_pct = evidence["deteriorating_pct"]
    improving_pct = evidence["improving_pct"]
    matched_count = evidence["paired_n"]

    lead_sentence = (
        f"Analysis of {matched_count} employees who participated in health assessments in both years indicates that "
        f"{metric_label} remains the most important workforce health signal in this review."
    )

    interpretation_parts: list[str] = []
    if persistent_pct > 0:
        interpretation_parts.append(
            f"More than {persistent_pct:.1f}% remained in an abnormal category, suggesting that the issue is persistent rather than temporary."
        )
    if deteriorating_pct > 0:
        interpretation_parts.append(
            f"In addition, {deteriorating_pct:.1f}% moved toward a less favorable category, indicating that risk is still building for part of the workforce."
        )
    if improving_pct > 0:
        interpretation_parts.append(
            f"By comparison, {improving_pct:.1f}% moved toward a more favorable category, so improvement is present but more limited."
        )

    closing_sentence = (
        f"Overall, the findings suggest that {metric_label.lower()}-related health risk continues to affect a meaningful share of returning employees."
    )

    summary = " ".join([lead_sentence] + interpretation_parts + [closing_sentence])
    if packet.get("caveat"):
        summary = f"{summary} {packet['caveat']}"
    return summary


def build_exploration_questions(packet: dict) -> list[str]:
    metric_label = METRIC_LABELS.get(packet["metric"], packet["metric"])
    drilldowns = humanize_drilldown_labels(packet["recommended_drilldown"])
    first_dimension = drilldowns[0] if drilldowns else "workforce segment"
    second_dimension = drilldowns[1] if len(drilldowns) > 1 else "workforce segment"

    return [
        f"Which {first_dimension.lower()}s account for most persistent {metric_label} concerns?",
        f"Are {metric_label}-related health risks concentrated within specific {second_dimension.lower()}s?",
        f"Which workforce segments contribute most to recent deterioration in {metric_label}?",
    ]


def build_fallback_executive_brief(insight_packets: list[dict]) -> str:
    if not insight_packets:
        return (
            "Headline: Most health indicators cannot yet be summarized in a year-over-year executive brief.\n\n"
            "Executive Summary: The current dashboard does not yet contain enough verified year-over-year evidence to produce a reliable executive workforce health briefing.\n\n"
            "Why It Matters: Leadership should rely on the available dashboard evidence until more consistent returning-employee comparisons are available.\n\n"
            "Suggested Next Exploration:\n"
            "- Review the year-over-year matched results\n"
            "- Inspect available movement evidence"
        )

    lead_packet = insight_packets[0]
    exploration_lines = build_exploration_questions(lead_packet)

    return (
        f"Headline: {build_executive_headline(lead_packet)}\n\n"
        f"Executive Summary: {build_executive_summary(lead_packet)}\n\n"
        f"Why It Matters: {build_organizational_significance(lead_packet)}\n\n"
        "Suggested Next Exploration:\n"
        f"- {exploration_lines[0]}\n"
        f"- {exploration_lines[1]}\n"
        f"- {exploration_lines[2]}"
    )
