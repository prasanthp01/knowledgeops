import json

from knowledgeops.api import app
from knowledgeops.config import ROOT
from knowledgeops.models import SimulationStart


def save(name, schema):
    (ROOT / "schemas" / name).write_text(json.dumps(schema, indent=2) + "\n")


save(
    "simulation-start-v1.schema.json",
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **SimulationStart.model_json_schema(),
    },
)
for name in [
    "simulation-event-v1.schema.json",
    "simulation-result-v1.schema.json",
    "model-inference-request-v1.schema.json",
    "portfolio-evidence-v1.schema.json",
]:
    schema = json.loads((ROOT / "contracts/baseline" / name).read_text())
    if name.startswith("simulation-result"):
        schema["properties"].update(
            {
                "connector_kind": {"const": "synthetic_postgresql"},
                "data_provenance": {"type": "string"},
                "policy_version": {"type": "string"},
                "usage": {"type": "object"},
                "replayed_from": {"type": ["string", "null"]},
                "limitations": {"type": "array", "items": {"type": "string"}},
            }
        )
        schema["required"] += [
            "fixtures_version",
            "connector_kind",
            "data_provenance",
            "policy_version",
            "usage",
            "limitations",
        ]
    if name.startswith("portfolio-evidence"):
        schema["properties"].update(
            {
                "environment": {"type": "string"},
                "verified_deployment_url": {"type": ["string", "null"]},
            }
        )
        schema["required"] += ["environment", "verified_deployment_url", "last_verified_at"]
    save(name, schema)
save("openapi.json", app.openapi())
