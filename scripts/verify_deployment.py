"""Safe, bounded synthetic API checks against the deployed project only."""

import argparse
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

p = argparse.ArgumentParser()
p.add_argument("--internal", action="store_true")
p.add_argument("--output")
args = p.parse_args()
origin = "https://portfolio.gappazappa.in"
base = "http://127.0.0.1:8044" if args.internal else origin
prefix = "/api/projects/knowledgeops"
checks = []
headers = {"Origin": origin, "Host": "portfolio.gappazappa.in"}
with httpx.Client(base_url=base, headers=headers, timeout=20) as c:
    assert c.get(prefix + "/ready").status_code == 200
    s = c.post(prefix + "/session", json={})
    assert s.status_code == 201, s.text
    cookie = s.headers["set-cookie"]
    assert all(x in cookie.lower() for x in ["httponly", "secure", "samesite=strict"])
    if args.internal:
        c.headers["Cookie"] = "ko_session=" + s.cookies["ko_session"]
    c.headers["X-CSRF-Token"] = s.json()["csrf_token"]
    checks.append("readiness and secure scoped session")
    cases = [
        ("hotel allowance domestic travel", "staff", "current", "140", None),
        ("hotel allowance domestic travel", "staff", "previous", "110", None),
        ("hotel allowance domestic travel", "staff", "conflict", None, "CONFLICTING_REVISIONS"),
        ("treasury reserve target", "staff", "current", None, "NOT_ENOUGH_EVIDENCE"),
        ("treasury reserve target", "finance", "current", "420000", None),
        (
            "What is the lunar relocation allowance?",
            "staff",
            "current",
            None,
            "NOT_ENOUGH_EVIDENCE",
        ),
    ]
    outputs = []
    for question, role, revision, needle, abstention in cases:
        key = str(uuid.uuid4())
        body = {"input": {"question": question, "role": role, "revision": revision}}
        a = c.post(prefix + "/simulations", json=body, headers={"Idempotency-Key": key})
        assert a.status_code == 202, a.text
        rid = a.json()["run_id"]
        for _ in range(150):
            result = c.get(prefix + f"/simulations/{rid}/result")
            if result.status_code == 200:
                break
            assert result.status_code == 409, result.text
            time.sleep(0.15)
        assert result.status_code == 200, result.text
        o = result.json()["output"]
        answer = " ".join(x["text"] for x in o["answer"])
        assert not needle or needle in answer, answer
        assert o["abstention"] == abstention, (o["abstention"], abstention)
        if role == "staff" and "treasury" in question:
            assert (
                not o["citations"]
                and "420000" not in result.text
                and "reserve-plan" not in result.text
            )
        ev = c.get(prefix + f"/simulations/{rid}/events?format=json")
        ev.raise_for_status()
        assert [e["sequence"] for e in ev.json()["events"]] == [1, 2, 3, 4, 5]
        again = c.post(prefix + "/simulations", json=body, headers={"Idempotency-Key": key})
        assert again.json()["run_id"] == rid
        outputs.append(
            {
                "question": question,
                "role": role,
                "revision": revision,
                "abstention": o["abstention"],
                "citation_count": len(o["citations"]),
                "status": "PASS",
            }
        )
    checks.extend(
        [
            "six current/historical/conflict/permission/missing-evidence cases",
            "ordered durable events",
            "idempotent admission",
        ]
    )
    sse = c.get(prefix + f"/simulations/{rid}/events", headers={"Last-Event-ID": "3"})
    assert (
        sse.status_code == 200
        and "id: 4\n" in sse.text
        and "id: 1\n" not in sse.text
        and "event: done" in sse.text
    )
    checks.append("SSE and resume cursor")
    bad = c.post(
        prefix + "/simulations",
        json=body,
        headers={"Idempotency-Key": str(uuid.uuid4()), "X-CSRF-Token": "wrong"},
    )
    assert bad.status_code == 403
    bad = c.post(prefix + "/session", json={}, headers={"Origin": "https://invalid.example"})
    assert bad.status_code == 403
    bad = c.post(
        prefix + "/simulations",
        json={"input": {"question": "hotel allowance", "token": "do-not-echo-test-marker"}},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert bad.status_code == 422 and "do-not-echo-test-marker" not in bad.text
    bad = c.post(
        prefix + "/simulations",
        json={"input": {"question": "hotel allowance"}, "mode": "LIVE_MODEL_AUTHENTICATED"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert bad.status_code == 422
    checks.extend(
        [
            "CSRF and Origin rejection",
            "input validation without sensitive-field echo",
            "public paid inference refused",
        ]
    )
    with httpx.Client(base_url=base, headers=headers, timeout=20) as stranger:
        ss = stranger.post(prefix + "/session", json={})
        assert ss.status_code == 201, ss.text
        if args.internal:
            stranger.headers["Cookie"] = "ko_session=" + ss.cookies["ko_session"]
        assert stranger.get(prefix + f"/simulations/{rid}/result").status_code == 404
    checks.append("cross-session result denied")
    page = c.get("/projects/knowledgeops")
    assert page.status_code == 200 and "Every answer" in page.text
    assert c.get("/projects/knowledgeops/").status_code == 200
    assert c.get("/projects/knowledgeops/assets/app.js").status_code == 200
    assert c.get("/projects/knowledgeops/assets/style.css").status_code == 200
    assert "frame-ancestors" in page.headers.get("content-security-policy", "")
    checks.append("page trailing slash assets and CSP")
receipt = {
    "status": "PASS",
    "verified_at": datetime.now(UTC).isoformat(),
    "environment": "private container loopback" if args.internal else "public HTTPS",
    "origin": origin,
    "checks": checks,
    "cases": outputs,
    "external_provider_calls": 0,
}
if args.output:
    Path(args.output).write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt, indent=2))
