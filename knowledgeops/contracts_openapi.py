"""Describe implemented operations using the unchanged canonical schema snapshot."""

import json

from knowledgeops import __version__
from knowledgeops.config import ROOT
from knowledgeops.contracts import BODY_LIMIT, INPUT_SCHEMA


def specification():
    spec = json.loads((ROOT / "schemas/ecosystem-v1.json").read_text())
    spec["info"] = {
        "title": "KnowledgeOps Contracts adapter",
        "version": __version__,
        "description": "Contracts 1.0.0; synthetic local adoption. Bearer sessions or CSRF-protected scoped cookies. No production cutover.",
    }
    spec["servers"] = [{"url": "/api/projects/knowledgeops"}, {"url": "/"}]
    spec["paths"] = {
        path: methods
        for path, methods in spec["paths"].items()
        if path.startswith("/simulation") or path == "/knowledge/query"
    }
    spec["components"]["schemas"]["knowledgeops-input"] = INPUT_SCHEMA
    spec["paths"]["/simulations"]["post"]["parameters"] = [
        {
            "name": "Idempotency-Key",
            "in": "header",
            "required": True,
            "schema": {
                "type": "string",
                "minLength": 8,
                "maxLength": 80,
                "pattern": "^[A-Za-z0-9_-]+$",
            },
        }
    ]
    spec["paths"]["/simulations"]["post"]["description"] = (
        f"Maximum {BODY_LIMIT} bytes. Required canonical seed/correlation. Input follows knowledgeops-input; "
        "nested seed rejected. Replay requires extensions.x_replay_run_id, same owned input and seed."
    )
    spec["paths"]["/knowledge/query"]["post"]["description"] = (
        "Bearer credential bound to application, tenant, environment, audience, role and collection scopes. "
        "Synthetic data only. Product query length 3..500, max_results 1..5, at most four unique known collections."
    )
    spec["paths"]["/simulation/session"] = {
        "post": {
            "operationId": "bootstrap_knowledgeops_session",
            "security": [],
            "description": "Same-origin POST {}. Use the returned bearer token for internal paths; scoped HttpOnly cookie for prefixed EventSource. Cookie writes also require X-CSRF-Token.",
            "parameters": [
                {"name": "Origin", "in": "header", "required": True, "schema": {"type": "string"}}
            ],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "additionalProperties": False}
                    }
                },
            },
            "responses": {
                "201": {
                    "description": "Project-owned session bootstrap",
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["session_token", "csrf_token", "expires_in"],
                                "additionalProperties": False,
                                "properties": {
                                    "session_token": {"type": "string"},
                                    "csrf_token": {"type": "string"},
                                    "expires_in": {
                                        "type": "integer",
                                        "minimum": 60,
                                        "maximum": 3600,
                                    },
                                },
                            }
                        }
                    },
                }
            },
        }
    }
    return spec
