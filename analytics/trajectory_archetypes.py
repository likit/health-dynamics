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
from app.models import DimDate, DimMeasure, FactHealthArchetype, FactHealthTrajectory

STATUS_RANK = {"normal": 0, "borderline": 1, "abnormal": 2}


@dataclass
class ArchetypeStats:
    groups_seen: int = 0
    archetypes_loaded: int = 0
    persons_loaded: int = 0


def load_trajectory_groups() -> dict[tuple[int, int], list[FactHealthTrajectory]]:
    rows = (
        FactHealthTrajectory.query.join(DimDate, FactHealthTrajectory.from_date_key == DimDate.date_key)
        .order_by(
            FactHealthTrajectory.person_key.asc(),
            FactHealthTrajectory.measure_key.asc(),
            DimDate.checkup_date.asc(),
            FactHealthTrajectory.trajectory_key.asc(),
        )
        .all()
    )

    grouped: dict[tuple[int, int], list[FactHealthTrajectory]] = defaultdict(list)
    for row in rows:
        grouped[(row.person_key, row.measure_key)].append(row)
    return grouped


def classify_archetype(rows: list[FactHealthTrajectory]) -> tuple[str, str, float, int, int]:
    first_row = rows[0]
    last_row = rows[-1]
    unique_date_keys = {row.from_date_key for row in rows} | {row.to_date_key for row in rows}
    num_checkups = len(unique_date_keys)

    transition_counts = Counter(row.status_transition for row in rows)
    direction_counts = Counter(row.trajectory_class for row in rows)
    all_statuses = [first_row.previous_status, *[row.current_status for row in rows]]
    initial_status = all_statuses[0]
    final_status = all_statuses[-1]
    has_abnormal_status = "abnormal" in all_statuses

    first_date_key = min(unique_date_keys)
    last_date_key = max(unique_date_keys)

    # Fewer than three observations means the longitudinal signal is too short to
    # trust a richer archetype label. We explicitly keep these cases separate so
    # downstream dashboards can treat them as low-confidence trajectories.
    if num_checkups < 3:
        return (
            "Insufficient Data",
            "Fewer than three checkups are available, so the pattern is too short for a stable archetype.",
            0.35,
            first_date_key,
            last_date_key,
        )

    # Stable Healthy is the cleanest state: every observed status is normal and
    # there is no drift into borderline or abnormal territory anywhere in the series.
    if all(status == "normal" for status in all_statuses):
        return (
            "Stable Healthy",
            "All observed checkups remain in the normal range across the full timeline.",
            confidence_from_match_strength(1.0, num_checkups),
            first_date_key,
            last_date_key,
        )

    # Emerging Risk captures people who have started leaving the normal range but
    # have not yet crossed into abnormal territory. The key signature is at least
    # one normal_to_borderline transition with no abnormal status present at all.
    if transition_counts["normal_to_borderline"] > 0 and not has_abnormal_status:
        strength = transition_counts["normal_to_borderline"] / len(rows)
        return (
            "Emerging Risk",
            "The pattern shows movement from normal to borderline without abnormal results yet.",
            confidence_from_match_strength(strength, num_checkups),
            first_date_key,
            last_date_key,
        )

    # Progressive Deterioration is reserved for clear worsening toward higher-risk
    # states. We require at least one direct step into abnormal and an overall final
    # status that is not better than the initial status.
    if (
        transition_counts["normal_to_abnormal"] > 0
        or transition_counts["borderline_to_abnormal"] > 0
    ) and STATUS_RANK.get(final_status, -1) >= STATUS_RANK.get(initial_status, -1):
        worsening_share = (
            transition_counts["normal_to_abnormal"] + transition_counts["borderline_to_abnormal"]
        ) / len(rows)
        return (
            "Progressive Deterioration",
            "Risk states worsen over time, including at least one transition into abnormal status.",
            confidence_from_match_strength(max(worsening_share, direction_counts["worsening"] / len(rows)), num_checkups),
            first_date_key,
            last_date_key,
        )

    # Chronic High Risk describes trajectories where abnormal results keep recurring.
    # We look for abnormal_to_abnormal as the dominant transition and require the
    # series to still end in an abnormal state.
    if (
        transition_counts["abnormal_to_abnormal"] == max(transition_counts.values())
        and final_status == "abnormal"
        and transition_counts["abnormal_to_abnormal"] > 0
    ):
        strength = transition_counts["abnormal_to_abnormal"] / len(rows)
        return (
            "Chronic High Risk",
            "Abnormal results persist across checkups, indicating sustained high-risk status.",
            confidence_from_match_strength(strength, num_checkups),
            first_date_key,
            last_date_key,
        )

    # Recovery focuses on people who have been abnormal and later move to a lower-risk
    # state. We require both a recovery-type transition and a final status rank that
    # is better than the first observed status.
    if (
        transition_counts["abnormal_to_borderline"] > 0
        or transition_counts["abnormal_to_normal"] > 0
    ) and STATUS_RANK.get(final_status, 99) < STATUS_RANK.get(initial_status, 99):
        strength = (
            transition_counts["abnormal_to_borderline"] + transition_counts["abnormal_to_normal"]
        ) / len(rows)
        return (
            "Recovery",
            "Later checkups move from abnormal toward lower-risk states, indicating improvement over time.",
            confidence_from_match_strength(max(strength, direction_counts["improving"] / len(rows)), num_checkups),
            first_date_key,
            last_date_key,
        )

    # Fluctuating is the fallback for longer sequences with mixed movement. We use it
    # when both improving and worsening signals exist and no clearer monotonic pattern
    # dominates the sequence.
    if direction_counts["improving"] > 0 and direction_counts["worsening"] > 0:
        balance = min(direction_counts["improving"], direction_counts["worsening"]) / len(rows)
        return (
            "Fluctuating",
            "The trajectory alternates between improvement and worsening with no stable direction.",
            confidence_from_match_strength(balance, num_checkups),
            first_date_key,
            last_date_key,
        )

    # If no prior rule matched, use the closest deterministic interpretation.
    # This catches edge cases such as consistently borderline profiles or short
    # stable abnormal/borderline runs that are directionally flat.
    if final_status == "abnormal":
        return (
            "Chronic High Risk",
            "The trajectory ends in abnormal status without a sustained recovery signal.",
            confidence_from_match_strength(max(transition_counts["abnormal_to_abnormal"] / len(rows), 0.55), num_checkups),
            first_date_key,
            last_date_key,
        )

    if STATUS_RANK.get(final_status, 99) > STATUS_RANK.get(initial_status, 99):
        return (
            "Progressive Deterioration",
            "The overall direction is toward a higher-risk ending status.",
            confidence_from_match_strength(0.55, num_checkups),
            first_date_key,
            last_date_key,
        )

    if STATUS_RANK.get(final_status, 99) < STATUS_RANK.get(initial_status, 99):
        return (
            "Recovery",
            "The overall direction is toward a lower-risk ending status.",
            confidence_from_match_strength(0.55, num_checkups),
            first_date_key,
            last_date_key,
        )

    return (
        "Fluctuating",
        "The trajectory does not show a single dominant pattern and is best treated as mixed movement.",
        confidence_from_match_strength(0.45, num_checkups),
        first_date_key,
        last_date_key,
    )


