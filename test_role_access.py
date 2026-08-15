import unittest

import role_access


class RoleAccessTests(unittest.TestCase):
    def test_valid_student_identity_can_only_access_self(self) -> None:
        result = role_access.verify_demo_identity("S004-DEMO", role_access.DEMO_CODE)
        self.assertTrue(result["verified"])
        session = role_access.require_role_session(
            result["session_token"], ("student",), target_student_id="S004"
        )
        self.assertEqual(session["role"], "student")
        with self.assertRaises(PermissionError):
            role_access.require_role_session(
                result["session_token"], ("student",), target_student_id="S002"
            )

    def test_head_teacher_is_limited_to_authorized_class(self) -> None:
        result = role_access.verify_demo_identity("HT-S604124", role_access.DEMO_CODE)
        role_access.require_role_session(
            result["session_token"], ("head_teacher",), target_class_name="S604124移动"
        )
        with self.assertRaises(PermissionError):
            role_access.require_role_session(
                result["session_token"], ("head_teacher",), target_class_name="电商2403"
            )

    def test_invalid_code_does_not_issue_token(self) -> None:
        result = role_access.verify_demo_identity("CO-DIGITAL", "wrong-code")
        self.assertFalse(result["verified"])
        self.assertNotIn("session_token", result)


if __name__ == "__main__":
    unittest.main()
