from etf_rotation.config import load_config
from etf_rotation.data import CsvMarketDataStore, normalize_daily_frame
import pandas as pd
import pytest
import yaml
from etf_rotation.cli import _completed_download_end, _latest_common_date


def test_load_config(config_path):
    config = load_config(config_path)
    assert config.symbols == ["GROWTH.SH", "ALT.SH", "BOND.SH"]
    assert config.strategy["max_gross_exposure"] <= 1
    assert config.project["strategy_version"] == "0.5.2"


def test_config_rejects_strategy_version_drift(config_path):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["project"]["strategy_version"] = "9.9.9"
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="strategy_version"):
        load_config(config_path)


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("strategy", "score_volatility_exponent", -0.1, "score_volatility_exponent"),
        ("strategy", "idle_cash_proxy_max_weight", 1.1, "idle_cash_proxy_max_weight"),
        ("risk", "minimum_stop_distance", 1.0, "minimum_stop_distance"),
    ],
)
def test_new_strategy_parameters_reject_invalid_ranges(
    config_path, section, key, value, message
):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw[section][key] = value
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ValueError, match=message):
        load_config(config_path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw["cash_proxy"].update(symbol="UNKNOWN.SH"), "symbol"),
        (lambda raw: raw["cash_proxy"].update(signal_mode="raw"), "signal_mode"),
        (
            lambda raw: raw["cash_proxy"].update(reset_return_threshold=0.0),
            "reset_return_threshold",
        ),
        (
            lambda raw: raw["cash_proxy"].update(reset_window_start="02-30"),
            "reset_window_start",
        ),
    ],
)
def test_cash_proxy_config_fails_closed(config_path, mutate, message):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["universe"].append(
        {
            "symbol": "CASH.SH",
            "name": "现金代理",
            "role": "cash",
            "group": "money_market",
            "t0": True,
        }
    )
    raw["strategy"]["idle_cash_proxy_group"] = "money_market"
    raw["cash_proxy"].update(enabled=True, symbol="CASH.SH")
    mutate(raw)
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ValueError, match=message):
        load_config(config_path)


def test_cash_role_proxy_cannot_disable_its_corporate_action_guard(config_path):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["universe"].append(
        {
            "symbol": "CASH.SH",
            "name": "现金代理",
            "role": "cash",
            "group": "money_market",
            "t0": True,
        }
    )
    raw["strategy"].update(
        idle_cash_proxy_group="money_market", idle_cash_proxy_max_weight=0.30
    )
    raw["cash_proxy"].update(enabled=False, symbol="CASH.SH")
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="现金类闲置资金代理.*保护"):
        load_config(config_path)


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


def test_integer_etf_share_split_is_continuity_adjusted():
    frame = pd.DataFrame(
        {
            "open": [5.00, 5.10, 1.01, 1.02],
            "high": [5.10, 5.20, 1.02, 1.03],
            "low": [4.90, 5.00, 1.00, 1.01],
            "close": [5.05, 5.15, 1.015, 1.025],
            "volume": [100.0, 120.0, 600.0, 500.0],
            "amount": [505.0, 618.0, 609.0, 512.5],
        },
        index=pd.to_datetime(["2022-01-11", "2022-01-12", "2022-01-14", "2022-01-17"]),
    )

    normalized = normalize_daily_frame(frame)

    assert normalized.loc["2022-01-12", "close"] == pytest.approx(1.03)
    assert normalized.loc["2022-01-12", "volume"] == pytest.approx(600.0)
    assert normalized.loc["2022-01-12", "amount"] == pytest.approx(618.0)
    assert normalized.attrs["share_split_adjustments"] == [
        {
            "date": "2022-01-14",
            "kind": "split",
            "factor": 5.0,
            "observed_ratio": pytest.approx(5.15 / 1.01),
        }
    ]


def test_normal_market_gap_is_not_share_split_adjusted():
    frame = pd.DataFrame(
        {
            "open": [1.00, 0.90],
            "high": [1.02, 0.93],
            "low": [0.99, 0.89],
            "close": [1.00, 0.92],
            "volume": [100.0, 120.0],
            "amount": [100.0, 110.4],
        },
        index=pd.to_datetime(["2025-04-04", "2025-04-07"]),
    )

    normalized = normalize_daily_frame(frame)

    assert normalized.loc["2025-04-04", "close"] == pytest.approx(1.00)
    assert normalized.attrs["share_split_adjustments"] == []


def test_latest_common_date_requires_every_loaded_symbol():
    data = {
        "A": pd.DataFrame({"close": [1.0, 1.1]}, index=pd.to_datetime(["2025-01-02", "2025-01-03"])),
        "B": pd.DataFrame({"close": [2.0]}, index=pd.to_datetime(["2025-01-02"])),
    }

    assert _latest_common_date(data) == pd.Timestamp("2025-01-02")


def test_download_end_never_includes_an_unfinished_requested_day():
    assert _completed_download_end("20250110", "20250109") == "20250109"
    assert _completed_download_end("20250108", "20250109") == "20250108"


def test_market_data_store_can_require_every_requested_symbol(tmp_path):
    frame = pd.DataFrame(
        {
            "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0],
            "volume": [100], "amount": [1000],
        },
        index=pd.to_datetime(["2025-01-02"]),
    )
    store = CsvMarketDataStore(tmp_path)
    store.save(
        {"A.SH": frame},
        {"end_requested": "20250102", "completed_through": "20250102"},
    )

    with pytest.raises(FileNotFoundError, match="B.SH"):
        store.load(["A.SH", "B.SH"], require_all=True)
    assert store.load_metadata()["end_requested"] == "20250102"
    assert store.load_metadata()["completed_through"] == "20250102"
