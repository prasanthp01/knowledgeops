import os

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from knowledgeops.api import create_app
from knowledgeops.config import ROOT, Settings
from knowledgeops.db import Database
from knowledgeops.ingestion import load_corpus


@pytest.fixture(scope="session")
def test_urls():
    runtime = os.environ.get("KO_TEST_DATABASE_URL", "")
    owner = os.environ.get("KO_TEST_MIGRATION_URL", "")
    if not runtime or not owner:
        pytest.fail(
            "Set KO_TEST_DATABASE_URL and KO_TEST_MIGRATION_URL to isolated *_test databases"
        )
    from psycopg.conninfo import conninfo_to_dict

    if not all(conninfo_to_dict(url)["dbname"].endswith("_test") for url in [runtime, owner]):
        pytest.fail("Refusing non-test database")
    os.environ["KO_MIGRATION_URL"] = owner
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    with psycopg.connect(owner) as conn:
        conn.execute((ROOT / "operations/grant-runtime.sql").read_text())
    return runtime, owner


@pytest.fixture
def db(test_urls):
    runtime, owner = test_urls
    with psycopg.connect(owner, row_factory=dict_row) as conn:
        conn.execute(
            "TRUNCATE documents,sessions,ingest_audit,rate_buckets,inference_ledger,ingestion_jobs RESTART IDENTITY CASCADE"
        )
        load_corpus(conn, ROOT / "data/corpus.json")
    database = Database(runtime)
    database.open()
    yield database
    database.close()


@pytest.fixture
def settings(test_urls):
    return Settings(
        database_url=test_urls[0], public_origin="http://testserver", runs_per_minute=120
    )


@pytest.fixture
def app(db, settings):
    return create_app(settings, db)


@pytest.fixture
def client(app):
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        response = client.post("/api/projects/knowledgeops/session", json={})
        assert response.status_code == 201
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield client
