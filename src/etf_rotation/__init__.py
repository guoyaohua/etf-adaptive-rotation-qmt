"""Regime-aware ETF rotation for QMT."""

from .config import AppConfig, load_config
from .strategy import RegimeRotationStrategy, TargetPortfolio

__all__ = ["AppConfig", "RegimeRotationStrategy", "TargetPortfolio", "load_config"]
__version__ = "0.1.0"
