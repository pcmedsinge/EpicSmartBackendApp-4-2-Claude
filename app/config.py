from functools import lru_cache  # stdlib decorator for caching function results
from pathlib import Path          # stdlib cross-platform path type (better than raw strings)

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Typed configuration loaded from the .env file.

    How pydantic-settings works:
      - Each class attribute maps 1-to-1 to an environment variable by the same name.
      - On instantiation, pydantic reads the .env file (configured below) and populates
        each field, coercing types automatically (e.g. "8000" → int).
      - If a required field is missing from .env, pydantic raises a clear ValidationError
        at startup — fail fast, rather than crashing later with an obscure KeyError.
      - C# analogy: IOptions<T> with appsettings.json binding, but with runtime type
        validation included.

    Field(...) marks a field as required (no default).
    Field(default=...) provides a fallback if the variable is absent from .env.
    """

    # -------------------------------------------------------------------------
    # Epic SMART Backend Services
    # -------------------------------------------------------------------------
    epic_client_id: str = Field(..., description="Epic app client ID (non-sensitive UUID)")

    epic_token_endpoint: str = Field(
        default="https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token",
        description="Epic OAuth 2.0 token endpoint",
    )

    epic_fhir_base_url: str = Field(
        default="https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
        description="Epic FHIR R4 base URL",
    )

    epic_private_key_path: Path = Field(
        default=Path("./keys/privatekey.pem"),
        description="Path to RS384 private key PEM file",
    )
    # Path is a stdlib type — pydantic-settings automatically converts the string
    # from .env into a Path object. Use .read_text() to load the key contents later.

    epic_key_id: str = Field(
        default="my-epic-key-v2",
        description="Key ID registered in Epic app config (must match exactly)",
    )

    epic_group_id: str = Field(
        ...,
        description="Epic Group ID for Bulk FHIR export (used in Phase 3+)",
    )

    # -------------------------------------------------------------------------
    # Tunneling — required for CDS Hooks (Epic POSTs to our service)
    # -------------------------------------------------------------------------
    zrok_public_url: str = Field(
        default="https://pcmsmartbackendapp1.share.zrok.io",
        description="Public zrok tunnel URL registered with Epic",
    )

    # -------------------------------------------------------------------------
    # OpenAI — NL generation, appeal letters, card narratives (Phase 5)
    # -------------------------------------------------------------------------
    openai_api_key: str = Field(..., description="OpenAI API key")

    openai_model: str = Field(
        default="gpt-4o-mini",
        description="OpenAI model ID to use for generation",
    )

    # -------------------------------------------------------------------------
    # Application settings
    # -------------------------------------------------------------------------
    app_host: str = Field(default="0.0.0.0", description="Uvicorn bind host")
    app_port: int = Field(default=8000, description="Uvicorn bind port")
    log_level: str = Field(default="info", description="Uvicorn / logging level")

    # -------------------------------------------------------------------------
    # Phase 3 — Clinical-Financial Bridge
    # -------------------------------------------------------------------------
    use_synthetic_overlay: bool = Field(
        default=True,
        description=(
            "When True, the bridge fills missing FHIR data from synthetic scenario data. "
            "When False, only real FHIR data is used — missing data = unmet criteria. "
            "Set to False to test against live Epic sandbox data only."
        ),
    )
    # bool coercion: pydantic-settings converts the string "true"/"false" from .env
    # to a Python bool automatically. C# analogy: bool.Parse(config["USE_SYNTHETIC_OVERLAY"])

    # -------------------------------------------------------------------------
    # Pydantic-settings configuration
    # model_config is a class-level dict (not an instance field) that tells
    # pydantic-settings HOW to load values.
    # C# analogy: like configuring the Options pattern builder.
    # -------------------------------------------------------------------------
    model_config = SettingsConfigDict(
        env_file=".env",           # load from .env in the working directory
        env_file_encoding="utf-8",
        case_sensitive=False,      # EPIC_CLIENT_ID and epic_client_id both work
        extra="ignore",            # silently ignore unknown variables in .env
    )


@lru_cache
def get_settings() -> Settings:
    """
    Return the singleton Settings instance.

    @lru_cache (Least Recently Used cache) on a zero-argument function acts as
    a lazy singleton: the first call constructs and caches the object; every
    subsequent call returns the same cached instance without re-reading .env.

    C# analogy: Lazy<Settings> or a static readonly property backed by
    IOptions<Settings> — constructed once, reused everywhere.

    Usage anywhere in the app:
        from app.config import get_settings
        settings = get_settings()
        print(settings.epic_client_id)
    """
    return Settings()  # type: ignore[call-arg]
    # type: ignore[call-arg] suppresses a spurious mypy warning about required
    # fields — pydantic populates them from .env at runtime, not via __init__ args.
