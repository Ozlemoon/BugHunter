"""Project document library: upload, paste, read, re-index, delete, search, AI answers and access."""

import json
from unittest.mock import patch

import httpx
import pytest
from starlette.testclient import TestClient

import web.app as web_app
from core import documents
from core.config import Settings
from core.database import Database
from core.documents import DocumentError, DocumentLibrary, safe_filename
from core.engine import HuntSession
from core.rag import DocumentIndex, split_passages

PASSWORD = "dogru-sifre-123"
REQUIREMENTS = (b"Iletisim formu\n\nForm gonderilince kullaniciya 'Mesajiniz alindi' yazan bir onay gosterilmelidir.\n\n"
                b"E-posta alani zorunludur ve @ isareti icermelidir; aksi halde 422 donulmelidir.")


class FakeIndex:
    """Stands in for the Chroma index: keyword overlap instead of embeddings."""

    def __init__(self):
        self.passages = {}  # source -> [(chunk, start, text)]

    def index_file(self, path):
        content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
        chunks = split_passages(content)
        self.passages[str(path)] = [(i, start, text) for i, (start, text) in enumerate(chunks, start=1)]
        return len(chunks)

    def delete_source(self, source):
        self.passages.pop(source, None)

    def search(self, question, top_k=3, sources=None):
        words = {w.lower() for w in question.split() if len(w) > 2}
        rows = []
        for source, chunks in self.passages.items():
            if sources is not None and source not in sources:
                continue
            for chunk, start, text in chunks:
                score = len(words & {w.lower().strip(".,;:'") for w in text.split()})
                rows.append({"source": source, "chunk": chunk, "start": start, "text": text,
                             "similarity": score / max(1, len(words))})
        return sorted(rows, key=lambda r: -r["similarity"])[:top_k]


def fake_chat(answer="Onay mesaji gosterilir [1]."):
    def handler(request):
        lines = [json.dumps({"message": {"content": w + " "}, "done": False}) for w in answer.split(" ")]
        lines.append(json.dumps({"message": {"content": ""}, "done": True}))
        return httpx.Response(200, content="\n".join(lines).encode())
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def library(tmp_path, guide=None, **kwargs):
    index = FakeIndex()
    settings = Settings(reports_dir=tmp_path / "reports", rag_db=tmp_path / "rag")
    lib = DocumentLibrary(settings, Database(tmp_path / "x.db"), index_factory=lambda fresh=False: index,
                          guide_path=guide or tmp_path / "no-guide.txt", **kwargs)
    return lib, index


# -- library -----------------------------------------------------------------------

def test_upload_search_read_and_delete(tmp_path):
    lib, index = library(tmp_path)
    doc = lib.add("gereksinimler.md", REQUIREMENTS, "ayse")
    assert doc.filename == "gereksinimler.md" and doc.chunks == 2 and doc.uploaded_by == "ayse"
    assert lib.read(doc.id).startswith("Iletisim formu")

    results = lib.search("form gonderilince ne gosterilmeli")
    assert results[0]["document_id"] == doc.id and "onay" in results[0]["text"]
    assert results[0]["filename"] == "gereksinimler.md" and results[0]["chunk"] == 1

    lib.delete(doc.id)
    assert lib.get(doc.id) is None and index.passages == {} and lib.search("form") == []
    assert not list((tmp_path / "documents").glob("*"))  # the stored file is removed too


def test_pasted_text_becomes_a_document(tmp_path):
    lib, _ = library(tmp_path)
    doc = lib.add_text("Giriş/Ekranı: kurallar", "Sifre en az 8 karakter olmalidir.", "ayse")
    assert doc.filename == "Giriş Ekranı kurallar.txt"  # no folders or odd characters in the name
    with pytest.raises(DocumentError, match="başlık"):
        lib.add_text("  ", "metin", "ayse")


