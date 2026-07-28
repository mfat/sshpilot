import importlib.util
import json
from pathlib import Path


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


def test_generated_api_artifacts_are_current():
    generator = _load_generator()
    stale = [
        str(path.relative_to(ROOT))
        for path, expected in generator.artifacts().items()
        if not path.exists() or path.read_text(encoding="utf-8") != expected
    ]

    assert not stale, (
        "Generated API artifacts are stale: "
        + ", ".join(stale)
        + ". Review compatibility and docs/api/CHANGELOG.md, then run "
        "`python3 scripts/generate_api_artifacts.py`."
    )
