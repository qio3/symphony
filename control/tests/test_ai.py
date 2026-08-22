import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from owner_control.ai import CodexReadOnly


class CodexReadOnlyTest(unittest.TestCase):
    def test_returns_structured_answer_without_forwarding_api_or_control_secrets(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                textwrap.dedent(
                    """
                    import json
                    import os
                    import sys

                    assert "OPENAI_API_KEY" not in os.environ
                    assert "TELEGRAM_BOT_TOKEN" not in os.environ
                    prompt = sys.stdin.read()
                    assert "why is #401 slow?" in prompt
                    output = sys.argv[sys.argv.index("-o") + 1]
                    with open(output, "w", encoding="utf-8") as handle:
                        json.dump({"answer": "#401 has been running for 15 minutes."}, handle)
                    """
                ),
                encoding="utf-8",
            )
            previous_api_key = os.environ.get("OPENAI_API_KEY")
            previous_telegram = os.environ.get("TELEGRAM_BOT_TOKEN")
            os.environ["OPENAI_API_KEY"] = "must-not-leak"
            os.environ["TELEGRAM_BOT_TOKEN"] = "must-not-leak"
            self.addCleanup(self._restore_env, "OPENAI_API_KEY", previous_api_key)
            self.addCleanup(self._restore_env, "TELEGRAM_BOT_TOKEN", previous_telegram)

            adapter = CodexReadOnly(
                executable=[sys.executable, str(fake_codex)],
                codex_home=root,
                timeout_seconds=5,
            )
            answer = adapter.answer("why is #401 slow?", {"counts": {"running": 1}})

            self.assertEqual(answer, "#401 has been running for 15 minutes.")

    @staticmethod
    def _restore_env(name, value):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
