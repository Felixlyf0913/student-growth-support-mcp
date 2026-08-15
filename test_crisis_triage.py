import unittest

import student_management_mcp_sse as service


class CrisisTriageTests(unittest.TestCase):
    def test_explicit_crisis_signal_uses_offline_verification_lane(self) -> None:
        profile = service.get_student_profile("DEMO-AI2501-01")

        self.assertEqual(profile["computed_attention_level"], "需立即线下核实")
        self.assertTrue(
            any("立即线下核实" in action for action in profile["suggested_actions"])
        )
        self.assertTrue(
            any("不作心理诊断" in action for action in profile["suggested_actions"])
        )

    def test_class_dashboard_counts_crisis_signal_separately(self) -> None:
        dashboard = service.get_class_dashboard("人工智能2501（演示）")

        self.assertEqual(dashboard["student_count"], 2)
        self.assertEqual(dashboard["attention_levels"]["需立即线下核实"], 1)
        self.assertEqual(dashboard["attention_levels"]["高关注"], 1)
        self.assertEqual(dashboard["top_attention_students"][0]["student_id"], "DEMO-AI2501-01")


if __name__ == "__main__":
    unittest.main()
