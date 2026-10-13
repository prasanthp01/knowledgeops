"""Validate running API/worker behavior with synthetic data; receipts never include tokens."""

import argparse
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from knowledgeops.contracts import REGISTRY
from knowledgeops.vendor_contracts.validation import validate_stream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--tokens", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Explicit loopback-only probe; production tests are a separate release decision.
    if args.origin not in {"http://127.0.0.1:8047", "http://localhost:8047"}:
        raise SystemExit("This probe is restricted to the local Contracts service on port 8047")
    tokens = json.loads(args.tokens.read_text())
    prefix = "/api/projects/knowledgeops"
    receipt = {
        "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "environment": "local",
        "contracts_version": "1.0.0",
        "checks": [],
        "runs": [],
        "knowledge": {},
    }

    def check(condition, name):
        if not condition:
            raise RuntimeError("Probe failed: " + name)
        receipt["checks"].append(name)

    def contract(response, name, status=200):
        check(response.status_code == status, name + ":HTTP-" + str(status))
        value = response.json()
        REGISTRY.validate(name, value)
        check(True, name + ":canonical")
        return value

    with httpx.Client(base_url=args.origin, timeout=15, headers={"Origin": args.origin}) as client:
        health = client.get(prefix + "/health").json()
        receipt["source_revision"] = health["source_revision"]
        receipt["app_version"] = health["app_version"]
        check(client.get(prefix + "/ready").status_code == 200, "readiness")
        for route, name in [
            ("/identity", "project-identity"),
            ("/simulation/schema", "simulation-schema"),
            ("/simulation/presets", "simulation-presets"),
        ]:
            value = contract(client.get(prefix + route), name)
            check(client.get(route).json() == value, name + ":stripped-prefix-equivalence")
            receipt[name] = value
        schema = receipt["simulation-schema"]
        check(
            hashlib.sha256(
                json.dumps(
                    schema["input_schema"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            == schema["input_schema_sha256"],
            "input-schema-digest",
        )
        session = client.post(prefix + "/simulation/session", json={})
        check(session.status_code == 201, "session-bootstrap")
        client.headers["Authorization"] = "Bearer " + session.json()["session_token"]
        for role in ["staff", "finance", "finance"]:
            body = {
                "schema_version": "1.0",
                "project_id": "knowledgeops",
                "mode": "SANDBOX_DETERMINISTIC",
                "seed": 42,
                "client_correlation_id": "http-probe:" + str(uuid.uuid4()),
                "input": {"question": "What is the treasury reserve target?", "role": role},
            }
            run = contract(
                client.post(
                    prefix + "/simulations",
                    json=body,
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                ),
                "simulation-run",
                202,
            )
            url = prefix + "/simulations/" + run["run_id"]
            deadline = time.monotonic() + 15
            while run["status"] in {"ACCEPTED", "RUNNING"} and time.monotonic() < deadline:
                time.sleep(0.1)
                run = contract(client.get(url), "simulation-run")
            check(run["status"] == "SUCCEEDED", "durable-worker-completion:" + role)
            result = contract(client.get(url + "/result"), "simulation-result")
            events = contract(
                client.get(url + "/events", headers={"Accept": "application/json"}),
                "simulation-events",
            )
            validate_stream(events["events"], run_id=run["run_id"], project_id="knowledgeops")
            check(events["next_sequence"] == run["last_sequence"] == 5, "complete-ordered-events")
            stream = client.get(
                url + "/events", headers={"Accept": "text/event-stream", "Last-Event-ID": "2"}
            )
            steps = [
                json.loads(line[6:])
                for line in stream.text.splitlines()
                if line.startswith("data: ") and '"event_id"' in line
            ]
            validate_stream(steps, run_id=run["run_id"], project_id="knowledgeops", after=2)
            check(
                len(steps) == 3 and 'event: end\ndata: {"last_sequence": 5}' in stream.text,
                "SSE-reconnect-end",
            )
            receipt["runs"].append({"input": body, "run": run, "result": result, "events": events})
        check(
            receipt["runs"][0]["result"]["output"]["citations"] == [],
            "staff-no-restricted-citations",
        )
        check(
            bool(receipt["runs"][1]["result"]["output"]["citations"]),
            "finance-authorized-citations",
        )
        check(
            receipt["runs"][1]["result"]["output"] == receipt["runs"][2]["result"]["output"],
            "same-input-seed-semantic-repeatability",
        )
        # A different live session cannot read any representation of the run.
        with httpx.Client(
            base_url=args.origin, headers={"Origin": args.origin}, timeout=15
        ) as stranger:
            token = stranger.post(prefix + "/simulation/session", json={}).json()["session_token"]
            stranger.headers["Authorization"] = "Bearer " + token
            for suffix in ["", "/events", "/result"]:
                error = contract(stranger.get(url + suffix), "error", 404)
                check(error["code"] == "NOT_FOUND", "foreign-run-indistinguishable")
        marker = "SYNTHETIC_HTTP_PROPERTY_CANARY"
        body["input"][marker] = marker
        response = client.post(
            prefix + "/simulations", json=body, headers={"Idempotency-Key": str(uuid.uuid4())}
        )
        contract(response, "error", 422)
        check(marker not in response.text, "unexpected-property-redaction")
        for label, token_name, text, collections, expected in [
            ("normal", "resolveai_staff", "hotel allowance domestic travel", ["people"], "OK"),
            ("denied", "denied_fixture", "treasury reserve target", ["finance"], "DENIED"),
            (
                "no-results",
                "resolveai_staff",
                "lunar relocation allowance",
                ["people"],
                "NO_RESULTS",
            ),
            ("restricted", "resolveai_staff", "treasury reserve target", ["finance"], "NO_RESULTS"),
            ("authorized", "resolveai_finance", "treasury reserve target", ["finance"], "OK"),
            ("flowops", "flowops_staff", "hotel allowance domestic travel", ["people"], "OK"),
        ]:
            query = {
                "schema_version": "1.0",
                "application_id": "flowops" if label == "flowops" else "resolveai",
                "tenant_id": "demo",
                "query": text,
                "collection_ids": collections,
                "max_results": 3,
                "max_excerpt_chars": 200,
                "correlation_id": "http-lookup:" + label,
                "mode": "SANDBOX_DETERMINISTIC",
            }
            response = client.post(
                prefix + "/knowledge/query",
                json=query,
                headers={"Authorization": "Bearer " + tokens[token_name]},
            )
            answer = contract(response, "knowledge-response")
            check(answer["status"] == expected, "knowledge:" + label)
            if expected != "OK":
                check(
                    answer["answer"] is None
                    and answer["citations"] == []
                    and "treasury" not in response.text,
                    "no-metadata-disclosure:" + label,
                )
            for citation in answer["citations"]:
                check(
                    len(citation["passage"]) <= 200
                    and hashlib.sha256(citation["passage"].encode()).hexdigest()
                    == citation["evidence"]["sha256"],
                    "exact-excerpt-hash",
                )
            receipt["knowledge"][label] = answer
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": "PASS",
                "checks": len(receipt["checks"]),
                "source_revision": receipt["source_revision"],
                "receipt": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
