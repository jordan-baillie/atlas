"""Contract + behavior tests for atlas.execution.intake (#34 one-direction artifacts)."""
import json
from pathlib import Path

import pytest

from atlas.execution import intake, registry
from atlas.execution.registry import DeployedStrategy

FIXTURES = Path(__file__).resolve().parent.parent / "contract"


def _mkbook(live_dir: Path, name: str, **artifacts) -> Path:
    d = live_dir / name
    d.mkdir(parents=True, exist_ok=True)
    for fname, payload in artifacts.items():
        (d / f"{fname}.json").write_text(json.dumps(payload))
    return d


@pytest.fixture
def live_dir(tmp_path, monkeypatch):
    d = tmp_path / "intake_live"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(intake, "LIVE_DATA", d)
    monkeypatch.setattr(intake, "SOAK_LOG", d / "intake_soak.jsonl")
    return d


def test_contract_fixtures_parse():
    """The checked-in fixtures (shared verbatim with crucible's producer tests) must parse."""
    req = json.loads((FIXTURES / "deploy_request.fixture.json").read_text())
    ver = json.loads((FIXTURES / "lifecycle_verdict.fixture.json").read_text())
    assert req["schema_version"] == intake.SUPPORTED_SCHEMA
    assert ver["schema_version"] == intake.SUPPORTED_SCHEMA
    for k in ("name", "strategy_path", "capital", "broker", "tif", "expectation"):
        assert k in req
    for k in ("book", "asof", "lifecycle", "gates_all_pass", "n_days"):
        assert k in ver


def test_unsupported_schema_ignored(live_dir):
    d = _mkbook(live_dir, "b1", deploy_request={"schema_version": 99, "name": "b1"})
    assert intake._read(d / "deploy_request.json") is None


def test_shadow_matches(live_dir):
    req = json.loads((FIXTURES / "deploy_request.fixture.json").read_text())
    ver = json.loads((FIXTURES / "lifecycle_verdict.fixture.json").read_text())
    _mkbook(live_dir, "fixture_strategy", deploy_request=req, lifecycle_verdict=ver)
    s = DeployedStrategy(name="fixture_strategy", provider="fixture_strategy",
                         capital=5000.0, broker="alpaca", tif="opg", lifecycle="evidence")
    assert intake.check_book(live_dir / "fixture_strategy", {"fixture_strategy": s}) == []


def test_shadow_detects_lifecycle_divergence(live_dir):
    ver = json.loads((FIXTURES / "lifecycle_verdict.fixture.json").read_text())
    _mkbook(live_dir, "fixture_strategy", lifecycle_verdict=ver)
    s = DeployedStrategy(name="fixture_strategy", provider="fixture_strategy", lifecycle="shadow")
    div = intake.check_book(live_dir / "fixture_strategy", {"fixture_strategy": s})
    assert len(div) == 1 and div[0]["divergence"] == "lifecycle_mismatch"


def test_apply_refuses_to_unretire(live_dir):
    ver = json.loads((FIXTURES / "lifecycle_verdict.fixture.json").read_text())
    _mkbook(live_dir, "fixture_strategy", lifecycle_verdict=ver)
    s = DeployedStrategy(name="fixture_strategy", provider="fixture_strategy", lifecycle="retired")
    registry.upsert(s)
    intake.apply_book(live_dir / "fixture_strategy", {"fixture_strategy": s})
    assert next(x for x in registry.load() if x.name == "fixture_strategy").lifecycle == "retired"


def test_apply_lifecycle_transition(live_dir):
    ver = json.loads((FIXTURES / "lifecycle_verdict.fixture.json").read_text())
    _mkbook(live_dir, "fixture_strategy", lifecycle_verdict=ver)
    s = DeployedStrategy(name="fixture_strategy", provider="fixture_strategy", lifecycle="shadow")
    registry.upsert(s)
    actions = intake.apply_book(live_dir / "fixture_strategy", {"fixture_strategy": s})
    assert actions == ["fixture_strategy lifecycle shadow -> evidence"]
    assert next(x for x in registry.load() if x.name == "fixture_strategy").lifecycle == "evidence"
