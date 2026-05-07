"""統一的設定載入。所有腳本透過 `from scripts.lib.config import config` 取用。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 找專案根目錄的 .env（market-track/.env）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")


def _required(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(f"環境變數 {key} 未設定，請檢查 .env")
    return value


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


@dataclass(frozen=True)
class Config:
    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str

    # LLM
    llm_backend: str
    anthropic_api_key: str

    # Sources
    trump_source_url: str
    gua_cancer_base_url: str

    # Paths
    project_root: Path
    data_dir: Path
    log_dir: Path
    rules_file: Path

    # Schedule
    daily_report_time: str
    timezone: str


def load() -> Config:
    return Config(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_required("TELEGRAM_CHAT_ID"),
        llm_backend=_optional("LLM_BACKEND", "cowork"),
        anthropic_api_key=_optional("ANTHROPIC_API_KEY"),
        trump_source_url=_optional("TRUMP_SOURCE_URL"),
        gua_cancer_base_url=_optional("GUA_CANCER_BASE_URL", "https://socialworkerdaily.com"),
        project_root=_PROJECT_ROOT,
        data_dir=Path(_optional("DATA_DIR", str(_PROJECT_ROOT / "data"))),
        log_dir=Path(_optional("LOG_DIR", str(_PROJECT_ROOT / "logs"))),
        rules_file=Path(_optional("RULES_FILE", str(_PROJECT_ROOT / "scripts" / "event_impact_rules.yml"))),
        daily_report_time=_optional("DAILY_REPORT_TIME", "09:00"),
        timezone=_optional("TIMEZONE", "Asia/Taipei"),
    )


# 模組層級單例，import 即可用
config = load()
