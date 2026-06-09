import unittest

from app.health_dynamics.insight_rules import (
    build_fallback_executive_brief,
    build_verified_insight_packets,
    humanize_drilldown_labels,
    select_top_priority_insights,
    validate_generated_summary,
)


class InsightRulesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.matched_rows = [
            {
                "measure": "LDL",
                "denominator_matched": 90,
                "abnormal_percent_previous_matched": 18.0,
                "abnormal_percent_latest_matched": 17.0,
                "delta_percentage_points_matched": -1.0,
            },
            {
                "measure": "Gluc",
                "denominator_matched": 220,
                "abnormal_percent_previous_matched": 8.0,
                "abnormal_percent_latest_matched": 11.0,
                "delta_percentage_points_matched": 3.0,
            },
            {
                "measure": "BMI",
                "denominator_matched": 180,
                "abnormal_percent_previous_matched": 25.0,
                "abnormal_percent_latest_matched": 26.0,
                "delta_percentage_points_matched": 1.0,
            },
        ]
        self.trajectory_rows = [
            {
                "metric": "LDL",
                "persistent_abnormal_pct": 18.2,
                "deteriorating_pct": 7.1,
                "improving_pct": 5.4,
                "paired_n": 90,
            },
            {
                "metric": "Gluc",
                "persistent_abnormal_pct": 9.0,
                "deteriorating_pct": 13.0,
                "improving_pct": 4.0,
                "paired_n": 220,
            },
            {
                "metric": "BMI",
                "persistent_abnormal_pct": 10.0,
                "deteriorating_pct": 8.0,
                "improving_pct": 2.0,
                "paired_n": 180,
            },
        ]

    def test_build_verified_insight_packets(self) -> None:
        packets = build_verified_insight_packets(
            matched_cohort_comparison=self.matched_rows,
            trajectory_measure_summary=self.trajectory_rows,
        )
        ldl_packet = next(packet for packet in packets if packet["metric"] == "LDL")
        self.assertEqual(ldl_packet["pattern"], "persistent_abnormal")
        self.assertEqual(ldl_packet["severity"], "high")
        self.assertIn("Persistent LDL abnormality", ldl_packet["message"])

    def test_priority_ranking(self) -> None:
        packets = build_verified_insight_packets(
            matched_cohort_comparison=self.matched_rows,
            trajectory_measure_summary=self.trajectory_rows,
        )
        top_packets = select_top_priority_insights(packets, limit=2)
        self.assertEqual(top_packets[0]["metric"], "LDL")
        self.assertGreaterEqual(top_packets[0]["priority_score"], top_packets[1]["priority_score"])

    def test_denominator_caveat(self) -> None:
        packets = build_verified_insight_packets(
            matched_cohort_comparison=self.matched_rows,
            trajectory_measure_summary=self.trajectory_rows,
        )
        ldl_packet = next(packet for packet in packets if packet["metric"] == "LDL")
        self.assertEqual(ldl_packet["caveat"], "Matched denominator is limited; interpretation should be cautious.")

    def test_forbidden_term_validation(self) -> None:
        packets = build_verified_insight_packets(
            matched_cohort_comparison=self.matched_rows,
            trajectory_measure_summary=self.trajectory_rows,
        )
        ldl_packet = next(packet for packet in packets if packet["metric"] == "LDL")
        is_valid, issues = validate_generated_summary(
            "Headline: LDL is worsening across the organization.",
            [ldl_packet],
        )
        self.assertFalse(is_valid)
        self.assertTrue(any("worsening" in issue for issue in issues))

    def test_fallback_brief_generation(self) -> None:
        packets = build_verified_insight_packets(
            matched_cohort_comparison=self.matched_rows,
            trajectory_measure_summary=self.trajectory_rows,
        )
        brief = build_fallback_executive_brief(packets[:1])
        self.assertIn("Headline:", brief)
        self.assertIn("Suggested Next Exploration:", brief)
        self.assertNotIn("worsening", brief.lower())
        self.assertIn("Executive Summary:", brief)
        self.assertIn("remained in an abnormal category", brief)
        self.assertIn("Analysis of 90 employees", brief)
        self.assertIn("ongoing workforce health concern", brief)
        self.assertIn("Which departments account for most persistent LDL concerns?", brief)
        self.assertIn("specific age groups", brief)
        self.assertNotIn("age_group", brief)

    def test_humanize_drilldown_labels(self) -> None:
        labels = humanize_drilldown_labels(["department", "age_group", "sex"])
        self.assertEqual(labels, ["Department", "Age Group", "Sex"])


if __name__ == "__main__":
    unittest.main()
