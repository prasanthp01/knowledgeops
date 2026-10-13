"""Verify and vendor the immutable locally released Contracts 1.0.0 snapshot."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from knowledgeops.config import ROOT

RELEASE = "b55d1a9dc5d2bab23817b984b9801ccff7304f04"
TESTED = "02dacbc396189eb72809cfa228efc548d7e48925"


def require(condition, label):
    if not condition:
        raise ValueError("Contracts verification failed: " + label)


def run():
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, required=True)
    args = p.parse_args()
    source = args.source.resolve()
    require(
        subprocess.check_output(["git", "rev-parse", "v1.0.0"], cwd=source, text=True).strip()
        == RELEASE,
        "release tag",
    )
    manifest = json.loads((source / "contracts-manifest.json").read_text())
    require(manifest["release_source_commit"] == TESTED, "tested source")
    require(
        (source / "contracts-manifest.json").read_bytes()
        == subprocess.check_output(
            ["git", "show", RELEASE + ":contracts-manifest.json"], cwd=source
        ),
        "manifest release contents",
    )
    checked = {**manifest["files"], **manifest["test_reports"]}
    for path, expected in checked.items():
        data = (source / path).read_bytes()
        require(hashlib.sha256(data).hexdigest() == expected, path)
        require(
            data == subprocess.check_output(["git", "show", RELEASE + ":" + path], cwd=source), path
        )
    target = ROOT / "knowledgeops/vendor_contracts"
    target.mkdir(exist_ok=True)
    (target / "__init__.py").write_text(
        '"""Unmodified Contracts 1.0.0 validation code and schemas; see LICENSE."""\n'
    )
    copied = {}
    for path in list(manifest["files"]) + ["LICENSE"]:
        if path.startswith("schemas/v1/"):
            dest = target / path
        elif path == "src/ecosystem_contracts/validation.py":
            dest = target / "validation.py"
        elif path == "LICENSE":
            dest = target / "LICENSE"
        elif path == "openapi/ecosystem-v1.json":
            dest = ROOT / "schemas/ecosystem-v1.json"
        else:
            continue
        require(
            (source / path).read_bytes()
            == subprocess.check_output(["git", "show", RELEASE + ":" + path], cwd=source),
            path,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / path, dest)
        copied[str(dest.relative_to(ROOT))] = hashlib.sha256(dest.read_bytes()).hexdigest()
    for kind in ["valid", "invalid"]:
        for path in sorted((source / "examples" / kind).glob("*.json")):
            data = path.read_bytes()
            require(
                data
                == subprocess.check_output(
                    ["git", "show", RELEASE + ":" + str(path.relative_to(source))], cwd=source
                ),
                str(path.relative_to(source)),
            )
            dest = ROOT / "tests/fixtures/contracts-v1" / kind / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            copied[str(dest.relative_to(ROOT))] = hashlib.sha256(data).hexdigest()
    lock = {
        "contract_version": "1.0.0",
        "wire_schema_version": "1.0",
        "tag": "v1.0.0",
        "release_commit": RELEASE,
        "tested_source_commit": TESTED,
        "canonical_manifest_sha256": hashlib.sha256(
            (source / "contracts-manifest.json").read_bytes()
        ).hexdigest(),
        "verified_manifest_files": len(checked),
        "publication_status": "LOCAL_ONLY_NOT_PUBLISHED",
        "files": copied,
    }
    (ROOT / "contracts.lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    print(json.dumps({"verified": len(checked), "vendored": len(copied), "release": RELEASE}))


if __name__ == "__main__":
    run()
