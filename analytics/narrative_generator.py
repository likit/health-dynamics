from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import desc, func

from app import db
from analytics.knowledge_retrieval import GuidanceNote, retrieve_guidance
from analytics.risk_rules import classify_status
from app.models import (
    DimDate,
    DimMeasure,
    DimPerson,
    FactCheckupMeasurement,
    FactHealthArchetype,
    FactHealthTrajectory,
    FactPersonCheckupSnapshot,
    FactPopulationForecast,
)

RISK_MEASURE_CODES = ["Gluc", "LDL", "TG", "HDL"]


@dataclass
class SummarySection:
    heading: str
    paragraphs: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    guidance_notes: list[GuidanceNote] = field(default_factory=list)
    empty_message: str | None = None


@dataclass
class ExecutiveSummary:
    population_overview: SummarySection
    movement_summary: SummarySection
    archetype_summary: SummarySection
    forecast_summary: SummarySection
    attention_areas: SummarySection


def generate_executive_summary() -> ExecutiveSummary:
    latest_snapshots = load_latest_snapshots()
    latest_measurements = load_latest_measurements(RISK_MEASURE_CODES)
    population_profiles = build_population_profiles(latest_snapshots, latest_measurements)

    summary = ExecutiveSummary(
        population_overview=build_population_overview(population_profiles),
        movement_summary=build_movement_summary(),
        archetype_summary=build_archetype_summary(),
        forecast_summary=build_forecast_summary(),
        attention_areas=build_attention_areas(population_profiles),
    )
    for section in [
        summary.population_overview,
        summary.movement_summary,
        summary.archetype_summary,
        summary.forecast_summary,
        summary.attention_areas,
    ]:
        section.guidance_notes = deduplicate_guidance(section.guidance_notes)
    return summary


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

    for person in DimPerson.query.order_by(DimPerson.cms_code.asc()).all():
        snapshot = latest_snapshots.get(person.person_key)
        bmi = snapshot.bmi if snapshot else None
        glucose = latest_measurements.get((person.person_key, "Gluc"))
        ldl = latest_measurements.get((person.person_key, "LDL"))
        tg = latest_measurements.get((person.person_key, "TG"))
        hdl = latest_measurements.get((person.person_key, "HDL"))

        profile = {
            "person": person,
            "bmi_status": classify_status("BMI", bmi),
            "glucose_status": classify_status("Gluc", glucose),
            "ldl_status": classify_status("LDL", ldl),
            "tg_status": classify_status("TG", tg),
            "hdl_status": classify_status("HDL", hdl),
        }
        profile["abnormal_marker_count"] = sum(
            profile[key] == "abnormal"
            for key in ["bmi_status", "glucose_status", "ldl_status", "tg_status", "hdl_status"]
        )
        profiles.append(profile)

    return profiles


def build_population_overview(profiles: list[dict]) -> SummarySection:
    section = SummarySection(heading="Population Health Overview")
    total_people = len(profiles)
    if total_people == 0:
        section.empty_message = "No person-level checkup data is available yet."
        return section

    section.paragraphs.append(
        f"The current warehouse contains {total_people} people with latest checkup profiles available for population-level review."
    )

    marker_labels = {
        "bmi_status": "BMI",
        "glucose_status": "glucose",
        "ldl_status": "LDL",
        "tg_status": "triglyceride",
        "hdl_status": "HDL",
    }
    abnormal_rates = []
    for key, label in marker_labels.items():
        known = [profile for profile in profiles if profile[key] != "unknown"]
        if not known:
            continue
        abnormal_count = len([profile for profile in known if profile[key] == "abnormal"])
        abnormal_rates.append((label, (abnormal_count / len(known)) * 100.0, abnormal_count))

    abnormal_rates.sort(key=lambda item: item[1], reverse=True)
    if abnormal_rates:
        top_markers = ", ".join(
            f"{label} ({rate:.1f}%, {count} people)" for label, rate, count in abnormal_rates[:3]
        )
        section.paragraphs.append(f"The highest abnormal marker burdens are currently seen in {top_markers}.")
        section.guidance_notes.extend(retrieve_guidance(*(label for label, _, _ in abnormal_rates[:2]), limit=2))
    else:
        section.paragraphs.append("No abnormal marker rates can be calculated from the latest measurement set.")

    department_rows = summarize_department_burden(profiles)
    if department_rows:
        top_departments = ", ".join(
            f"{row['department']} ({row['three_plus_pct']:.1f}% with 3+ abnormal markers)"
            for row in department_rows[:3]
        )
        section.paragraphs.append(f"Departments with higher NCD risk burden include {top_departments}.")
        section.guidance_notes.extend(retrieve_guidance("department-level intervention", "metabolic syndrome", limit=2))
    else:
        section.empty_message = "Department fields are not populated in the current data, so department burden cannot be summarized yet."

    return section


