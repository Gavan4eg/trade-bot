from pydantic_settings import BaseSettings
from typing import Literal


class Settings(BaseSettings):
    # Exchange selection: "bybit" | "binance"
    exchange: str = "bybit"

    # Bybit API
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = True

    # Binance API
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True

    paper_trading: bool = True  # Симуляция без реальных сделок

    # Trading settings
    max_positions: int = 3
    risk_per_trade: float = 1.0  # % of balance
    min_rr: float = 2.0
    range_timeframe: str = "1h"
    range_candles: int = 24
    range_max_width_percent: float = 5.0  # % max range width (increase for testnet)
    liquidation_buffer_percent: float = 0.2  # % buffer beyond liquidation cluster for stop

    # Confirmation settings
    min_confirmations: int = 2
    sweep_threshold_percent: float = 0.1
    volume_spike_multiplier: float = 2.0

    # TP/SL settings
    tp1_rr: float = 2.0
    tp1_close_percent: int = 50
    tp2_rr: float = 3.0
    tp2_close_percent: int = 30
    trailing_activation_rr: float = 2.5
    trailing_step_percent: float = 0.5

    # Server settings
    host: str = "0.0.0.0"
    port: int = 8000

    # Security
    webhook_token: str = ""  # Secret token for webhook authentication
    dashboard_username: str = "admin"
    dashboard_password: str = "admin123"

    # Database
    database_url: str = "sqlite+aiosqlite:///./trading_bot.db"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
