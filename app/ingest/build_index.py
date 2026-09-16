"""Embed the chunks and build the FAISS index.

    python -m app.ingest.build_index
    python -m app.ingest.build_index --query "getting around Singapore by train"

Two design points worth knowing:

**Cosine similarity, not squashed distance.** The index normalises vectors and
uses inner product, and the relevance score function is the identity, so a
"relevance score" here *is* the cosine similarity between query and chunk.
That matters because `settings.relevance_floor` -- the threshold below which the
assistant says "not in the knowledge base" rather than inventing an answer --
has to be a number a human can reason about and tune.

**Atomic swap.** The index is built into a temporary directory and only moved
into place once it is complete. A chat request during a rebuild keeps being
served by the previous index, and a rebuild that fails leaves the working index
untouched rather than half-written.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.config import settings
from app.ingest.chunk import build_chunks, chunk_counts_by_source
from app.ingest.registry import SourceRegistry, utc_now_iso

MANIFEST_NAME = "manifest.json"
INDEX_NAME = "index"
MANIFEST_VERSION = 1


def get_embeddings() -> Embeddings:
    """The embedding model: FastEmbed ONNX, local, keyless, no torch.

    The first call downloads roughly 130 MB of model into the cache directory
    and is slow; later calls are fast.
    """
    return FastEmbedEmbeddings(
        model_name=settings.embedding_model,
        cache_dir=str(settings.project_root / ".cache" / "fastembed"),
    )


def _identity(score: float) -> float:
    """Relevance score function.

    Vectors are L2-normalised and compared by inner product, so the raw score
    already *is* cosine similarity. Returning it unchanged keeps
    `relevance_floor` interpretable instead of a squashed distance.
    """
    return score


def new_vector_store(chunks: list[Document], embeddings: Embeddings) -> FAISS:
    return FAISS.from_documents(
        chunks,
        embeddings,
        normalize_L2=True,
        distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT,
        relevance_score_fn=_identity,
    )


def load_vector_store(embeddings: Embeddings | None = None) -> FAISS:
    """Load the persisted index.

    `allow_dangerous_deserialization` is required because FAISS stores its
    docstore as a pickle. It is safe here for a specific reason: the only thing
    we ever load is an index this application built itself, on this machine,
    from `data/kb/`. We never load a third-party index file.
    """
    if not index_exists():
        raise FileNotFoundError(
            f"No FAISS index at {settings.index_dir}. "
            "Run `python -m app.ingest.build_index` first."
        )
    return FAISS.load_local(
        str(settings.index_dir),
        embeddings or get_embeddings(),
        index_name=INDEX_NAME,
        allow_dangerous_deserialization=True,
        normalize_L2=True,
        distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT,
        relevance_score_fn=_identity,
    )


def index_exists() -> bool:
    return (settings.index_dir / f"{INDEX_NAME}.faiss").is_file()


def read_manifest() -> dict | None:
    """Index metadata, or None when there is no usable index."""
    path = settings.manifest_path
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_manifest(directory: Path, chunks: list[Document], dimension: int) -> dict:
    counts = chunk_counts_by_source(chunks)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "built_at": utc_now_iso(),
        "embedding_model": settings.embedding_model,
        "dimension": dimension,
        "distance_strategy": "cosine (normalised inner product)",
        "chunk_count": len(chunks),
        "source_count": len(counts),
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "chunks_by_source": dict(sorted(counts.items())),
    }
    (directory / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _swap_into_place(staging: Path, destination: Path) -> None:
    """Replace `destination` with `staging` as atomically as Windows allows.

    There is no single syscall that swaps two directories on Windows, so the
    old directory is moved aside first and only deleted once the new one is in
    place. If the move of the new directory fails, the old one is restored.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous = destination.with_name(destination.name + ".previous")
    if previous.exists():
        shutil.rmtree(previous)

    had_previous = destination.exists()
    if had_previous:
        destination.rename(previous)
    try:
        staging.rename(destination)
    except OSError:
        if had_previous:
            previous.rename(destination)
        raise
    if had_previous:
        shutil.rmtree(previous, ignore_errors=True)


def rebuild_index(
    chunks: list[Document] | None = None,
    *,
    registry: SourceRegistry | None = None,
    progress: callable | None = None,
) -> dict:
    """Chunk, embed, persist and swap in a new index. Returns the manifest.

    `progress` is called with short status strings so the /admin job log can
    show what stage a rebuild is at.
    """

    def report(message: str) -> None:
        if progress is not None:
            progress(message)

    registry = registry or SourceRegistry.load()

    if chunks is None:
        report("chunking documents")
        chunks = build_chunks(registry)
    if not chunks:
        raise ValueError(
            "No chunks to index. The knowledge base is empty -- run "
            "`python -m app.ingest.fetch_sources` or add a source in /admin."
        )

    report(f"embedding {len(chunks)} chunks")
    embeddings = get_embeddings()
    store = new_vector_store(chunks, embeddings)
    dimension = int(store.index.d)

    report("writing index")
    staging_parent = settings.index_dir.parent
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="index-staging-", dir=staging_parent))
    try:
        store.save_local(str(staging), index_name=INDEX_NAME)
        manifest = _write_manifest(staging, chunks, dimension)
        report("swapping index into place")
        _swap_into_place(staging, settings.index_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # Chunk counts are owned here, not by the fetcher, so the registry and the
    # index cannot disagree about how much of each source is searchable.
    registry.set_chunk_counts(chunk_counts_by_source(chunks))
    registry.save()

    report(f"done: {len(chunks)} chunks, {dimension} dimensions")
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_manifest(manifest: dict) -> None:
    print()
    print(f"index built at {manifest['built_at']}")
    print(f"  model      : {manifest['embedding_model']}")
    print(f"  dimension  : {manifest['dimension']}")
    print(f"  distance   : {manifest['distance_strategy']}")
    print(f"  chunks     : {manifest['chunk_count']:,} from "
          f"{manifest['source_count']} sources")
    print(f"  location   : {settings.index_dir}")


def _run_query(query: str, k: int) -> None:
    store = load_vector_store()
    results = store.similarity_search_with_relevance_scores(query, k=k)
    print()
    print(f'query: "{query}"')
    print()
    for score, document in ((s, d) for d, s in results):
        metadata = document.metadata
        print(f"  {score:.3f}  {metadata['section_path']}")
        print(f"         {metadata['source_title']}  [{', '.join(metadata['categories'])}]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the FAISS index.")
    parser.add_argument("--query", metavar="TEXT",
                        help="Skip building; query the existing index instead.")
    parser.add_argument("--k", type=int, default=5, help="Results for --query.")
    parser.add_argument("--status", action="store_true",
                        help="Print the current index manifest and exit.")
    args = parser.parse_args(argv)

    if args.status:
        manifest = read_manifest()
        if manifest is None:
            print(f"No index at {settings.index_dir}.")
            return 1
        _print_manifest(manifest)
        return 0

    if args.query:
        _run_query(args.query, args.k)
        return 0

    manifest = rebuild_index(progress=lambda message: print(f"  ... {message}"))
    _print_manifest(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
