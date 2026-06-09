import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from app import create_app, db
from app.health_dynamics.exploration_briefs import (
    generate_exploration_fallback_brief,
    generate_exploration_insight_packet,
    load_or_create_exploration_brief,
    serialize_exploration_brief_record,
)
from app.health_dynamics.explorations import build_exploration_context
from app.models import DimDate, DimMeasure, DimPerson, ExplorationBrief, FactHealthTrajectory, FactPersonCheckupSnapshot


class TestConfig:
    TESTING = True
    SQLALCHEMY_DATABASE_URI = ""
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = "test"
    LOCAL_LLM_BASE_URL = "http://localhost:11434/v1"
    LOCAL_LLM_MODEL = "ministral-3:3b"
    LOCAL_LLM_API_KEY = "ollama"


class ExplorationBriefTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        TestConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{self.db_path}"
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        db.session.add_all(
            [
                DimDate(date_key=20250115, checkup_year=2025, checkup_date=date(2025, 1, 15)),
                DimDate(date_key=20260115, checkup_year=2026, checkup_date=date(2026, 1, 15)),
                DimMeasure(measure_key=1, measure_code="BMI", measure_name="BMI"),
                DimPerson(person_key=1, cms_code="CMS001", employee_id="E001", full_name="A", department="Department A"),
                DimPerson(person_key=2, cms_code="CMS002", employee_id="E002", full_name="B", department="Department B"),
                DimPerson(person_key=3, cms_code="CMS003", employee_id="E003", full_name="C", department="Department B"),
            ]
        )
        db.session.add_all(
            [
                FactPersonCheckupSnapshot(snapshot_key=1, person_key=1, date_key=20260115, age=46, bmi=31.0),
                FactPersonCheckupSnapshot(snapshot_key=2, person_key=2, date_key=20260115, age=38, bmi=28.5),
                FactPersonCheckupSnapshot(snapshot_key=3, person_key=3, date_key=20260115, age=33, bmi=24.0),
            ]
        )
        db.session.add_all(
            [
                FactHealthTrajectory(
                    trajectory_key=1,
                    person_key=1,
                    measure_key=1,
                    from_date_key=20250115,
                    to_date_key=20260115,
                    previous_value=30.5,
                    current_value=31.0,
                    delta_value=0.5,
                    percent_change=1.6,
                    previous_status="abnormal",
                    current_status="abnormal",
                    status_transition="abnormal_to_abnormal",
                    trajectory_class="stable",
                    risk_direction="unchanged_risk",
                    interpretation="Persistent",
                ),
                FactHealthTrajectory(
                    trajectory_key=2,
                    person_key=2,
                    measure_key=1,
                    from_date_key=20250115,
                    to_date_key=20260115,
                    previous_value=24.0,
                    current_value=28.5,
                    delta_value=4.5,
                    percent_change=18.8,
                    previous_status="normal",
                    current_status="abnormal",
                    status_transition="normal_to_abnormal",
                    trajectory_class="worsening",
                    risk_direction="higher_risk",
                    interpretation="Deteriorating",
                ),
                FactHealthTrajectory(
                    trajectory_key=3,
                    person_key=3,
                    measure_key=1,
                    from_date_key=20250115,
                    to_date_key=20260115,
                    previous_value=27.0,
                    current_value=24.0,
                    delta_value=-3.0,
                    percent_change=-11.1,
                    previous_status="abnormal",
                    current_status="normal",
                    status_transition="abnormal_to_normal",
                    trajectory_class="improving",
                    risk_direction="lower_risk",
                    interpretation="Improving",
                ),
            ]
        )
        db.session.commit()

        self.exploration = build_exploration_context(
            year=2026,
            metric="BMI",
            dimension="age_group",
            pattern="persistent_abnormal",
        )

    def tearDown(self) -> None:
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_exploration_insight_packet_generation(self) -> None:
        packet = generate_exploration_insight_packet(self.exploration)
        self.assertEqual(packet["metric"], "BMI")
        self.assertEqual(packet["dimension"], "age_group")
        self.assertEqual(packet["total_paired_n"], 3)

    def test_highest_persistent_group_detection(self) -> None:
        packet = generate_exploration_insight_packet(self.exploration)
        self.assertEqual(packet["highest_persistent_group"], "40-49")
        self.assertEqual(packet["highest_persistent_pct"], 100.0)

    def test_highest_deterioration_group_detection(self) -> None:
        packet = generate_exploration_insight_packet(self.exploration)
        self.assertEqual(packet["highest_deterioration_group"], "30-39")
        self.assertEqual(packet["highest_deterioration_pct"], 50.0)

    def test_fallback_brief_generation(self) -> None:
        packet = generate_exploration_insight_packet(self.exploration)
        brief = generate_exploration_fallback_brief(packet)
        self.assertIn("Key Finding:", brief)
        self.assertIn("Interpretation:", brief)
        self.assertIn("Suggested Next Investigation:", brief)
        self.assertNotIn("age_group", brief)

    @patch("app.health_dynamics.exploration_briefs.generate_exploration_brief")
    def test_brief_persistence_and_reload(self, mock_generate) -> None:
        mock_generate.return_value = (
            "Key Finding: Employees aged 40-49 show the highest level of persistent BMI concern.\n\n"
            "Interpretation: BMI-related health risk appears established in this group.\n\n"
            "Suggested Next Investigation: Review Department-level BMI patterns for the groups that stand out.",
            "ministral-3:3b",
        )

        first = load_or_create_exploration_brief(
            exploration=self.exploration,
            base_url=None,
            model=None,
            api_key=None,
        )
        second = load_or_create_exploration_brief(
            exploration=self.exploration,
            base_url=None,
            model=None,
            api_key=None,
        )

        self.assertEqual(first["key_finding"], second["key_finding"])
        self.assertEqual(ExplorationBrief.query.count(), 1)
        mock_generate.assert_called_once()

    def test_invalid_exploration_handling(self) -> None:
        result = load_or_create_exploration_brief(
            exploration={"available": False},
            base_url=None,
            model=None,
            api_key=None,
        )
        self.assertFalse(result["available"])

    @patch("app.health_dynamics.exploration_briefs.generate_exploration_brief")
    def test_fallback_brief_is_marked_passed(self, mock_generate) -> None:
        mock_generate.side_effect = RuntimeError("llm unavailable")

        result = load_or_create_exploration_brief(
            exploration=self.exploration,
            base_url=None,
            model=None,
            api_key=None,
        )

        self.assertEqual(result["generation_status"], "fallback")
        self.assertEqual(result["validation_status"], "passed")
        self.assertEqual(result["diagnostic_code"], "llm_unexpected_error")
        self.assertIn("unexpected error", result["diagnostic_message"])

    def test_legacy_failed_fallback_record_is_normalized(self) -> None:
        record = ExplorationBrief(
            year=2026,
            metric="BMI",
            dimension="age_group",
            pattern="persistent_abnormal",
            key_finding="Fallback key finding.",
            interpretation="Fallback interpretation.",
            suggested_next_investigation="Fallback next step.",
            model_name="deterministic-fallback",
            generation_status="fallback",
            validation_status="failed",
            diagnostic_code=None,
            diagnostic_message=None,
            data_signature="sig",
        )
        db.session.add(record)
        db.session.commit()

        serialized = serialize_exploration_brief_record(record)

        self.assertEqual(serialized["generation_status"], "fallback")
        self.assertEqual(serialized["validation_status"], "passed")
        self.assertEqual(serialized["diagnostic_code"], "llm_fallback_used")
        self.assertIn("fallback summary was used", serialized["diagnostic_message"])


if __name__ == "__main__":
    unittest.main()
