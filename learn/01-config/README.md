# 01 — Config: how `.env` becomes typed Python

**Real file:** [app/config.py](../../app/config.py)
**Library:** `pydantic-settings` (which sits on `pydantic`)

We start here because it is the smallest piece, and because it introduces
pydantic — which you will meet again in FastAPI (lesson 12) and in tool schemas
(lesson 08). Learning pydantic once pays for itself three times.

## The problem

`.env` holds text. Only text. Every value in it is a string:

```
RETRIEVAL_K=5
RELEVANCE_FLOOR=0.60
LANGSMITH_TRACING=false
GROQ_API_KEY=gsk_abc123
```

Your code wants an `int`, a `float`, a `bool`, and a secret that never gets
printed by accident. Someone has to convert, and someone has to complain when
`RETRIEVAL_K=banana`.

## Level 1 — raw stdlib

```python
import os
k = int(os.environ.get("RETRIEVAL_K", "5"))
```

Works. Problems, in order of how much they will hurt you:

1. **It does not read `.env` at all.** `os.environ` is the *shell* environment.
   A `.env` file is a convention, not a thing Python knows about. You have to
   parse it yourself.
2. `int(...)` raises `ValueError: invalid literal for int()` with no mention of
   which setting was wrong.
3. `bool("false")` is `True`. Every non-empty string is truthy. This bug is
   extremely common and completely silent.
4. The default `"5"` is buried at the use site. Ten modules read the same
   variable, each with its own default, and they drift.
5. The key is a plain `str`, so it lands in logs and tracebacks.

## Level 2 — parse `.env` by hand

`run.py` in this folder does it: split on `=`, strip quotes, skip comments.
It is about 12 lines and it is genuinely all `python-dotenv` does for the
common case. Worth writing once so `.env` stops feeling magical.

Still leaves problems 2–5.

## Level 3 — `pydantic-settings`, what NorthStar uses

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env")

    retrieval_k: int = Field(default=5, ge=1, le=50)
    relevance_floor: float = Field(default=0.60, ge=0.0, le=1.0)
    langsmith_tracing: bool = False
    groq_api_key: SecretStr | None = None
```

What those four lines bought:

| Problem | How it is solved |
|---|---|
| reads `.env` | `env_file=` |
| bad value | `ValidationError` naming the field, the value, and the rule |
| `bool("false")` | pydantic knows `false/0/no/off` are all `False` |
| scattered defaults | one class, one place, `ge`/`le` bounds visible |
| leaked secrets | `SecretStr` prints as `**********`; you must call `.get_secret_value()` |

Two more things it does that are less obvious and more valuable:

**Field name -> env var, automatically.** `retrieval_k` reads `RETRIEVAL_K`.
Case-insensitive, no mapping table to maintain.

**Cross-field validation.** `app/config.py` has:

```python
@model_validator(mode="after")
def _overlap_fits_in_chunk(self):
    if self.chunk_overlap >= self.chunk_size:
        raise ValueError(...)
```

`CHUNK_OVERLAP=1000` with `CHUNK_SIZE=900` is not an invalid *value* — both are
fine integers. It is an invalid *combination*, and it would produce an infinite
loop deep inside the chunker. Here it fails at startup with a readable message.
**This is the thing hand-rolled config never gets around to.**

## The trap worth knowing about

`app/config.py` has a function that looks like paranoia:

```python
def dotenv_keys_shadowed_by_environment() -> list[str]:
```

Here is why it exists. pydantic-settings resolves in this order:

```
  1. arguments passed to Settings(...)        highest
  2. real environment variables
  3. the .env file
  4. the default in the class                  lowest
```

**The shell environment beats `.env`.** That is correct for deployment — a
container sets real env vars and they should win. Locally it is a trap: you
edit `.env`, restart, and nothing changes, because a `GROQ_API_KEY` you exported
into your shell six weeks ago is silently winning. That function detects the
conflict and reports it at startup.

You are actually hitting this right now — lesson 00 printed
`GROQ_API_KEY in shell env: True`. `run.py` here will tell you whether the two
values differ.

## Alternatives

| Approach | Reads `.env` | Types | Validates | When to pick it |
|---|---|---|---|---|
| `os.environ` | no | no | no | one script, two settings |
| `python-dotenv` | yes | no | no | you want `.env` loading and nothing else |
| `pydantic-settings` | yes | yes | yes | **default choice for an app** |
| `dynaconf` | yes | yes | some | multi-environment layering (dev/stage/prod files) |
| `environs` | yes | yes | some | lighter than pydantic, same idea |
| Hydra / OmegaConf | via yaml | yes | some | ML experiments; deep nested config, CLI overrides |
| a plain `config.py` | n/a | yes | yes | no secrets, no per-environment change |

**Note on "just use a `config.py`".** It is a real option and often the right
one. The reason NorthStar does not is secrets: `GROQ_API_KEY` must not be in
git, so it has to come from outside the repo, which means `.env` or real env
vars, which means parsing and validating.

## Run it

```bash
.venv/Scripts/python.exe learn/01-config/run.py
```

It walks all four levels against this project's actual `.env`, shows a
`ValidationError` on purpose, demonstrates `SecretStr` refusing to print, and
checks whether your shell is shadowing your `.env`.

## Next

[02 — Fetching HTML](../02-fetch-html/) — where the knowledge base comes from.
