import psycopg
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row

from knowledgeops.config import ROOT, Settings
from knowledgeops.ingestion import load_corpus

command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
with psycopg.connect(Settings().database_url, row_factory=dict_row) as conn:
    conn.execute((ROOT / "operations/grant-runtime.sql").read_text())
    status = load_corpus(conn, ROOT / "data/corpus.json")
print("Schema and original synthetic corpus ready:", status)
