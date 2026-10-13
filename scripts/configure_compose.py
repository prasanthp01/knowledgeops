"""Create local-only generated passwords without printing them or replacing existing files."""

import os
import secrets
from pathlib import Path

path = Path(__file__).resolve().parents[1] / ".env.compose"
text = "\n".join(
    f"{name}={secrets.token_hex(24)}"
    for name in ["POSTGRES_PASSWORD", "OWNER_PASSWORD", "APP_PASSWORD"]
)
text += "\nKO_PUBLIC_ORIGIN=http://127.0.0.1:8045\nKO_HOST_PORT=8045\nKO_SECURE_COOKIES=false\n"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write(text)
print("Created .env.compose with generated local-only credentials. Keep it private.")
