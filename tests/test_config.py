from etf_rotation.config import load_config
from etf_rotation.data import normalize_daily_frame
import pandas as pd


def test_load_config(config_path):
    config = load_config(config_path)
    assert config.symbols == ["GROWTH.SH", "ALT.SH", "BOND.SH"]
    assert config.strategy["max_gross_exposure"] <= 1


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