@pytest.mark.parametrize("name, data, message", [
    ("belge.pdf", b"%PDF", ".txt ve .md"),
    ("belge.txt", b"", "boş"),
    ("belge.txt", b"\xff\xfe\x00bozuk", "UTF-8"),
    ("belge.txt", b"a" * (documents.MAX_DOCUMENT_BYTES + 1), "KB"),
])
def test_bad_uploads_are_refused(tmp_path, name, data, message):
    lib, _ = library(tmp_path)
    with pytest.raises(DocumentError, match=message):
        lib.add(name, data, "ayse")
    assert lib.documents() == []


def test_failed_indexing_keeps_nothing(tmp_path):
    class DownIndex(FakeIndex):
        def index_file(self, path):
            raise RuntimeError("Yerel Ollama bağlantısı başarısız")

    settings = Settings(rag_db=tmp_path / "rag")
    lib = DocumentLibrary(settings, Database(tmp_path / "x.db"), index_factory=lambda fresh=False: DownIndex())
    with pytest.raises(DocumentError, match="işlenemedi"):
        lib.add("a.txt", b"metin", "ayse")
    assert lib.documents() == [] and not list((tmp_path / "documents").glob("*"))


def paragraphs(*starts):
    """Paragraphs long enough to stay separate (short ones are merged into the next as headings)."""
    filler = " Bu paragraf bolme kuralini sinamak icin yeterince uzun tutulmustur ve tek basina bir parca olmalidir." * 2
    return "\n\n".join(start + filler for start in starts)


def test_user_guide_is_built_in_and_follows_file_changes(tmp_path):
    guide = tmp_path / "kilavuz.txt"
    guide.write_text(paragraphs("Ilk surum.", "Ikinci paragraf."), encoding="utf-8")
    lib, _ = library(tmp_path, guide=guide)
    first = lib.sync_builtin()
    assert first.builtin and first.chunks == 2 and not lib.is_stale(first)
    assert lib.sync_builtin().indexed_at == first.indexed_at  # unchanged: not re-indexed

    guide.write_text(paragraphs("Yeni surum.", "Yeni paragraf.", "Ucuncu paragraf."), encoding="utf-8")
    assert lib.is_stale(lib.get(first.id))
    updated = lib.sync_builtin()
    assert updated.id == first.id and updated.chunks == 3 and not lib.is_stale(updated)
    with pytest.raises(DocumentError, match="silinemez"):
        lib.delete(first.id)


def test_search_can_be_limited_to_one_document(tmp_path):
    lib, _ = library(tmp_path)
    a = lib.add("a.txt", b"Form onay mesaji gosterir.", "ayse")
    b = lib.add("b.txt", b"Form hata mesaji gosterir.", "ayse")
    assert {r["document_id"] for r in lib.search("form mesaji")} == {a.id, b.id}
    assert {r["document_id"] for r in lib.search("form mesaji", document_ids=[b.id])} == {b.id}


def test_ai_answer_streams_and_survives_ollama_down(tmp_path):
    lib, _ = library(tmp_path, http_client=fake_chat())
    lib.add("g.txt", REQUIREMENTS, "ayse")
    passages = lib.search("form onay")
    events = list(lib.answer("form onay", passages))
    assert "".join(e["text"] for e in events if e["type"] == "token").strip() == "Onay mesaji gosterilir [1]."
    assert events[-1] == {"type": "done"}

    def down():
        return httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))))
    offline, _ = library(tmp_path / "o", http_client=down)
    error = list(offline.answer("form", passages))[-1]
    assert error["type"] == "error" and "ollama serve" in error["detail"]
    assert list(lib.answer("x", []))[0]["text"].startswith("Belgelerde")


def test_safe_filename():
    assert safe_filename("../../etc/passwd.txt") == "passwd.txt"
    assert safe_filename(r"C:\gizli\not.md") == "not.md"
    assert safe_filename("rapor.exe") == "rapor.txt"


# -- the real Chroma index (embeddings faked) -------------------------------------------

