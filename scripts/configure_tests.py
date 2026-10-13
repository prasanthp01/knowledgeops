"""Create private configuration for the isolated local test database."""

import argparse
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=55446)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    path = Path(__file__).resolve().parents[1] / ".env.test"
    if path.exists():
        print("Existing .env.test preserved. Reuse it with the same test database volume.")
        return
    owner, runtime = secrets.token_hex(24), secrets.token_hex(24)
    values = {
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "OWNER_PASSWORD": owner,
        "APP_PASSWORD": runtime,
        "KO_TEST_PORT": str(args.port),
        "KO_TEST_MIGRATION_URL": f"postgresql://knowledgeops_owner:{owner}@127.0.0.1:{args.port}/knowledgeops_test",
        "KO_TEST_DATABASE_URL": f"postgresql://knowledgeops_app:{runtime}@127.0.0.1:{args.port}/knowledgeops_test",
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
    print("Created .env.test for an isolated knowledgeops_test database. No credentials printed.")


if __name__ == "__main__":
    main()
