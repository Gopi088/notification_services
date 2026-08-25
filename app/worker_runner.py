"""
Worker process entry point.

Usage:
    python3 -m app.worker_runner whatsapp          # one channel
    python3 -m app.worker_runner whatsapp --worker-id w1
    python3 -m app.worker_runner --retry           # retry stream consumer
"""
import logging
import sys

from app.config import get_settings
from app.logging_config import configure_logging


def main() -> int:
    configure_logging()
    args = sys.argv[1:]
    if not args:
        print("usage: python3 -m app.worker_runner <channel|--retry> [--worker-id id]")
        return 2

    get_settings()  # load config early for logging

    from app.storage import get_storage

    get_storage()  # connect (schema for postgres is created by migration step)

    # Ensure schema is migrated before this worker starts consuming. Advisory
    # lock makes concurrent api/worker startups safe (idempotent, no race).
    if get_settings().STORAGE_BACKEND == "postgres":
        from app.migrate import up as run_migrations

        run_migrations()

    if args[0] == "--retry":
        from app.worker import run_retry_worker

        run_retry_worker()
        return 0

    channel = args[0]
    worker_id = None
    if "--worker-id" in args:
        worker_id = args[args.index("--worker-id") + 1]

    from app.worker import run_worker

    run_worker(channel, worker_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())