import os

from alembic import context
from sqlalchemy import create_engine

from knowledgeops.config import Settings

url = os.environ.get("KO_MIGRATION_URL", Settings().database_url)
url = url.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(url)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
