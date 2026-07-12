from etf_rotation.config import load_config
from etf_rotation.data import normalize_daily_frame
import pandas as pd
import pytest
import yaml
from etf_rotation.cli import _latest_common_date


def test_load_config(config_path):
    config = load_config(config_path)
    assert config.symbols == ["GROWTH.SH", "ALT.SH", "BOND.SH"]
    assert config.strategy["max_gross_exposure"] <= 1


def test_local_config_extends_and_deep_merges(config_path, tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text(
        "extends: strategy.yaml\nqmt:\n  allow_live_orders: true\n",
        encoding="utf-8",
    )

    config = load_config(local)

    assert config.qmt["allow_live_orders"] is True
    assert config.qmt["account_id_env"] == "QMT_ACCOUNT_ID"
    assert config.strategy["rebalance_sleeves"] == 4
    assert config.path == local.resolve()


def test_config_extends_replaces_lists(config_path, tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text(
        "extends: strategy.yaml\n"
        "strategy:\n"
        "  momentum_lookbacks: [5, 10]\n"
        "  momentum_weights: [0.4, 0.6]\n",
        encoding="utf-8",
    )

    config = load_config(local)

    assert config.strategy["momentum_lookbacks"] == [5, 10]
    assert config.strategy["momentum_weights"] == [0.4, 0.6]


def test_config_extends_rejects_cycles(tmp_path):
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text("extends: second.yaml\n", encoding="utf-8")
    second.write_text("extends: first.yaml\n", encoding="utf-8")

    with pytest.raises(ValueError, match="循环引用"):
        load_config(first)


def test_llm_vote_config_is_loaded(config_path):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["llm"].update({
        "enabled": True, "mode": "vote",
        "models": ["github_copilot/model-a", "github_copilot/model-b"],
        "min_valid_votes": 2,
    })
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    config = load_config(config_path)

    assert config.llm["mode"] == "vote"
    assert config.llm["min_valid_votes"] == 2


def test_llm_single_rejects_multiple_models(config_path):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["llm"].update({
        "mode": "single",
        "models": ["github_copilot/model-a", "github_copilot/model-b"],
    })
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="只能配置一个模型"):
        load_config(config_path)


def test_csv_iso_dates_are_normalized():
    frame = pd.DataFrame(
        {
            "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0],
            "volume": [100], "amount": [1000],
        },
        index=["2025-01-02"],
    )
    normalized = normalize_daily_frame(frame)
    assert normalized.index[0] == pd.Timestamp("2025-01-02")


def test_latest_common_date_requires_every_loaded_symbol():
    data = {
        "A": pd.DataFrame({"close": [1.0, 1.1]}, index=pd.to_datetime(["2025-01-02", "2025-01-03"])),
        "B": pd.DataFrame({"close": [2.0]}, index=pd.to_datetime(["2025-01-02"])),
    }

    assert _latest_common_date(data) == pd.Timestamp("2025-01-02")
