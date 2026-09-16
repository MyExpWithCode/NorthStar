"""Typed application settings, loaded from the environment and `.env`.

Everything configurable lives here so no other module reads `os.environ`.
Filesystem paths are derived from the project root rather than configured, so
the app cannot be pointed at a knowledge base it did not build.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application configuration. See `.env.example` for the documented keys."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- LLM ----------------------------------------------------------------
    llm_provider: Literal["groq", "anthropic"] = "groq"

    groq_api_key: SecretStr | None = None
    #: Blank means "resolve a current tool-calling model from the live Groq
    #: catalogue at startup" rather than pinning an id that may be retired.
    groq_model: str | None = None

    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5"

    # -- Destination --------------------------------------------------------
    destination: str = "Singapore"
    destination_currency: str = "SGD"
    home_currency: str = "INR"

    # -- Retrieval ----------------------------------------------------------
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    retrieval_k: int = Field(default=6, ge=1, le=50)
    #: Cosine-similarity floor below which the KB tool returns
    #: NO_RELEVANT_CONTENT instead of weak chunks.
    #:
    #: Calibrated by measurement, not guessed (see docs/ARCHITECTURE.md section
    #: 6.1). On this corpus, genuine travel questions score 0.62-0.86 and
    #: unrelated questions 0.49-0.63, so 0.60 rejects clearly-unrelated queries
    #: without rejecting real ones. It is deliberately a COARSE guard: the
    #: ranges overlap, so no threshold can decide on its own whether retrieved
    #: text actually answers the question -- that is the prompt's job.
    relevance_floor: float = Field(default=0.60, ge=0.0, le=1.0)
    chunk_size: int = Field(default=900, ge=200)
    chunk_overlap: int = Field(default=120, ge=0)

    # -- Ingestion UI -------------------------------------------------------
    max_upload_mb: int = Field(default=20, ge=1, le=200)
    allowed_upload_extensions: tuple[str, ...] = (
        ".md",
        ".txt",
        ".html",
        ".htm",
        ".pdf",
        ".docx",
    )

    @model_validator(mode="after")
    def _overlap_fits_in_chunk(self) -> Settings:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"CHUNK_OVERLAP ({self.chunk_overlap}) must be smaller than "
                f"CHUNK_SIZE ({self.chunk_size})"
            )
        return self

    # -- Derived paths ------------------------------------------------------
    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def kb_dir(self) -> Path:
        """Knowledge-base documents plus the source registry."""
        return PROJECT_ROOT / "data" / "kb"

    @property
    def index_dir(self) -> Path:
        """Persisted FAISS index. Rebuildable; not committed."""
        return PROJECT_ROOT / "data" / "index"

    @property
    def registry_path(self) -> Path:
        """`sources.json` -- the authority on what is in the knowledge base."""
        return self.kb_dir / "sources.json"

    @property
    def manifest_path(self) -> Path:
        return self.index_dir / "manifest.json"

    @property
    def static_dir(self) -> Path:
        return PROJECT_ROOT / "app" / "static"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    # -- LLM credentials ----------------------------------------------------
    @property
    def llm_api_key_env(self) -> str:
        """Name of the env var the active provider needs, for error messages."""
        return "GROQ_API_KEY" if self.llm_provider == "groq" else "ANTHROPIC_API_KEY"

    @property
    def llm_api_key(self) -> str | None:
        secret = (
            self.groq_api_key if self.llm_provider == "groq" else self.anthropic_api_key
        )
        if secret is None:
            return None
        value = secret.get_secret_value().strip()
        return value or None


settings = Settings()
