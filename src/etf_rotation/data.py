from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume", "amount")
PRICE_COLUMNS = ("open", "high", "low", "close")


def adjust_integer_share_splits(
    frame: pd.DataFrame,
    *,
    minimum_factor: int = 2,
    ratio_tolerance: float = 0.08,
) -> pd.DataFrame:
    """Back-adjust unambiguous ETF share splits missing from QMT factors.

    QMT's ``front_ratio`` data does not always include ETF share consolidations
    and splits.  A 5-for-1 split would otherwise look like an 80% overnight
    loss and contaminate momentum, volatility and ATR for months.  Only gaps
    close to an integer factor are adjusted; ordinary market gaps are left
    untouched.  Prices remain on the latest share-unit scale, volume is
    converted to the same unit, and turnover amount is unchanged.
    """
    result = frame.copy()
    if len(result) < 2:
        result.attrs["share_split_adjustments"] = []
        return result

    previous_close = result["close"].shift(1)
    current_open = result["open"]
    adjustments: list[dict[str, float | str]] = []

    for date in result.index[1:]:
        before = float(previous_close.loc[date])
        after = float(current_open.loc[date])
        if not np.isfinite(before) or not np.isfinite(after) or before <= 0 or after <= 0:
            continue

        split_ratio = before / after
        consolidation_ratio = after / before
        if split_ratio >= minimum_factor - ratio_tolerance:
            factor = int(round(split_ratio))
            relative_error = abs(split_ratio / factor - 1.0) if factor >= minimum_factor else np.inf
            kind = "split"
        elif consolidation_ratio >= minimum_factor - ratio_tolerance:
            factor = int(round(consolidation_ratio))
            relative_error = (
                abs(consolidation_ratio / factor - 1.0) if factor >= minimum_factor else np.inf
            )
            kind = "consolidation"
        else:
            continue
        if relative_error > ratio_tolerance:
            continue

        historical = result.index < date
        if kind == "split":
            result.loc[historical, PRICE_COLUMNS] /= factor
            result.loc[historical, "volume"] *= factor
        else:
            result.loc[historical, PRICE_COLUMNS] *= factor
            result.loc[historical, "volume"] /= factor
        adjustments.append(
            {
                "date": pd.Timestamp(date).date().isoformat(),
                "kind": kind,
                "factor": float(factor),
                "observed_ratio": float(split_ratio if kind == "split" else consolidation_ratio),
            }
        )

    result.attrs["share_split_adjustments"] = adjustments
    return result


def normalize_daily_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set(REQUIRED_COLUMNS).difference(frame.columns)
    if missing:
        raise ValueError(f"行情缺少字段: {sorted(missing)}")
    result = frame.loc[:, REQUIRED_COLUMNS].copy()
    if not isinstance(result.index, pd.DatetimeIndex):
        raw_index = result.index.astype(str)
        compact = raw_index.str.fullmatch(r"\d{8}")
        parsed = pd.Series(pd.NaT, index=np.arange(len(raw_index)), dtype="datetime64[ns]")
        if compact.any():
            parsed.loc[compact] = pd.to_datetime(raw_index[compact], format="%Y%m%d", errors="coerce").to_numpy()
        if (~compact).any():
            parsed.loc[~compact] = pd.to_datetime(raw_index[~compact], errors="coerce").to_numpy()
        result.index = pd.DatetimeIndex(parsed.to_numpy())
    result = result.loc[~result.index.isna()].sort_index()
    result = result[~result.index.duplicated(keep="last")]
    result = result.apply(pd.to_numeric, errors="coerce")
    for column in ("open", "high", "low", "close"):
        result.loc[result[column] <= 0, column] = np.nan
    result.loc[result["volume"] < 0, "volume"] = np.nan
    result.loc[result["amount"] < 0, "amount"] = np.nan
    result = result.dropna(subset=["open", "high", "low", "close"])
    return adjust_integer_share_splits(result)


