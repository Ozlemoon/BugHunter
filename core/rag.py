"""Index UTF-8 documents and retrieve relevant passages from local Chroma."""

import argparse
import hashlib
import json
from pathlib import Path

import chromadb
import httpx


class DocumentIndex:
    def __init__(self, database: Path):
        client = chromadb.PersistentClient(path=str(database))
        self.collection = client.get_or_create_collection(
            "bughunter-embeddinggemma",
            embedding_function=None,
            configuration={"hnsw": {"space": "cosine"}},
        )

    @staticmethod
    def embed(texts: list[str]) -> list[list[float]]:
        try:
            response = httpx.post(
                "http://127.0.0.1:11434/api/embed",
                json={"model": "embeddinggemma", "input": texts, "truncate": False},
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
        content = path.read_text(encoding="utf-8-sig").strip()
        chunks = []
        for start in range(0, len(content), chunk_size - overlap):
            end = start + chunk_size
            passage = content[start:end]
            if passage.strip():
                chunks.append((start, passage))
            if end >= len(content):
                break
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
        return [
            {**metadata, "text": text, "similarity": 1.0 - distance}
            for metadata, text, distance in zip(
                results["metadatas"][0], results["documents"][0], results["distances"][0]
            )
        ]


def main() -> None:
    parser = argparse.ArgumentParser(description="BugHunter belge indeksleme ve arama")
    parser.add_argument("--db", type=Path, default=Path("run_artifacts/rag"))
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
    ask.add_argument("--model", default="qwen3.5")
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
