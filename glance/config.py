"""Config loading and validation.

Rule from the spec: config.yaml stores the NAMES of environment variables,
never secret values. `api_key_env` is validated to look like an env var name,
which structurally rejects a pasted key.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from glance.models import Region

ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# pynput GlobalHotKeys syntax: "<cmd>+<shift>+1"
HOTKEY_RE = re.compile(r"^(<[a-z_]+>|[^<>+\s])(\+(<[a-z_]+>|[^<>+\s]))*$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModeConfig(Strict):
    name: str = Field(min_length=1)
    hotkey: str
    system_prompt: str = Field(min_length=1)

    @field_validator("hotkey")
    @classmethod
    def _hotkey_shape(cls, v: str) -> str:
        if not HOTKEY_RE.match(v):
            raise ValueError(
                f"{v!r} is not a pynput hotkey; expected e.g. '<cmd>+<shift>+1'"
            )
        return v


class ProviderConfig(Strict):
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen3-vl:8b"
    api_key_env: str | None = None
    timeout_s: float = Field(120.0, gt=0)
    max_tokens: int = Field(1024, gt=0)
    temperature: float = Field(0.2, ge=0, le=2)

    @field_validator("api_key_env")
    @classmethod
    def _must_be_env_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not ENV_NAME_RE.match(v):
            raise ValueError(
                f"api_key_env must be an environment variable NAME "
                f"(e.g. OPENAI_API_KEY), not a secret value. Got {v[:6]!r}..."
            )
        return v

    def resolve_key(self) -> str:
        """Read the secret from the environment at use time, never from config."""
        if self.api_key_env is None:
            return "not-needed"  # local Ollama rejects an empty string
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise RuntimeError(
                f"config references ${self.api_key_env} but it is not set. "
                f"Put it in .env or export it."
            )
        return key


class CaptureConfig(Strict):
    display: int = Field(1, ge=1)
    region: Region | None = None
    jpeg_quality: int = Field(70, ge=1, le=95)
    max_edge: int = Field(1600, ge=256, le=4096)

    @field_validator("region")
    @classmethod
    def _positive(cls, v: Region | None) -> Region | None:
        if v is not None and (v[2] <= 0 or v[3] <= 0):
            raise ValueError("region width and height must be positive")
        return v


class WatchConfig(Strict):
    enabled: bool = False
    interval_s: float = Field(10.0, ge=1.0)
    phash_distance: int = Field(4, ge=0, le=64)


class ServerConfig(Strict):
    port: int = Field(8765, ge=1024, le=65535)
    lan: bool = False


class PrivacyConfig(Strict):
    """Capture is skipped when a denied window overlaps the capture region."""

    denied_processes: list[str] = Field(default_factory=list)
    denied_window_titles: list[str] = Field(default_factory=list)
    # On macOS a window title is only readable with Screen Recording permission.
    # When a title cannot be read we do not know what it is, so skip the capture.
    fail_closed_on_unknown_title: bool = True


class OcrConfig(Strict):
    """augment = give the model the text too; fallback = only if the model fails."""

    mode: Literal["off", "augment", "fallback"] = "fallback"
    min_confidence: float = Field(0.5, ge=0, le=1)
    max_chars: int = Field(4000, gt=0)


class HudConfig(Strict):
    """The floating panel's appearance and its own hotkey."""

    # Resting opacity. The panel sits on top of whatever the user is reading,
    # so it stays faint until it is wanted.
    opacity: float = Field(default=0.28, ge=0.05, le=1.0)
    # Opacity while the pointer is over it, or while a response is streaming -
    # a panel you cannot read is not useful.
    opacity_active: float = Field(default=0.97, ge=0.05, le=1.0)
    # Show/hide the panel without touching the mouse.
    toggle_hotkey: str = "<cmd>+<shift>+\\"

    @field_validator("toggle_hotkey")
    @classmethod
    def _hotkey_shape(cls, v: str) -> str:
        if not HOTKEY_RE.match(v):
            raise ValueError(
                f"{v!r} is not a pynput hotkey; expected e.g. '<cmd>+<shift>+1'"
            )
        return v

    @model_validator(mode="after")
    def _ordered(self) -> "HudConfig":
        if self.opacity > self.opacity_active:
            raise ValueError(
                f"hud.opacity ({self.opacity}) is above hud.opacity_active "
                f"({self.opacity_active}); the panel would fade when pointed at"
            )
        return self


class HistoryConfig(Strict):
    max_exchanges: int = Field(6, ge=0, le=50)


class AppConfig(Strict):
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    hud: HudConfig = Field(default_factory=HudConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    pause_hotkey: str = "<cmd>+<shift>+0"
    modes: list[ModeConfig]

    @model_validator(mode="after")
    def _coherent(self) -> "AppConfig":
        if not self.modes:
            raise ValueError("at least one mode must be defined")
        names = [m.name for m in self.modes]
        if len(set(names)) != len(names):
            dupes = {n for n in names if names.count(n) > 1}
            raise ValueError(f"duplicate mode names: {sorted(dupes)}")
        keys = [m.hotkey for m in self.modes] + [
            self.pause_hotkey,
            self.hud.toggle_hotkey,
        ]
        if len(set(keys)) != len(keys):
            dupes = {k for k in keys if keys.count(k) > 1}
            raise ValueError(f"hotkey bound more than once: {sorted(dupes)}")
        return self

    def mode(self, name: str) -> ModeConfig:
        for m in self.modes:
            if m.name == name:
                return m
        raise KeyError(name)


class ConfigError(Exception):
    pass


def load_config(path: str | Path) -> AppConfig:
    p = Path(path).expanduser()
    if not p.exists():
        raise ConfigError(
            f"no config at {p}. Copy config.example.yaml to config.yaml to start."
        )
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{p} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{p} must contain a YAML mapping at the top level")
    try:
        return AppConfig.model_validate(raw)
    except Exception as e:
        raise ConfigError(f"{p} failed validation:\n{e}") from e