def align_market_data(data: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    if not data:
        raise ValueError("行情数据为空")
    result = {symbol: normalize_daily_frame(frame) for symbol, frame in data.items()}
    if all(frame.empty for frame in result.values()):
        raise ValueError("所有 ETF 行情均为空")
    return result


class CsvMarketDataStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    @staticmethod
    def _filename(symbol: str) -> str:
        return symbol.replace(".", "_") + ".csv"

    def save(self, data: Mapping[str, pd.DataFrame], metadata: Mapping | None = None) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for symbol, frame in data.items():
            normalized = normalize_daily_frame(frame)
            normalized.to_csv(self.directory / self._filename(symbol), index_label="date")
        if metadata is not None:
            with (self.directory / "metadata.json").open("w", encoding="utf-8") as handle:
                json.dump(metadata, handle, ensure_ascii=False, indent=2, default=str)

    def load_metadata(self) -> dict:
        path = self.directory / "metadata.json"
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"行情元数据必须是 JSON 对象: {path}")
        return payload

    def load(
        self,
        symbols: Iterable[str],
        start: str | None = None,
        end: str | None = None,
        *,
        require_all: bool = False,
    ) -> dict[str, pd.DataFrame]:
        requested = list(symbols)
        result: dict[str, pd.DataFrame] = {}
        for symbol in requested:
            path = self.directory / self._filename(symbol)
            if not path.exists():
                continue
            frame = pd.read_csv(path, index_col="date")
            frame = normalize_daily_frame(frame)
            if start:
                frame = frame.loc[frame.index >= pd.Timestamp(start)]
            if end:
                frame = frame.loc[frame.index <= pd.Timestamp(end)]
            result[symbol] = frame
        missing = sorted(set(requested).difference(result))
        if require_all and missing:
            raise FileNotFoundError(f"行情目录缺少必需标的: {missing}")
        if not result:
            raise FileNotFoundError(f"目录中没有可用行情: {self.directory}")
        return result


class QmtDailyDownloader:
    """Thin, optional adapter around the local QMT xtdata service."""

    def __init__(self):
        try:
            from xtquant import xtdata
        except ImportError as exc:  # pragma: no cover - depends on QMT installation
            raise RuntimeError("当前 Python 环境未安装 xtquant，请从 QMT 环境运行") from exc
        self.xtdata = xtdata

    def download(
        self,
        symbols: list[str],
        start: str,
        end: str,
        wait_timeout: int = 300,
    ) -> dict[str, pd.DataFrame]:
        completed = {"done": False, "error": None}

        def callback(payload: dict) -> None:
            if payload.get("error"):
                completed["error"] = str(payload["error"])
            if payload.get("finished") == payload.get("total"):
                completed["done"] = True

        try:
            self.xtdata.download_history_data2(
                symbols,
                period="1d",
                start_time=start,
                end_time=end,
                callback=callback,
                incrementally=None,
            )
            deadline = time.time() + wait_timeout
            while not completed["done"] and time.time() < deadline:
                time.sleep(0.5)
            if completed["error"]:
                raise RuntimeError(f"QMT 下载失败: {completed['error']}")
            if not completed["done"]:
                raise TimeoutError(f"QMT 行情下载超过 {wait_timeout} 秒")
        except AttributeError:
            for symbol in symbols:  # compatibility with older xtquant builds
                self.xtdata.download_history_data(symbol, "1d", start, end, None)

        panels = self.xtdata.get_market_data(
            field_list=list(REQUIRED_COLUMNS),
            stock_list=symbols,
            period="1d",
            start_time=start,
            end_time=end,
            count=-1,
            dividend_type="front",
            fill_data=False,
        )
        result: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            columns: dict[str, pd.Series] = {}
            for field in REQUIRED_COLUMNS:
                panel = panels.get(field)
                if panel is not None and symbol in panel.index:
                    columns[field] = panel.loc[symbol]
            if len(columns) != len(REQUIRED_COLUMNS):
                continue
            result[symbol] = normalize_daily_frame(pd.DataFrame(columns))
        if not result:
            raise RuntimeError("QMT 未返回任何日线，请确认行情端已启动并下载权限正常")
        missing = sorted(set(symbols).difference(result))
        if missing:
            raise RuntimeError(f"QMT 未返回全部必需日线: {missing}")
        return result
