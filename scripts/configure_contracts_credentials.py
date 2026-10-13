"""Create development-only synthetic service grants, without printing credentials."""

import argparse
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = [args.directory / name for name in ["server.json", "probe-tokens.json"]]
    if any(path.exists() for path in paths):
        raise SystemExit("Existing credentials preserved; use a new private directory to rotate.")
    tokens, grants = {}, []
    for name, app, role, scopes in [
        ("resolveai_staff", "resolveai", "staff", ["knowledge:read"]),
        ("flowops_staff", "flowops", "staff", ["knowledge:read"]),
        ("resolveai_finance", "resolveai", "finance", ["knowledge:read"]),
        ("denied_fixture", "resolveai", "staff", []),
    ]:
        token = secrets.token_urlsafe(32)
        tokens[name] = token
        grants.append(
            {
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "application_id": app,
                "tenant_id": "demo",
                "environment": "development",
                "audience": "knowledgeops",
                "role": role,
                "collection_ids": ["people", "operations", "security", "finance"],
                "scopes": scopes,
                "expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
                "synthetic_only": True,
            }
        )
    for path, data in zip(paths, [grants, tokens], strict=True):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream)
    print("Created four 24-hour development grants for the synthetic demo tenant; files mode 0600.")


if __name__ == "__main__":
    main()
