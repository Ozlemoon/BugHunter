"""Index UTF-8 documents and retrieve relevant passages from local Chroma."""

import argparse
import hashlib
import json
import re
from pathlib import Path

import chromadb
import httpx

from core.config import get_settings


def split_passages(content: str, chunk_size: int = 600, overlap: int = 100) -> list[tuple[int, str]]:
    """
    Splits a document into (start_offset, passage) pairs that follow its structure:

    - each paragraph (blank-line separated) is its own passage, so one topic is
      never glued to the next; only short pieces such as headings are merged
      into the paragraph that follows them
    - a paragraph longer than `chunk_size` is split at sentence ends
    - a single sentence longer than `chunk_size` falls back to overlapping windows

    Every passage is an exact slice of `content` starting at its offset.
    """
    short = chunk_size // 4
    pieces: list[tuple[int, int, bool]] = []  # (start, end, is_whole_paragraph)
    for match in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", content, flags=re.S):
        start, end = match.span()
        if end - start <= chunk_size:
            pieces.append((start, end, True))
        else:
            pieces.extend((s, e, False) for s, e in _split_long(content, start, end, chunk_size, overlap))

    chunks: list[tuple[int, str]] = []
    current: tuple[int, int] | None = None
    current_is_heading = False
    for start, end, whole in pieces:
        if current and current_is_heading and end - current[0] <= chunk_size:
            current = (current[0], end)  # attach a heading to the paragraph below it
            current_is_heading = False
            continue
        if current:
            chunks.append((current[0], content[current[0]:current[1]]))
        current = (start, end)
        current_is_heading = whole and end - start <= short
    if current:
        chunks.append((current[0], content[current[0]:current[1]]))
    return chunks


def _split_long(content: str, start: int, end: int, chunk_size: int, overlap: int) -> list[tuple[int, int]]:
    """Packs whole sentences of content[start:end] into spans of at most chunk_size."""
    sentences = [(start + m.start(), start + m.end())
                 for m in re.finditer(r"\S.*?(?:[.!?](?=\s)|$)", content[start:end], flags=re.S)]
    spans: list[tuple[int, int]] = []
    current: tuple[int, int] | None = None
    for s_start, s_end in sentences:
        if s_end - s_start > chunk_size:
            if current:
                spans.append(current)
                current = None
            step = chunk_size - overlap
            for offset in range(s_start, s_end, step):
                spans.append((offset, min(offset + chunk_size, s_end)))
                if offset + chunk_size >= s_end:
                    break
            continue
        if current and s_end - current[0] > chunk_size:
            spans.append(current)
            current = None
        current = (current[0], s_end) if current else (s_start, s_end)
    if current:
        spans.append(current)
    return spans


class StaleIndexError(RuntimeError):
    """This process' cached view of the index is older than what another process wrote."""


