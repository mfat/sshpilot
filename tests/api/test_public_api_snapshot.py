import importlib.util
import json
from pathlib import Path

from sshpilot.api.models.connections import ConnectionValidationResult


ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = ROOT / "scripts" / "generate_api_artifacts.py"
SNAPSHOT_PATH = ROOT / "tests" / "api" / "snapshots" / "public_api.json"


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_api_artifacts", GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_public_api_surface_matches_reviewed_snapshot():
    generator = _load_generator()
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    actual = generator.build_surface()

    assert actual == expected, (
        "The public API surface changed. Review compatibility, update docs/api "
        "and docs/api/CHANGELOG.md, then deliberately run "
        "`python3 scripts/generate_api_artifacts.py`."
    )


def test_all_documented_model_types_are_convenience_exports():
    generator = _load_generator()
    model_exports = set(generator.MODEL_EXPORTS)
    module_model_names = {
        model.__name__
        for model in generator.public_models()
        if model not in generator.EXTRA_MODELS
    }
    module_enum_names = {
        enum_type.__name__
        for enum_type in generator.public_enums()
        if enum_type not in generator.EXTRA_ENUMS
    }

    transport_exports = set(generator.TRANSPORT_EXPORTS)

    assert module_model_names | module_enum_names <= model_exports | transport_exports


def test_snapshot_records_pre_wire_client_signature_shapes():
    generator = _load_generator()
    signatures = generator.client_signatures()

    assert signatures["delete_connection"]["parameters"] == [
        {
            "name": "request",
            "kind": "positional_or_keyword",
            "type": "DeleteConnectionRequest",
        }
    ]
    assert signatures["replay_terminal"] == {
        "parameters": [
            {
                "name": "request",
                "kind": "positional_or_keyword",
                "type": "ReplayRequest",
            }
        ],
        "return": "ReplayResult",
    }


def test_generated_model_purpose_ignores_dataclass_signature_docstrings():
    generator = _load_generator()

    assert generator._model_purpose(ConnectionValidationResult) == (
        "Frontend-neutral `ConnectionValidationResult` record."
    )


def test_normalize_sorts_unordered_collections():
    generator = _load_generator()

    assert generator._normalize(frozenset({"zeta", "alpha"})) == ["alpha", "zeta"]
