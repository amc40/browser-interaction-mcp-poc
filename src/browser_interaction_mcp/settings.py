"""Runtime configuration, read from the environment or a local ``.env`` file."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Transport = Literal["stdio", "http"]


class Settings(BaseSettings):
    """Settings for the browser interaction MCP server.

    Every field can be overridden with a ``BROWSER_MCP_``-prefixed environment
    variable, for example ``BROWSER_MCP_RATE_LIMIT_PER_SECOND=0.5``.
    """

    model_config = SettingsConfigDict(
        env_prefix="BROWSER_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    transport: Transport = Field(
        default="stdio",
        description="Transport the server listens on.",
    )
    host: str = Field(
        default="127.0.0.1",
        description="Bind address, used by the http transport only. Kept on "
        "loopback by default because this server acts with personal credentials.",
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="Bind port, used by the http transport only.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Logging verbosity for the server process.",
    )

    rate_limit_per_second: float = Field(
        default=1.0,
        gt=0,
        description="Sustained tool-call rate allowed per client.",
    )
    rate_limit_burst: int = Field(
        default=5,
        ge=1,
        description="Number of calls a client may make back-to-back before the "
        "sustained rate applies.",
    )

    include_error_details: bool = Field(
        default=False,
        description="Send internal error details to the client. Leave disabled "
        "outside local debugging so browser state does not leak into tool output.",
    )
