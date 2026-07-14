import re
from pathlib import Path

import yaml

from etf_rotation import STRATEGY_VERSION, __version__
from etf_rotation.config import load_config
from etf_rotation.version import STRATEGY_VERSION as CANONICAL_VERSION


ROOT = Path(__file__).parents[1]


def test_package_config_and_metadata_versions_match():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    project_version = re.search(
        r'^version\s*=\s*"([^"]+)"', pyproject, flags=re.MULTILINE
    )
    displayed_version = re.search(r"^当前策略版本：`v([^`]+)`", readme, flags=re.MULTILINE)
    raw_config = yaml.safe_load((ROOT / "configs" / "strategy.yaml").read_text(encoding="utf-8"))
    config = load_config(ROOT / "configs" / "strategy.yaml")

    assert CANONICAL_VERSION == STRATEGY_VERSION == __version__
    assert project_version is not None
    assert project_version.group(1) == CANONICAL_VERSION
    assert displayed_version is not None
    assert displayed_version.group(1) == CANONICAL_VERSION
    assert raw_config["project"]["strategy_version"] == CANONICAL_VERSION
    assert config.project["strategy_version"] == CANONICAL_VERSION


def test_update_log_latest_version_matches_code_and_is_append_ordered():
    text = (ROOT / "docs" / "STRATEGY_UPDATES.md").read_text(encoding="utf-8")
    versions = re.findall(r"^## v(\d+\.\d+\.\d+)\b", text, flags=re.MULTILINE)
    parsed = [tuple(map(int, version.split("."))) for version in versions]

    assert versions
    assert parsed == sorted(parsed)
    assert len(parsed) == len(set(parsed))
    assert versions[-1] == CANONICAL_VERSION
