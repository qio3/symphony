import tempfile
import unittest
from pathlib import Path

from owner_control.config import Config, ConfigError


class ConfigTest(unittest.TestCase):
    def test_requires_long_control_token_and_loopback_by_default(self):
        env = valid_env()
        env["SYMPHONY_CONTROL_TOKEN"] = "short"
        with self.assertRaisesRegex(ConfigError, "32"):
            Config.from_env(env)

        env = valid_env()
        env["SYMPHONY_CONTROL_BIND"] = "0.0.0.0"
        with self.assertRaisesRegex(ConfigError, "non-loopback"):
            Config.from_env(env)

    def test_secrets_and_state_are_external_configuration(self):
        config = Config.from_env(valid_env())

        self.assertEqual(config.owner_chat_id, 10)
        self.assertEqual(config.owner_user_id, 20)
        self.assertTrue(config.state_path.is_absolute())
        self.assertEqual(config.bind_host, "127.0.0.1")
        self.assertEqual(config.bind_port, 4080)


def valid_env():
    root = Path(tempfile.gettempdir()).resolve()
    return {
        "SYMPHONY_CONTROL_TOKEN": "c" * 32,
        "SYMPHONY_CONTROL_STATE_PATH": str(root / "symphony-control-test" / "state.json"),
        "SYMPHONY_COMPOSE_FILE": str(root / "symphony-control-test" / "docker-compose.yml"),
        "SYMPHONY_URL": "http://127.0.0.1:4080",
        "SYMPHONY_CONTAINER": "zavod-symphony",
        "SYMPHONY_COMPOSE_SERVICE": "symphony",
        "SYMPHONY_WORKER_LIMIT": "2",
        "GITHUB_TOKEN": "github-token",
        "GITHUB_REPOSITORY": "qio3/zavod",
        "GITHUB_PROJECT_ID": "project-id",
        "GITHUB_CANONICAL_REF": "rebrand/stanina",
        "TEST_HEALTH_URL": "https://test.example/api/health",
        "TEST_URL": "https://test.example",
        "TELEGRAM_BOT_TOKEN": "telegram-token",
        "TELEGRAM_OWNER_CHAT_ID": "10",
        "TELEGRAM_OWNER_USER_ID": "20",
        "CODEX_HOME": str(root / "codex-home"),
    }


if __name__ == "__main__":
    unittest.main()
