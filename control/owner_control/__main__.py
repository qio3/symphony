from __future__ import annotations

import threading
import time

from .actions import ActionService
from .ai import CodexReadOnly
from .bot import NotificationPublisher, TelegramApi, TelegramBot
from .clients import GitHubClient, SymphonyClient, TestEnvironmentClient
from .config import Config
from .http_server import create_server
from .runtime import SnapshotService
from .state_store import StateStore
from .supervisor import DockerComposeSupervisor
from .telegram import NotificationDetector, TelegramCommandHandler


def _publish_notifications_once(notifications: NotificationPublisher, snapshots: SnapshotService) -> None:
    notifications.publish(snapshots.snapshot())


def main() -> None:
    config = Config.from_env()
    store = StateStore(config.state_path)
    supervisor = DockerComposeSupervisor(
        compose_file=config.compose_file,
        container_name=config.container_name,
        service_name=config.compose_service,
    )
    github = GitHubClient(
        token=config.github_token,
        repository=config.github_repository,
        project_id=config.github_project_id,
        mutation_logger=store.append_mutation,
    )
    snapshots = SnapshotService(
        symphony=SymphonyClient(config.symphony_url),
        github=github,
        test_environment=TestEnvironmentClient(config.test_health_url, config.test_url),
        supervisor=supervisor,
        state_store=store,
        worker_limit=config.worker_limit,
        canonical_ref=config.canonical_ref,
    )
    actions = ActionService(
        snapshot_provider=snapshots.cached_snapshot,
        fresh_snapshot_provider=lambda: snapshots.snapshot(fresh=True),
        lifecycle=github,
        supervisor=supervisor,
        state_store=store,
        after_action=snapshots.invalidate,
    )
    telegram_api = TelegramApi(token=config.telegram_token, owner_chat_id=config.owner_chat_id)
    handler = TelegramCommandHandler(
        snapshot_provider=snapshots.snapshot,
        action_service=actions,
        logs_provider=supervisor.logs,
        ai=CodexReadOnly(
            executable=[config.codex_executable],
            codex_home=config.codex_home,
        ),
    )
    bot = TelegramBot(
        api=telegram_api,
        handler=handler,
        state_store=store,
        owner_chat_id=config.owner_chat_id,
        owner_user_id=config.owner_user_id,
    )
    notifications = NotificationPublisher(
        api=telegram_api,
        state_store=store,
        detector=NotificationDetector(),
    )
    server = create_server(
        (config.bind_host, config.bind_port),
        token=config.control_token,
        snapshot_provider=snapshots.snapshot,
        intake_provider=lambda: bool(snapshots.snapshot().get("intake", {}).get("active")),
        action_service=actions,
        logs_provider=supervisor.logs,
        runtime_diagnostics_url=config.symphony_url.rstrip("/") + "/",
    )
    server_thread = threading.Thread(target=server.serve_forever, name="owner-control-http", daemon=True)
    server_thread.start()
    try:
        while True:
            try:
                bot.poll_once(timeout=10)
                _publish_notifications_once(notifications, snapshots)
            except Exception:
                time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
