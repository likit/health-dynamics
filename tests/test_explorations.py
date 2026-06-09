import os
import tempfile
import unittest
from datetime import date

from app import create_app, db
from app.health_dynamics.explorations import build_exploration_actions, build_exploration_context
from app.models import DimDate, DimMeasure, DimPerson, FactHealthTrajectory, FactPersonCheckupSnapshot


class TestConfig:
    TESTING = True
    SQLALCHEMY_DATABASE_URI = ""
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = "test"


class ExplorationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        TestConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{self.db_path}"
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        previous_date = DimDate(date_key=20250115, checkup_year=2025, checkup_date=date(2025, 1, 15))
        latest_date = DimDate(date_key=20260115, checkup_year=2026, checkup_date=date(2026, 1, 15))
        db.session.add_all([previous_date, latest_date])

        measure = DimMeasure(measure_key=1, measure_code="BMI", measure_name="BMI", category="Anthropometric")
        db.session.add(measure)

        people = [
            DimPerson(person_key=1, cms_code="CMS001", employee_id="E001", full_name="Person A", department="Department A"),
            DimPerson(person_key=2, cms_code="CMS002", employee_id="E002", full_name="Person B", department="Department B"),
            DimPerson(person_key=3, cms_code="CMS003", employee_id="E003", full_name="Person C", department=None),
        ]
        db.session.add_all(people)

        snapshots = [
            FactPersonCheckupSnapshot(snapshot_key=1, person_key=1, date_key=20260115, age=34, bmi=31.0),
            FactPersonCheckupSnapshot(snapshot_key=2, person_key=2, date_key=20260115, age=47, bmi=29.5),
            FactPersonCheckupSnapshot(snapshot_key=3, person_key=3, date_key=20260115, age=29, bmi=24.0),
        ]
        db.session.add_all(snapshots)

        trajectories = [
            FactHealthTrajectory(
                trajectory_key=1,
                person_key=1,
                measure_key=1,
                from_date_key=20250115,
                to_date_key=20260115,
                previous_value=30.0,
                current_value=31.0,
                delta_value=1.0,
                percent_change=3.3,
                previous_status="abnormal",
                current_status="abnormal",
                status_transition="abnormal_to_abnormal",
                trajectory_class="stable",
                risk_direction="unchanged_risk",
                interpretation="Persistent abnormal BMI.",
            ),
            FactHealthTrajectory(
                trajectory_key=2,
                person_key=2,
                measure_key=1,
                from_date_key=20250115,
                to_date_key=20260115,
                previous_value=25.0,
                current_value=29.5,
                delta_value=4.5,
                percent_change=18.0,
                previous_status="normal",
                current_status="abnormal",
                status_transition="normal_to_abnormal",
                trajectory_class="worsening",
                risk_direction="higher_risk",
                interpretation="BMI worsened.",
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
                interpretation="BMI improved.",
            ),
        ]
        db.session.add_all(trajectories)
        db.session.commit()

    def tearDown(self) -> None:
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_exploration_links_generated_from_insight_packets(self) -> None:
        actions = build_exploration_actions(
            [
                {
                    "metric": "BMI",
                    "pattern": "persistent_abnormal",
                    "recommended_drilldown": ["department", "age_group"],
                }
            ],
            2026,
        )
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]["label"], "Explore BMI by Department")
        self.assertEqual(actions[1]["label"], "Explore BMI by Age Group")

    def test_exploration_route_loads_with_valid_parameters(self) -> None:
        response = self.client.get(
            "/dashboard/explore?year=2026&metric=BMI&dimension=department&pattern=persistent_abnormal"
        )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("BMI Exploration by Department", html)
        self.assertIn("Exploration Brief", html)
        self.assertIn("Department A", html)
        self.assertIn("Matched Employees", html)

    def test_exploration_route_handles_invalid_parameters_safely(self) -> None:
        response = self.client.get("/dashboard/explore?year=2026&dimension=department")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("The dashboard does not contain enough information to perform this exploration.", html)

    def test_grouped_results_contain_expected_fields(self) -> None:
        exploration = build_exploration_context(
            year=2026,
            metric="BMI",
            dimension="age_group",
            pattern="persistent_abnormal",
        )
        self.assertTrue(exploration["available"])
        first_row = exploration["results"][0]
        self.assertEqual(
            set(first_row),
            {"group", "paired_n", "persistent_abnormal_pct", "deteriorating_pct", "improving_pct"},
        )
        self.assertEqual(exploration["summary"]["total_matched_employees"], 3)


if __name__ == "__main__":
    unittest.main()
