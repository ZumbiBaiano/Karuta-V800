from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BrowserKind = Literal["chrome", "brave", "opera_gx"]
AccountId = Literal["a", "b", "c"]


class AccountConfig(BaseModel):
    browser: BrowserKind
    profile_dir: Path
    executable_path: Path | None = None

    @field_validator("profile_dir", "executable_path", mode="before")
    @classmethod
    def _expand(cls, v):
        if v is None:
            return None
        return Path(os.path.expanduser(str(v)))


class KarutaConfig(BaseModel):
    guild_id: str
    channel_id: str
    karuta_bot_id: str = "646937666251915264"
    pick_cooldown_seconds: int = 660
    drop_timeout_seconds: int = 8
    rotation_order: list[AccountId] = Field(default_factory=lambda: ["a", "b", "c"])
    pick_button_index: dict[AccountId, int]
    accounts: dict[AccountId, AccountConfig]

    @field_validator("pick_button_index")
    @classmethod
    def _validate_buttons(cls, v):
        if sorted(v.values()) != [0, 1, 2]:
            raise ValueError("pick_button_index deve cobrir exatamente {0, 1, 2}")
        return v


class Settings(BaseSettings):
    """Variaveis de ambiente (.env)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="KARUTA_",
        extra="ignore",
    )

    config: Path = Path("./config.yaml")
    log_level: str = "INFO"


def load_config(path: Path | None = None) -> KarutaConfig:
    settings = Settings()
    cfg_path = path or settings.config
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Config nao encontrado em {cfg_path}. "
            "Copie config.example.yaml para config.yaml e ajuste."
        )
    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return KarutaConfig.model_validate(raw)
