"""Lesson 01 - four levels of config, on this project's real .env.

    .venv/Scripts/python.exe learn/01-config/run.py

Imports nothing from app/. Everything here is rebuilt from scratch so you can
see what pydantic-settings is doing for app/config.py.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOTENV = ROOT / ".env"


def rule(title: str) -> None:
    print()
    print("=" * 66)
    print(title)
    print("=" * 66)


# ======================================================================
rule("LEVEL 1 - os.environ only")
# ======================================================================

print("os.environ is the SHELL environment. It has never heard of .env.")
print()
for key in ("RETRIEVAL_K", "RELEVANCE_FLOOR", "LANGSMITH_TRACING"):
    print(f"  os.environ.get({key!r}) -> {os.environ.get(key)!r}")
print()
print("Those are probably all None, even though .env sets them.")
print("Nothing reads .env unless you write the code to.")

print()
print("And the classic silent bug:")
print(f"  bool('false')      -> {bool('false')}")
print(f"  bool('0')          -> {bool('0')}")
print(f"  bool('no')         -> {bool('no')}")
print("Every non-empty string is truthy. LANGSMITH_TRACING=false would")
print("switch tracing ON with naive code.")


# ======================================================================
rule("LEVEL 2 - parse .env by hand (this is ~all python-dotenv does)")
# ======================================================================


def parse_dotenv(path: Path) -> dict[str, str]:
    """A .env parser. 10 lines. No magic anywhere."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


raw = parse_dotenv(DOTENV)
print(f"parsed {len(raw)} keys from {DOTENV.name}")
print()
for key, value in raw.items():
    shown = "***redacted***" if "KEY" in key.upper() and value else repr(value)
    print(f"  {key:<22} = {shown}")

print()
print("Every value is a str. Note the types you actually want:")
for key in ("RETRIEVAL_K", "RELEVANCE_FLOOR", "LANGSMITH_TRACING"):
    if key in raw:
        print(f"  {key:<22} {raw[key]!r:<12} -> want "
              f"{'int' if key == 'RETRIEVAL_K' else 'float' if 'FLOOR' in key else 'bool'}")


# ======================================================================
rule("LEVEL 3 - hand-rolled conversion, and why the errors are bad")
# ======================================================================


def to_int(values: dict, key: str, default: int) -> int:
    return int(values.get(key) or default)


print("A deliberately broken value:")
broken = {"RETRIEVAL_K": "banana"}
try:
    to_int(broken, "RETRIEVAL_K", 5)
except ValueError as exc:
    print(f"  ValueError: {exc}")
print()
print("Notice what is missing: the word RETRIEVAL_K. In a 30-setting app")
print("that traceback tells you nothing about which setting is wrong.")


# ======================================================================
rule("LEVEL 4 - pydantic-settings, what app/config.py actually uses")
# ======================================================================

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MiniSettings(BaseSettings):
    """A cut-down app/config.py. Same library, ~15 lines instead of 200."""

    model_config = SettingsConfigDict(
        env_file=DOTENV,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",          # .env has keys we don't declare; don't crash
    )

    destination: str = "Singapore"
    retrieval_k: int = Field(default=5, ge=1, le=50)
    relevance_floor: float = Field(default=0.60, ge=0.0, le=1.0)
    chunk_size: int = Field(default=900, ge=200)
    chunk_overlap: int = Field(default=120, ge=0)
    langsmith_tracing: bool = False
    groq_api_key: SecretStr | None = None

    @model_validator(mode="after")
    def _overlap_fits_in_chunk(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"CHUNK_OVERLAP ({self.chunk_overlap}) must be smaller than "
                f"CHUNK_SIZE ({self.chunk_size})"
            )
        return self


s = MiniSettings()
print("Loaded from .env, converted, validated:")
print()
for name in MiniSettings.model_fields:
    value = getattr(s, name)
    print(f"  {name:<18} = {value!r:<28} {type(value).__name__}")

print()
print("Field name -> env var happened automatically:")
print("  retrieval_k  <- RETRIEVAL_K")
print("  no mapping table, case-insensitive")

# -- secrets -----------------------------------------------------------
print()
print("-- SecretStr ---------------------------------------------------")
print(f"  print(settings.groq_api_key)  -> {s.groq_api_key}")
print("  ^ pydantic refuses to print it. It cannot leak into a log or a")
print("    traceback by accident. To use it you must ask explicitly:")
if s.groq_api_key:
    real = s.groq_api_key.get_secret_value()
    # Show only the non-secret prefix. The point is that you had to ask.
    print(f"    .get_secret_value()  -> {real[:4]}... ({len(real)} chars)")

# -- single-value validation -------------------------------------------
print()
print("-- validation: one bad value -----------------------------------")
try:
    MiniSettings(retrieval_k=999)
except ValidationError as exc:
    for err in exc.errors():
        print(f"  field : {err['loc'][0]}")
        print(f"  got   : {err['input']}")
        print(f"  why   : {err['msg']}")
print("  ^ names the field, the value AND the rule. Compare level 3.")

# -- cross-field validation --------------------------------------------
print()
print("-- validation: a bad COMBINATION -------------------------------")
print("  CHUNK_OVERLAP=1000 with CHUNK_SIZE=900.")
print("  Both are perfectly valid integers on their own.")
try:
    MiniSettings(chunk_size=900, chunk_overlap=1000)
except ValidationError as exc:
    print(f"  caught: {exc.errors()[0]['msg']}")
print("  ^ this is the one hand-rolled config never gets around to.")
print("    Without it the chunker loops forever with no explanation.")


# ======================================================================
rule("THE TRAP - precedence, and why app/config.py has a shadow check")
# ======================================================================

print("pydantic-settings resolution order, highest wins:")
print("  1. Settings(retrieval_k=...) arguments")
print("  2. real shell environment variables")
print("  3. the .env file")
print("  4. the default in the class")
print()
print("So the SHELL BEATS .env. Correct in production (a container sets real")
print("env vars). A trap locally: you edit .env, nothing changes.")
print()

shadowed = []
for key, file_value in raw.items():
    if not file_value:
        continue
    shell_value = os.environ.get(key)
    if shell_value is not None and shell_value != file_value:
        shadowed.append(key)

if shadowed:
    print(f"  !! On THIS machine, {len(shadowed)} key(s) in .env are being")
    print(f"     overridden by your shell with a different value:")
    for key in shadowed:
        print(f"       {key}")
    print()
    print("     Editing .env will not change these. That is exactly what")
    print("     app/config.py:dotenv_keys_shadowed_by_environment() reports")
    print("     at startup and on GET /health.")
else:
    print("  No conflicts on this machine right now.")
    print("  (app/config.py checks anyway, because it costs 20 lines and")
    print("   saves someone an hour.)")


# ======================================================================
rule("SUMMARY")
# ======================================================================
print("""
  os.environ         text only, no .env, silent bool bug
  + hand parser      reads .env                      (~10 lines)
  + hand conversion  types, but useless errors       (~40 lines, buggy)
  pydantic-settings  types + bounds + cross-field    (~15 lines, done)

  The thing to carry forward: pydantic is a DECLARATIVE VALIDATOR.
  You describe the shape you want; it makes reality match or explains
  why it can't. You will see the identical idea twice more:
    lesson 08  tool arguments the model sends you
    lesson 12  JSON request bodies FastAPI receives

Next: learn/02-fetch-html/README.md
""")
