"""Allowlisted local document parsing in a resource-bounded subprocess."""

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import cast

MAX_BYTES = 262144


def parse_bytes(data: bytes, suffix: str) -> list[dict]:
    if not data or len(data) > MAX_BYTES:
        raise ValueError("DOCUMENT_SIZE_LIMIT")
    if suffix in {".txt", ".md"}:
        text = data.decode("utf-8", errors="strict").strip()
        if not text or "\x00" in text or len(text) > 60000:
            raise ValueError("INVALID_TEXT")
        # Preserve a bounded section size; chunking happens during indexing.
        return [
            {"heading": f"Text segment {i // 10000 + 1}", "text": text[i : i + 10000], "page": 1}
            for i in range(0, len(text), 10000)
        ]
    if suffix != ".pdf":
        raise ValueError("UNSUPPORTED_FORMAT")
    from pypdf import PdfReader
    from pypdf.generic import DictionaryObject

    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted or len(reader.pages) > 20:
        raise ValueError("UNSAFE_PDF")
    root = cast(DictionaryObject, reader.trailer["/Root"])
    if any(key in root for key in ["/OpenAction", "/AA"]) or any(
        key in root.get("/Names", {}) for key in ["/JavaScript", "/EmbeddedFiles"]
    ):
        raise ValueError("ACTIVE_PDF_REJECTED")
    sections, total = [], 0
    for i, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        total += len(text)
        if len(text) > 12000 or total > 60000:
            raise ValueError("EXTRACTED_TEXT_LIMIT")
        if text.strip():
            sections.append({"heading": f"Page {i}", "text": text.strip(), "page": i})
    if not sections:
        raise ValueError("NO_EXTRACTABLE_TEXT")
    return sections


def parse_local(path: Path) -> list[dict]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("UNAPPROVED_LOCAL_FILE")
    suffix = path.suffix.lower()
    if suffix not in {".txt", ".md", ".pdf"}:
        raise ValueError("UNSUPPORTED_FORMAT")
    data = path.read_bytes()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "knowledgeops.parsers", "--worker", suffix],
            input=data,
            capture_output=True,
            timeout=8,
            check=True,
        )
        if len(result.stdout) > 100000:
            raise ValueError("PARSER_OUTPUT_LIMIT")
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        raise ValueError("DOCUMENT_PARSE_FAILED") from None


if __name__ == "__main__":
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    try:
        result = parse_bytes(sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[2])
        sys.stdout.write(json.dumps(result))
    except Exception:
        sys.exit(2)
