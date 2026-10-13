import json

import jsonschema

from knowledgeops.config import ROOT
from knowledgeops.models import SimulationStart
from tests.test_api_jobs import P, completed


def test_actual_result_and_events_match_schemas(client, db, settings):
    run_id, result = completed(client, db, settings)
    result_schema = json.loads((ROOT / "schemas/simulation-result-v1.schema.json").read_text())
    event_schema = json.loads((ROOT / "schemas/simulation-event-v1.schema.json").read_text())
    jsonschema.Draft202012Validator(
        result_schema, format_checker=jsonschema.FormatChecker()
    ).validate(result)
    events = client.get(P + f"/simulations/{run_id}/events?format=json").json()["events"]
    for event in events:
        jsonschema.validate(event, event_schema)


def test_request_schema_matches_runtime_model():
    schema = json.loads((ROOT / "schemas/simulation-start-v1.schema.json").read_text())
    schema.pop("$schema")
    assert schema == SimulationStart.model_json_schema()


def test_all_schemas_valid():
    for path in (ROOT / "schemas").glob("*.schema.json"):
        jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text()))
