import logging
import signal
import time

from knowledgeops.config import Settings
from knowledgeops.db import Database
from knowledgeops.ingest_worker import tick_ingestion
from knowledgeops.jobs import JobError, claim, cleanup, fail, process
from knowledgeops.worker_health import mark_ready

logger = logging.getLogger("knowledgeops.worker")


def tick(database, settings):
    with database.connection() as conn:
        run = claim(conn, settings)
    if not run:
        return False
    try:
        process(database, run, settings)
    except Exception as exc:
        code = exc.code if isinstance(exc, JobError) else "PROCESSING_ERROR"
        logger.warning("run_failure code=%s", code)
        with database.connection() as conn:
            fail(conn, run, settings, code)
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    database = Database(settings.database_url)
    database.open()
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_cleanup = 0.0
    try:
        while not stopping:
            try:
                if time.monotonic() - last_cleanup > 15:
                    with database.connection() as conn:
                        cleanup(conn)
                    last_cleanup = time.monotonic()
                ingestion_work = tick_ingestion(database)
                had_work = tick(database, settings)
                mark_ready()
                if not had_work and not ingestion_work:
                    time.sleep(settings.worker_poll_seconds)
            except Exception:
                logger.warning("worker_dependency_unavailable")
                time.sleep(1)
    finally:
        database.close()


if __name__ == "__main__":
    main()
