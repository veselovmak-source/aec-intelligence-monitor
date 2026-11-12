import unittest
from src.monitor import tracked, classify

class MonitorClassificationTests(unittest.TestCase):
    def test_skill_file_is_tracked(self):
        self.assertTrue(tracked("agents/SKILL.md"))

    def test_prompt_extension_is_tracked(self):
        self.assertTrue(tracked("prompts/review.prompt"))

    def test_binary_extension_is_not_tracked(self):
        self.assertFalse(tracked("models/plan.dwg"))

    def test_skill_classification(self):
        self.assertIn("Agent Skill", classify("skills/SKILL.md", "# Instructions"))

    def test_prompt_classification(self):
        self.assertIn("Промпт", classify("prompts/checklist.md", "Review the drawing"))

if __name__ == "__main__":
    unittest.main()
