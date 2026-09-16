"""Lesson 00 - print the map with this project's real numbers attached.

    .venv/Scripts/python.exe learn/00-map/run.py

Imports nothing from app/. It just looks at the files on disk, so the diagram
in README.md stops being abstract.
"""

import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} TB"


def rule(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


print("NorthStar, measured")
print("=" * 62)
print(f"project root: {ROOT}")

# --------------------------------------------------------------- Flow A
rule("Flow A - ingestion (runs rarely, offline, no LLM)")

kb = ROOT / "data" / "kb"
docs = sorted(kb.glob("*.md"))
print(f"[1-2] knowledge base documents  : {len(docs)} markdown files")
print(f"      total size                : {human(sum(d.stat().st_size for d in docs))}")
if docs:
    biggest = max(docs, key=lambda d: d.stat().st_size)
    print(f"      biggest                   : {biggest.name} "
          f"({human(biggest.stat().st_size)})")

registry = kb / "sources.json"
if registry.is_file():
    data = json.loads(registry.read_text(encoding="utf-8"))
    records = data.get("sources", []) if isinstance(data, dict) else data
    if isinstance(records, list) and records:
        places = sorted({(r.get("destination") or "?") for r in records})
        print(f"      destinations covered      : {', '.join(places)}")

manifest = ROOT / "data" / "index" / "manifest.json"
if manifest.is_file():
    m = json.loads(manifest.read_text(encoding="utf-8"))
    print(f"[3]   chunks in the index       : {m.get('chunk_count', '?')}")
    print(f"[4]   embedding model           : {m.get('embedding_model', '?')}")
    print(f"      built at                  : {m.get('built_at', '?')}")

for name, label in (("index.faiss", "the vectors"), ("index.pkl", "the text")):
    f = ROOT / "data" / "index" / name
    if f.is_file():
        print(f"[5]   {name:<12} ({label:<11}): {human(f.stat().st_size)}")

# --------------------------------------------------------------- Flow B
rule("Flow B - a chat turn (runs on every question)")

db = ROOT / "data" / "conversations.sqlite3"
if db.is_file():
    print(f"[7]   conversation store        : {human(db.stat().st_size)}")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        threads = con.execute(
            "select count(distinct thread_id) from checkpoints"
        ).fetchone()[0]
        points = con.execute("select count(*) from checkpoints").fetchone()[0]
        con.close()
        print(f"      conversations saved       : {threads}")
        print(f"      checkpoints (several/turn): {points}")
    except sqlite3.Error as exc:
        print(f"      (could not read: {exc})")
else:
    print("[7]   conversation store        : not created yet")

env_file = ROOT / ".env"
in_dotenv = env_file.is_file() and any(
    line.startswith("GROQ_API_KEY=") and line.strip() != "GROQ_API_KEY="
    for line in env_file.read_text(encoding="utf-8").splitlines()
)
in_shell = bool(os.environ.get("GROQ_API_KEY"))
print(f"[8]   GROQ_API_KEY in .env      : {in_dotenv}")
print(f"      GROQ_API_KEY in shell env : {in_shell}")
print("[10]  MCP servers               : weather, currency (stdio subprocesses)")

# --------------------------------------------------------------- code
rule("The code")

for folder, label in ((ROOT / "app", "app/"), (ROOT / "learn", "learn/")):
    if not folder.is_dir():
        continue
    files = [p for p in folder.rglob("*.py") if "__pycache__" not in str(p)]
    lines = sum(
        len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        for p in files
    )
    print(f"{label:<8} {len(files):>3} python files, {lines:>5} lines")

rule("Dependencies")

req = ROOT / "requirements.txt"
if req.is_file():
    pins = [
        line.split("==")[0].strip()
        for line in req.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#") and "==" in line
    ]
    print(f"declared in requirements.txt : {len(pins)}")
    print("  " + ", ".join(pins))
    print()
    print("Lesson 13 rebuilds the app with 3 of these:")
    print("  httpx (network), fastembed (embeddings), numpy (search)")

print()
print("Next: learn/01-config/README.md")