def confidence_from_match_strength(match_strength: float, num_checkups: int) -> float:
    observation_bonus = min(max(num_checkups - 2, 0) * 0.05, 0.2)
    score = 0.4 + (match_strength * 0.4) + observation_bonus
    return round(min(score, 0.99), 2)


def rebuild_archetypes() -> ArchetypeStats:
    stats = ArchetypeStats()
    groups = load_trajectory_groups()
    stats.groups_seen = len(groups)

    db.session.execute(delete(FactHealthArchetype))
    db.session.flush()

    people_loaded: set[int] = set()
    for (person_key, measure_key), rows in groups.items():
        archetype_name, archetype_description, confidence_score, first_date_key, last_date_key = classify_archetype(rows)
        unique_date_keys = {row.from_date_key for row in rows} | {row.to_date_key for row in rows}

        db.session.add(
            FactHealthArchetype(
                person_key=person_key,
                measure_key=measure_key,
                first_date_key=first_date_key,
                last_date_key=last_date_key,
                num_checkups=len(unique_date_keys),
                archetype_name=archetype_name,
                archetype_description=archetype_description,
                confidence_score=confidence_score,
            )
        )
        stats.archetypes_loaded += 1
        people_loaded.add(person_key)

    db.session.commit()
    stats.persons_loaded = len(people_loaded)
    return stats


def print_report(stats: ArchetypeStats) -> None:
    print("Health Archetype Build Report")
    print("=============================")
    print(f"Trajectory groups seen: {stats.groups_seen}")
    print(f"Persons loaded: {stats.persons_loaded}")
    print(f"Archetypes loaded: {stats.archetypes_loaded}")


def main() -> int:
    app = create_app()
    with app.app_context():
        db.create_all()
        stats = rebuild_archetypes()
        print_report(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
