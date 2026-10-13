import argparse
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from knowledgeops.config import ROOT, Settings
from knowledgeops.ingest_worker import enqueue_document
from knowledgeops.ingestion import load_corpus, revoke
from knowledgeops.models import Document
from knowledgeops.parsers import parse_local


def main():
    parser = argparse.ArgumentParser(description="KnowledgeOps operator tools")
    parser.add_argument("command", choices=["seed", "revoke", "status", "enqueue"])
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--document-id")
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    with psycopg.connect(Settings().database_url, row_factory=dict_row) as conn:
        if args.command == "seed":
            result = load_corpus(conn, ROOT / "data/corpus.json", args.tenant)
        elif args.command == "revoke":
            if not args.document_id:
                parser.error("--document-id is required")
            result = {"revoked_versions": revoke(conn, args.tenant, args.document_id)}
        elif args.command == "enqueue":
            if not args.metadata or not args.source:
                parser.error("--metadata and --source are required")
            metadata = json.loads(args.metadata.read_text())
            metadata["sections"] = parse_local(args.source)
            row = enqueue_document(conn, args.tenant, Document.model_validate(metadata))
            result = {"id": str(row["id"]), "status": row["status"]}
        else:
            result = {
                "documents": conn.execute(
                    "SELECT lifecycle,count(*) AS count FROM documents WHERE tenant_id=%s GROUP BY lifecycle",
                    (args.tenant,),
                ).fetchall()
            }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