def summarize_department_burden(profiles: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for profile in profiles:
        department = (profile["person"].department or "").strip()
        if department:
            groups[department].append(profile)

    summary: list[dict] = []
    for department, members in groups.items():
        count_three_plus = len([profile for profile in members if profile["abnormal_marker_count"] >= 3])
        summary.append(
            {
                "department": department,
                "three_plus_pct": (count_three_plus / len(members)) * 100.0,
            }
        )
    return sorted(summary, key=lambda row: (-row["three_plus_pct"], row["department"].lower()))


def build_movement_summary() -> SummarySection:
    section = SummarySection(heading="Health Movement Summary")
    trajectory_count = FactHealthTrajectory.query.count()
    if trajectory_count == 0:
        section.empty_message = "No trajectory records are available yet."
        return section

    worsening_rows = (
        db.session.query(DimMeasure.measure_name, func.count(FactHealthTrajectory.trajectory_key))
        .join(DimMeasure, FactHealthTrajectory.measure_key == DimMeasure.measure_key)
        .filter(FactHealthTrajectory.trajectory_class == "worsening")
        .group_by(DimMeasure.measure_name)
        .order_by(func.count(FactHealthTrajectory.trajectory_key).desc())
        .all()
    )
    improving_rows = (
        db.session.query(DimMeasure.measure_name, func.count(FactHealthTrajectory.trajectory_key))
        .join(DimMeasure, FactHealthTrajectory.measure_key == DimMeasure.measure_key)
        .filter(FactHealthTrajectory.trajectory_class == "improving")
        .group_by(DimMeasure.measure_name)
        .order_by(func.count(FactHealthTrajectory.trajectory_key).desc())
        .all()
    )
    persistent_rows = (
        db.session.query(DimMeasure.measure_name, func.count(FactHealthTrajectory.trajectory_key))
        .join(DimMeasure, FactHealthTrajectory.measure_key == DimMeasure.measure_key)
        .filter(FactHealthTrajectory.status_transition == "abnormal_to_abnormal")
        .group_by(DimMeasure.measure_name)
        .order_by(func.count(FactHealthTrajectory.trajectory_key).desc())
        .all()
    )

    if worsening_rows:
        section.paragraphs.append(
            "Major worsening trends are concentrated in "
            + ", ".join(f"{name} ({count})" for name, count in worsening_rows[:3])
            + "."
        )
        section.guidance_notes.extend(retrieve_guidance(*(name for name, _ in worsening_rows[:2]), "progressive deterioration", limit=3))
    else:
        section.paragraphs.append("No worsening trajectories are currently recorded.")

    if improving_rows:
        section.paragraphs.append(
            "Major improving trends are visible in "
            + ", ".join(f"{name} ({count})" for name, count in improving_rows[:3])
            + "."
        )
        section.guidance_notes.extend(retrieve_guidance("recovery pattern", *(name for name, _ in improving_rows[:1]), limit=2))
    else:
        section.paragraphs.append("No improving trajectories are currently recorded.")

    if persistent_rows:
        section.paragraphs.append(
            "Persistent abnormal patterns remain most common in "
            + ", ".join(f"{name} ({count})" for name, count in persistent_rows[:3])
            + "."
        )
        section.guidance_notes.extend(retrieve_guidance("persistent abnormal status", *(name for name, _ in persistent_rows[:2]), limit=3))
    else:
        section.paragraphs.append("No persistent abnormal patterns are currently present in the trajectory table.")

    return section


def build_archetype_summary() -> SummarySection:
    section = SummarySection(heading="Trajectory Archetype Summary")
    archetype_count = FactHealthArchetype.query.count()
    if archetype_count == 0:
        section.empty_message = "No archetype records are available yet."
        return section

    archetype_rows = (
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

    section.paragraphs.append(
        "The most common archetypes are "
        + ", ".join(f"{name} ({count})" for name, count in archetype_rows[:3])
        + "."
    )

    if progressive_rows:
        section.paragraphs.append(
            "Measures with the highest progressive deterioration burden are "
            + ", ".join(f"{name} ({count})" for name, count in progressive_rows[:3])
            + "."
        )
        section.guidance_notes.extend(retrieve_guidance("progressive deterioration", *(name for name, _ in progressive_rows[:2]), limit=3))
    else:
        section.paragraphs.append("No progressive deterioration archetypes are present yet.")

    if chronic_rows:
        section.paragraphs.append(
            "Measures with the strongest chronic high-risk pattern are "
            + ", ".join(f"{name} ({count})" for name, count in chronic_rows[:3])
            + "."
        )
        section.guidance_notes.extend(retrieve_guidance("persistent abnormal status", *(name for name, _ in chronic_rows[:2]), limit=3))
    else:
        section.paragraphs.append("No chronic high-risk archetypes are present yet.")

    return section


def build_forecast_summary() -> SummarySection:
    section = SummarySection(heading="Forecast Summary")
    forecast_rows = (
        FactPopulationForecast.query.join(DimMeasure, FactPopulationForecast.measure_key == DimMeasure.measure_key)
        .order_by(DimMeasure.measure_name.asc())
        .all()
    )
    if not forecast_rows:
        section.empty_message = "No forecast records are available yet."
        return section

    worsening = []
    stable_or_improving = []
    for row in forecast_rows:
        delta_abnormal = row.forecast_abnormal_count - row.current_abnormal_count
        if delta_abnormal > 0.5:
            worsening.append((row.measure.measure_name, delta_abnormal))
        else:
            stable_or_improving.append((row.measure.measure_name, delta_abnormal))

    if worsening:
        worsening.sort(key=lambda item: item[1], reverse=True)
        section.paragraphs.append(
            "Measures predicted to worsen next year include "
            + ", ".join(f"{name} (+{delta:.2f} abnormal forecast)" for name, delta in worsening[:3])
            + "."
        )
        section.guidance_notes.extend(retrieve_guidance(*(name for name, _ in worsening[:2]), "progressive deterioration", limit=3))
    else:
        section.paragraphs.append("No measure currently shows a worsening abnormal forecast at the one-year horizon.")

    if stable_or_improving:
        stable_or_improving.sort(key=lambda item: item[1])
        section.paragraphs.append(
            "Measures with stable or improving forecast patterns include "
            + ", ".join(f"{name} ({delta:+.2f} abnormal forecast)" for name, delta in stable_or_improving[:3])
            + "."
        )
        section.guidance_notes.extend(retrieve_guidance("recovery pattern", *(name for name, _ in stable_or_improving[:1]), limit=2))
    else:
        section.paragraphs.append("No stable or improving forecast measures are currently available.")

    return section


def build_attention_areas(profiles: list[dict]) -> SummarySection:
    section = SummarySection(heading="Suggested Management Attention Areas")
    suggestions: list[str] = []

    glucose_departments = top_department_for_measure_trajectory("Gluc", "worsening")
    if glucose_departments:
        department, percentage = glucose_departments
        suggestions.append(
            f"Prioritize departments with high worsening glucose trajectories, especially {department} ({percentage:.1f}% worsening glucose trajectories)."
        )
        section.guidance_notes.extend(retrieve_guidance("glucose", "department-level intervention", "progressive deterioration", limit=3))

    chronic_ldl = top_measure_archetype_count("LDL", "Chronic High Risk")
    if chronic_ldl is not None:
        suggestions.append(
            f"Monitor groups with chronic high LDL; {chronic_ldl} archetype records are classified as Chronic High Risk for LDL."
        )
        section.guidance_notes.extend(retrieve_guidance("ldl", "persistent abnormal status", limit=2))

    bmi_departments = top_department_for_measure_trajectory("BMI", "worsening")
    if bmi_departments:
        department, percentage = bmi_departments
        suggestions.append(
            f"Investigate departments with high BMI deterioration, particularly {department} ({percentage:.1f}% worsening BMI trajectories)."
        )
        section.guidance_notes.extend(retrieve_guidance("bmi", "department-level intervention", "progressive deterioration", limit=3))

    if not suggestions:
        if not any((profile["person"].department or "").strip() for profile in profiles):
            section.empty_message = "Department-linked management suggestions cannot be generated because department data is not populated."
        else:
            section.empty_message = "Not enough movement or archetype data is available to generate management suggestions yet."
        return section

    section.bullets.extend(suggestions)
    section.guidance_notes = deduplicate_guidance(section.guidance_notes)
    return section


def top_department_for_measure_trajectory(measure_code: str, trajectory_class: str) -> tuple[str, float] | None:
    rows = (
        db.session.query(DimPerson.department, FactHealthTrajectory.trajectory_class)
        .join(DimPerson, FactHealthTrajectory.person_key == DimPerson.person_key)
        .join(DimMeasure, FactHealthTrajectory.measure_key == DimMeasure.measure_key)
        .filter(DimMeasure.measure_code == measure_code)
        .all()
    )

    groups: dict[str, list[str]] = defaultdict(list)
    for department, current_class in rows:
        department_name = (department or "").strip()
        if department_name:
            groups[department_name].append(current_class)

    best_department: tuple[str, float] | None = None
    for department, classes in groups.items():
        if not classes:
            continue
        percentage = (len([value for value in classes if value == trajectory_class]) / len(classes)) * 100.0
        if best_department is None or percentage > best_department[1]:
            best_department = (department, percentage)
    return best_department


def top_measure_archetype_count(measure_code: str, archetype_name: str) -> int | None:
    count = (
        db.session.query(func.count(FactHealthArchetype.archetype_key))
        .join(DimMeasure, FactHealthArchetype.measure_key == DimMeasure.measure_key)
        .filter(
            DimMeasure.measure_code == measure_code,
            FactHealthArchetype.archetype_name == archetype_name,
        )
        .scalar()
    )
    return int(count) if count else None


def deduplicate_guidance(notes: list[GuidanceNote]) -> list[GuidanceNote]:
    deduped: list[GuidanceNote] = []
    seen: set[str] = set()
    for note in notes:
        if note.topic in seen:
            continue
        deduped.append(note)
        seen.add(note.topic)
    return deduped
