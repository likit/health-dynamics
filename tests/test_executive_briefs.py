import os
import tempfile
import unittest
from unittest.mock import patch

from app import create_app, db
from app.health_dynamics.executive_briefs import (
    UNAVAILABLE_BRIEF,
    calculate_data_signature,
    load_or_create_executive_brief,
    serialize_brief_record,
)
from app.models import ExecutiveBrief


class TestConfig:
    TESTING = True
    SQLALCHEMY_DATABASE_URI = ""
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = "test"
    LOCAL_LLM_BASE_URL = "http://localhost:11434/v1"
    LOCAL_LLM_MODEL = "ministral-3:3b"
    LOCAL_LLM_API_KEY = "ollama"


class ExecutiveBriefPersistenceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        TestConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{self.db_path}"
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.base_yoy_brief = {
            "available": True,
            "latest_year": 2026,
            "previous_year": 2025,
            "matched_cohort_comparison": [
                {
                    "measure": "LDL",
                    "denominator_matched": 120,
                    "abnormal_percent_previous_matched": 18.0,
                    "abnormal_percent_latest_matched": 17.0,
                    "delta_percentage_points_matched": -1.0,
                },
                {
                    "measure": "BMI",
                    "denominator_matched": 140,
                    "abnormal_percent_previous_matched": 30.0,
                    "abnormal_percent_latest_matched": 33.0,
                    "delta_percentage_points_matched": 3.0,
                },
            ],
            "trajectory_measure_summary": [
                {
                    "metric": "LDL",
                    "persistent_abnormal_pct": 18.2,
                    "deteriorating_pct": 7.1,
                    "improving_pct": 5.4,
                    "paired_n": 120,
                },
                {
                    "metric": "BMI",
                    "persistent_abnormal_pct": 12.0,
                    "deteriorating_pct": 11.0,
                    "improving_pct": 3.0,
                    "paired_n": 140,
                },
            ],
        }

    def tearDown(self) -> None:
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    @patch("app.health_dynamics.executive_briefs.generate_dashboard_summary")
    def test_first_load_generates_and_saves_brief(self, mock_generate) -> None:
        mock_generate.return_value = (
            "Headline: Persistent BMI abnormality is the leading concern.\n\n"
            "Executive Summary: This analysis follows 140 employees who returned in both years.\n\n"
            "Why It Matters: This pattern requires attention.\n\n"
            "Suggested Next Exploration:\n- Review BMI patterns by Department.",
            "ministral-3:3b",
        )

        result = load_or_create_executive_brief(
            yoy_brief=self.base_yoy_brief,
            base_url=None,
            model=None,
            api_key=None,
        )

        self.assertTrue(result["available"])
        self.assertEqual(ExecutiveBrief.query.count(), 1)
        self.assertEqual(result["generation_status"], "generated")
        mock_generate.assert_called_once()

    @patch("app.health_dynamics.executive_briefs.generate_dashboard_summary")
    def test_second_load_reuses_saved_brief(self, mock_generate) -> None:
        mock_generate.return_value = (
            "Headline: Persistent BMI abnormality is the leading concern.\n\n"
            "Executive Summary: This analysis follows 140 employees who returned in both years.\n\n"
            "Why It Matters: This pattern requires attention.\n\n"
            "Suggested Next Exploration:\n- Review BMI patterns by Department.",
            "ministral-3:3b",
        )

        first = load_or_create_executive_brief(
            yoy_brief=self.base_yoy_brief,
            base_url=None,
            model=None,
            api_key=None,
        )
        second = load_or_create_executive_brief(
            yoy_brief=self.base_yoy_brief,
            base_url=None,
            model=None,
            api_key=None,
        )

        self.assertEqual(first["headline"], second["headline"])
        self.assertEqual(ExecutiveBrief.query.count(), 1)
        mock_generate.assert_called_once()

    @patch("app.health_dynamics.executive_briefs.generate_dashboard_summary")
    def test_changed_data_signature_triggers_regeneration(self, mock_generate) -> None:
        mock_generate.side_effect = [
            (
                "Headline: First brief.\n\nExecutive Summary: First.\n\nWhy It Matters: First.\n\nSuggested Next Exploration:\n- One",
                "ministral-3:3b",
            ),
            (
                "Headline: Second brief.\n\nExecutive Summary: Second.\n\nWhy It Matters: Second.\n\nSuggested Next Exploration:\n- Two",
                "ministral-3:3b",
            ),
        ]

        first = load_or_create_executive_brief(
            yoy_brief=self.base_yoy_brief,
            base_url=None,
            model=None,
            api_key=None,
        )

        changed_brief = dict(self.base_yoy_brief)
        changed_brief["trajectory_measure_summary"] = list(self.base_yoy_brief["trajectory_measure_summary"])
        changed_brief["trajectory_measure_summary"][1] = {
            **changed_brief["trajectory_measure_summary"][1],
            "persistent_abnormal_pct": 22.0,
        }

        second = load_or_create_executive_brief(
            yoy_brief=changed_brief,
            base_url=None,
            model=None,
            api_key=None,
        )

        self.assertNotEqual(first["headline"], second["headline"])
        self.assertEqual(ExecutiveBrief.query.count(), 1)
        self.assertEqual(mock_generate.call_count, 2)

    @patch("app.health_dynamics.executive_briefs.generate_dashboard_summary")
    def test_failed_validation_uses_fallback(self, mock_generate) -> None:
        mock_generate.return_value = (
            "Headline: Persistent BMI abnormality is the leading concern.\n\n"
            "Executive Summary: This analysis follows 140 employees who returned in both years.\n\n"
            "Why It Matters: This pattern requires attention.\n\n"
            "Suggested Next Exploration:\n- Review BMI patterns by Department.",
            "ministral-3:3b (fallback)",
        )

        result = load_or_create_executive_brief(
            yoy_brief=self.base_yoy_brief,
            base_url=None,
            model=None,
            api_key=None,
        )

        self.assertEqual(result["generation_status"], "fallback")
        self.assertEqual(result["validation_status"], "passed")
        self.assertEqual(result["diagnostic_code"], "llm_validation_failed")
        self.assertIn("failed validation checks", result["diagnostic_message"])

    @patch("app.health_dynamics.executive_briefs.generate_dashboard_summary")
    def test_runtime_failure_uses_passed_fallback(self, mock_generate) -> None:
        mock_generate.side_effect = RuntimeError("llm unavailable")

        result = load_or_create_executive_brief(
            yoy_brief=self.base_yoy_brief,
            base_url=None,
            model=None,
            api_key=None,
        )

        self.assertEqual(result["generation_status"], "fallback")
        self.assertEqual(result["validation_status"], "passed")
        self.assertEqual(result["diagnostic_code"], "llm_unexpected_error")
        self.assertIn("unexpected error", result["diagnostic_message"])

    def test_legacy_failed_fallback_record_is_normalized(self) -> None:
        record = ExecutiveBrief(
            organization_id=None,
            year=2026,
            brief_type="executive_dashboard",
            headline="Fallback headline.",
            executive_summary="Fallback summary.",
            why_it_matters="Fallback importance.",
            suggested_next_exploration="[]",
            insight_packets_json="[]",
            model_name="deterministic-fallback",
            generation_status="fallback",
            validation_status="failed",
            diagnostic_code=None,
            diagnostic_message=None,
            data_signature="sig",
        )
        db.session.add(record)
        db.session.commit()

        serialized = serialize_brief_record(record)

        self.assertEqual(serialized["generation_status"], "fallback")
        self.assertEqual(serialized["validation_status"], "passed")
        self.assertEqual(serialized["diagnostic_code"], "llm_fallback_used")
        self.assertIn("fallback summary was used", serialized["diagnostic_message"])

    def test_missing_insight_packets_displays_unavailable(self) -> None:
        unavailable = load_or_create_executive_brief(
            yoy_brief={
                "available": True,
                "latest_year": 2026,
                "previous_year": 2025,
                "matched_cohort_comparison": [],
                "trajectory_measure_summary": [],
            },
            base_url=None,
            model=None,
            api_key=None,
        )
        self.assertEqual(unavailable["headline"], UNAVAILABLE_BRIEF["headline"])


if __name__ == "__main__":
    unittest.main()
