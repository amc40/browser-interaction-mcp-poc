"""Runtime configuration, read from the environment or a local ``.env`` file."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Transport = Literal["stdio", "http"]

#: The account this server belongs to, as GitHub's immutable numeric user ID.
#: Everything here runs with that person's own browser credentials, so exactly
#: one account is ever authorised. Find an ID at https://api.github.com/users/<login>.
DEFAULT_GITHUB_USER_ID = "36701168"


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

    github_user_id: str = Field(
        default=DEFAULT_GITHUB_USER_ID,
        pattern=r"^\d+$",
        description="Numeric ID of the one GitHub account allowed to call "
        "tools, compared against the `sub` claim on the caller's verified "
        "token. An ID rather than a login because logins can be changed, and "
        "a freed login can be registered by somebody else.",
    )
    github_token_cache_seconds: int = Field(
        default=300,
        ge=0,
        description="How long a verified GitHub token stays cached. Without a "
        "cache every single request costs two GitHub API calls. The cost of "
        "one is that a token revoked on GitHub keeps working until its entry "
        "expires, so this is a revocation delay; 0 disables caching.",
    )
    github_client_id: str | None = Field(
        default=None,
        min_length=1,
        description="Client ID of the GitHub OAuth app clients authenticate "
        "against. Required by the http transport.",
    )
    github_client_secret: SecretStr | None = Field(
        default=None,
        description="Client secret of that GitHub OAuth app. Required by the "
        "http transport. Keep it in .env, never in the repository.",
    )
    github_oauth_base_url: str | None = Field(
        default=None,
        description="Public base URL the OAuth callback is reachable at, which "
        "must match the GitHub OAuth app's callback. Defaults to the bind "
        "address, which is right when the client runs on this machine.",
    )

    @property
    def oauth_base_url(self) -> str:
        """Return the public base URL to advertise for the OAuth endpoints."""
        if self.github_oauth_base_url is not None:
            return self.github_oauth_base_url
        return f"http://{self.host}:{self.port}"

    @model_validator(mode="after")
    def _check_oauth_app_configured(self) -> Self:
        """Reject an http server that has no GitHub OAuth app to authenticate against.

        Failing here rather than at the first tool call means the server never
        binds a port it cannot authenticate callers on.

        Returns:
            The validated settings.

        Raises:
            ValueError: If the http transport is selected without an OAuth app.
        """
        if self.transport == "http" and not (
            self.github_client_id and self.github_client_secret
        ):
            msg = (
                "The http transport authenticates callers with GitHub OAuth, so "
                "BROWSER_MCP_GITHUB_CLIENT_ID and BROWSER_MCP_GITHUB_CLIENT_SECRET "
                "must both be set."
            )
            raise ValueError(msg)
        return self