class DocumentIndex:
    def __init__(self, database: Path, fresh: bool = False):
        if fresh:
            # Chroma caches one client per path per process; drop it to see writes made by
            # another process (e.g. `main.py rag index` while the dashboard is running).
            from chromadb.api.client import SharedSystemClient

            SharedSystemClient.clear_system_cache()
        client = chromadb.PersistentClient(path=str(database))
        self.collection = client.get_or_create_collection(
            "bughunter-embeddinggemma",
            embedding_function=None,
            configuration={"hnsw": {"space": "cosine"}},
        )

    @staticmethod
    def embed(texts: list[str]) -> list[list[float]]:
        try:
            settings = get_settings()
            response = httpx.post(
                f"{settings.ollama_url}/api/embed",
                json={"model": settings.rag_embed_model, "input": texts, "truncate": False},
                timeout=60,
                trust_env=False,
            )
            response.raise_for_status()
            vectors = response.json()["embeddings"]
            if len(vectors) != len(texts) or any(not vector for vector in vectors):
                raise ValueError("Beklenmeyen vektör sayısı veya boş vektör.")
            return vectors
        except httpx.TimeoutException as error:
            raise RuntimeError(
                "Ollama belge veya soru vektörünü zamanında üretemedi."
            ) from error
        except httpx.RequestError as error:
            raise RuntimeError(f"Yerel Ollama bağlantısı başarısız: {error}") from error
        except httpx.HTTPStatusError as error:
            raise RuntimeError(
                f"Ollama HTTP {response.status_code}: {response.text}"
            ) from error
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError(f"Ollama vektör yanıtı geçersiz: {error}") from error

    def index_file(self, path: Path, chunk_size: int = 600, overlap: int = 100) -> int:
        if not 0 <= overlap < chunk_size:
            raise ValueError("Örtüşme sıfır veya daha büyük ve parça boyutundan küçük olmalı.")
        if path.suffix.lower() != ".txt":
            raise ValueError("Yalnızca TXT belgeleri desteklenir.")

        path = path.resolve()
        source = str(path)
        content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
        chunks = split_passages(content, chunk_size, overlap)
        if not chunks:
            raise ValueError(f"Belge boş: {path.name}")

        ids = [
            hashlib.sha256(f"{source}:{start}".encode()).hexdigest()
            for start, _ in chunks
        ]
        documents = [passage for _, passage in chunks]
        metadata = [
            {"source": source, "filename": path.name, "chunk": index, "start": start}
            for index, (start, _) in enumerate(chunks, start=1)
        ]
        embeddings = []
        for start in range(0, len(documents), 32):
            embeddings.extend(self.embed(documents[start:start + 32]))
        previous_ids = self.collection.get(where={"source": source}, include=[])["ids"]
        self.collection.upsert(
            ids=ids, documents=documents, embeddings=embeddings, metadatas=metadata
        )
        stale_ids = set(previous_ids).difference(ids)
        if stale_ids:
            self.collection.delete(ids=sorted(stale_ids))
        return len(ids)

    def search(self, question: str, top_k: int = 3) -> list[dict]:
        question = question.strip()
        if not question or top_k < 1:
            raise ValueError("Soru boş olamaz ve Top-K en az 1 olmalı.")
        count = self.collection.count()
        if not count:
            return []
        results = self.collection.query(
            query_embeddings=self.embed([question]),
            n_results=min(top_k, count),
            include=["documents", "metadatas", "distances"],
        )
        rows = list(zip(results["metadatas"][0], results["documents"][0], results["distances"][0]))
        if any(metadata is None or text is None for metadata, text, _ in rows):
            raise StaleIndexError("Belge indeksi başka bir süreç tarafından güncellendi.")
        return [{**metadata, "text": text, "similarity": 1.0 - distance} for metadata, text, distance in rows]


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="BugHunter belge indeksleme ve arama")
    parser.add_argument("--db", type=Path, default=settings.rag_db)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index")
    index.add_argument("files", nargs="+", type=Path)
    index.add_argument("--chunk-size", type=int, default=600)
    index.add_argument("--overlap", type=int, default=100)
    search = commands.add_parser("search")
    search.add_argument("question")
    search.add_argument("--top-k", type=int, default=3)
    ask = commands.add_parser("ask", help="Belgelerden doğrulanmış alıntılarla yanıt ver")
    ask.add_argument("question")
    ask.add_argument("--top-k", type=int, default=6)
    ask.add_argument("--top-n", type=int, default=3)
    ask.add_argument("--model", default=settings.rag_chat_model)
    args = parser.parse_args()

    try:
        store = DocumentIndex(args.db)
        if args.command == "index":
            for path in args.files:
                count = store.index_file(path, args.chunk_size, args.overlap)
                print(f"{path.name}: {count} belge parçası kaydedildi.")
        elif args.command == "search":
            print(json.dumps(store.search(args.question, args.top_k), ensure_ascii=False, indent=2))
        else:
            from core.rag_answer import answer_question

            result = answer_question(store, args.question, args.top_k, args.top_n, args.model)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"Hata: {error}\n")


if __name__ == "__main__":
    main()
