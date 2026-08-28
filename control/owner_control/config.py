from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    bind_host: str
    bind_port: int
    control_token: str
    state_path: Path
    symphony_url: str
    compose_file: Path
    container_name: str
    compose_service: str
    worker_limit: int
    github_token: str
    github_repository: str
    github_project_id: str
    canonical_ref: str
    test_health_url: str
    test_url: str
    telegram_token: str
    owner_chat_id: int
    owner_user_id: int
    codex_home: Path
    codex_executable: str
    infrastructure_config_path: Path | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Config":
        token = _required(env, "SYMPHONY_CONTROL_TOKEN")
        if len(token) < 32:
            raise ConfigError("SYMPHONY_CONTROL_TOKEN must contain at least 32 characters")
        bind_host = env.get("SYMPHONY_CONTROL_BIND", "127.0.0.1")
        allow_non_loopback = env.get("SYMPHONY_CONTROL_ALLOW_NON_LOOPBACK") == "1"
        try:
            is_loopback = ipaddress.ip_address(bind_host).is_loopback
        except ValueError as error:
            raise ConfigError("SYMPHONY_CONTROL_BIND must be an IP address") from error
        if not is_loopback and not allow_non_loopback:
            raise ConfigError("non-loopback control binding requires SYMPHONY_CONTROL_ALLOW_NON_LOOPBACK=1")

        state_path = _absolute_path(env, "SYMPHONY_CONTROL_STATE_PATH")
        compose_file = _absolute_path(env, "SYMPHONY_COMPOSE_FILE")
        codex_home = _absolute_path(env, "CODEX_HOME")
        worker_limit = _positive_int(env, "SYMPHONY_WORKER_LIMIT")
        return cls(
            bind_host=bind_host,
            bind_port=_port(env.get("SYMPHONY_CONTROL_PORT", "4080")),
            control_token=token,
            state_path=state_path,
            symphony_url=_required(env, "SYMPHONY_URL"),
            compose_file=compose_file,
            container_name=_required(env, "SYMPHONY_CONTAINER"),
            compose_service=_required(env, "SYMPHONY_COMPOSE_SERVICE"),
            worker_limit=worker_limit,
            github_token=env.get("GITHUB_TOKEN") or _required(env, "SYMPHONY_GITHUB_TOKEN"),
            github_repository=_required(env, "GITHUB_REPOSITORY"),
            github_project_id=_required(env, "GITHUB_PROJECT_ID"),
            canonical_ref=_required(env, "GITHUB_CANONICAL_REF"),
            test_health_url=_required(env, "TEST_HEALTH_URL"),
            test_url=_required(env, "TEST_URL"),
            telegram_token=_required(env, "TELEGRAM_BOT_TOKEN"),
            owner_chat_id=_integer(env, "TELEGRAM_OWNER_CHAT_ID"),
            owner_user_id=_integer(env, "TELEGRAM_OWNER_USER_ID"),
            codex_home=codex_home,
            codex_executable=env.get("CODEX_EXECUTABLE", "codex"),
            infrastructure_config_path=_optional_absolute_path(
                env, "SYMPHONY_INFRASTRUCTURE_CONFIG_PATH"
            ),
        )


def _required(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _absolute_path(env: Mapping[str, str], name: str) -> Path:
    path = Path(_required(env, name))
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    return path


def _optional_absolute_path(env: Mapping[str, str], name: str) -> Path | None:
    value = str(env.get(name) or "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    return path


def _integer(env: Mapping[str, str], name: str) -> int:
    try:
        return int(_required(env, name))
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer") from error


def _positive_int(env: Mapping[str, str], name: str) -> int:
    value = _integer(env, name)
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise ConfigError("SYMPHONY_CONTROL_PORT must be an integer") from error
    if port < 1 or port > 65535:
        raise ConfigError("SYMPHONY_CONTROL_PORT must be between 1 and 65535")
    return port