def test_real_index_filters_by_document_and_deletes(tmp_path):
    def embed(texts):
        vocab = ["onay", "hata", "form", "sifre"]
        return [[1.0 if w in t.lower() else 0.0 for w in vocab] + [0.01] for t in texts]

    with patch.object(DocumentIndex, "embed", staticmethod(embed)):
        index = DocumentIndex(tmp_path / "chroma")
        a, b = tmp_path / "a.txt", tmp_path / "b.md"
        a.write_text("Form onay gosterir.", encoding="utf-8")
        b.write_text(paragraphs("Form hata gosterir.", "Sifre kurali."), encoding="utf-8")
        assert index.index_file(a) == 1 and index.index_file(b) == 2
        only_b = index.search("form hata", top_k=5, sources=[str(b.resolve())])
        assert {r["filename"] for r in only_b} == {"b.md"}
        index.delete_source(str(a.resolve()))
        assert {r["filename"] for r in index.search("form onay", top_k=5)} == {"b.md"}
        assert index.search("form", sources=[]) == []


# -- web -----------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    session = HuntSession(settings=Settings(reports_dir=tmp_path / "reports"))
    monkeypatch.setattr(web_app, "session", session)
    index = FakeIndex()
    lib = DocumentLibrary(Settings(reports_dir=tmp_path / "reports", rag_db=tmp_path / "rag"), session.store.db,
                          index_factory=lambda fresh=False: index, http_client=fake_chat(),
                          guide_path=tmp_path / "no-guide.txt")
    monkeypatch.setattr(web_app, "get_library", lambda: lib)
    clients = {}
    for name in ("ayse", "mehmet"):
        clients[name] = TestClient(web_app.app)
        assert clients[name].post("/api/auth/register", json={"username": name, "password": PASSWORD}).status_code == 200
    return clients


def test_web_document_flow(client):
    ayse, mehmet = client["ayse"], client["mehmet"]
    uploaded = ayse.post("/api/docs", files={"file": ("gereksinim.txt", REQUIREMENTS, "text/plain")})
    assert uploaded.status_code == 200, uploaded.text
    doc = uploaded.json()
    pasted = ayse.post("/api/docs/text", json={"title": "Sifre kurallari", "content": "Sifre en az 8 karakter."}).json()

    overview = mehmet.get("/api/docs").json()  # the library is shared by the project
    assert {d["filename"] for d in overview["documents"]} == {"gereksinim.txt", "Sifre kurallari.txt"}
    assert overview["stats"] == {"documents": 2, "chunks": 3} and overview["limits"]["max_kb"] == 512

    found = mehmet.post("/api/docs/search", json={"question": "form gonderilince onay"}).json()["results"]
    assert found[0]["document_id"] == doc["id"]
    scoped = mehmet.post("/api/docs/search", json={"question": "sifre", "document_ids": [pasted["id"]]}).json()["results"]
    assert {r["document_id"] for r in scoped} == {pasted["id"]}
    assert "content" in mehmet.get(f"/api/docs/{doc['id']}").json()
    assert mehmet.post(f"/api/docs/{doc['id']}/reindex").status_code == 200

    assert mehmet.delete(f"/api/docs/{doc['id']}").status_code == 403  # only the uploader deletes
    assert ayse.delete(f"/api/docs/{doc['id']}").json() == {"status": "deleted"}
    assert ayse.get(f"/api/docs/{doc['id']}").status_code == 404


def test_web_ai_answer_and_access(client):
    ayse = client["ayse"]
    ayse.post("/api/docs", files={"file": ("g.txt", REQUIREMENTS, "text/plain")})
    response = ayse.post("/api/docs/ask", json={"question": "form onay"})
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0]["type"] == "sources" and events[0]["items"]
    assert "[1]" in "".join(e.get("text", "") for e in events) and events[-1]["type"] == "done"

    bad = ayse.post("/api/docs", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert bad.status_code == 422
    assert TestClient(web_app.app).get("/api/docs").status_code == 401
    html = ayse.get("/").text
    for text in ("Belge kütüphanesi", 'id="docs-query"', 'id="docs-file"', "Metin yapıştır", 'id="docs-viewer"'):
        assert text in html
