import unittest

from xau_agent.broker import safe_comment


class SafeCommentTests(unittest.TestCase):
    def test_strips_invalid_chars_and_length(self):
        self.assertEqual(safe_comment("xau-agent manual-pipeline-test"), "xau_agent_manual_pipeline")
        self.assertEqual(safe_comment("ทอง"), "___")
        self.assertEqual(safe_comment(""), "xau")
        self.assertLessEqual(len(safe_comment("x" * 100)), 25)


if __name__ == "__main__":
    unittest.main()
