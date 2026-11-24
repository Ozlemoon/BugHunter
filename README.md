 BugHunter AI — İnsan Onaylı Otonom Test ve Hata Analiz Ajanı

[![CI](https://github.com/Ozlemoon/BugHunter/actions/workflows/ci.yml/badge.svg)](https://github.com/Ozlemoon/BugHunter/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

BugHunter AI, **REST API'leri** (OpenAPI/Swagger) ve **web uygulamalarını** seçtiğiniz kriterlere göre otonom olarak test eder. Bulduğu her hatayı bağımsız çalışan bir kanıt scriptiyle belgeler ve düzeltme için bir **Unified Diff** hazırlar. Yamayı **yalnızca siz onaylarsanız** uygular, ardından tüm testleri yeniden koşturur. Yan etki varsa yamayı otomatik geri alır.

```text
Hedef + kriterler ─► Test üret ─► Koştur ─► Kanıt topla ─► Onay iste ─► Yamala ─► Regresyon testi
 (OpenAPI / URL)    (deterministik)          (repro script)  (HITL kilidi) (yedekli)  (otomatik geri alma)
```

Ücretli API anahtarı gerekmez; her şey yerelde çalışır.

---

## ✨ Neler yapabilir?

| Kriter | API | Web |
| :--- | :--- | :--- |
| **FUNCTIONAL** | Mutlu yol, belgelenmiş **yanıt şeması** doğrulaması | Geçerli form gönderimi |
| **BOUNDARY** | Var olmayan kaynak, negatif değer, eksik zorunlu alan, yanlış tip | Bozuk/boş form girdileri |
| **SECURITY** | Kimlik bilgisiz ve **geçersiz token**'lı yıkıcı istekler, belgelenmiş `security` gereksinimleri | **Yansıyan XSS**, eksik güvenlik başlıkları |
| **PERFORMANCE** | SLA eşiği (yalnızca salt okunur isteklerde) | Sayfa yükleme süresi |
| **WEB_VALIDATION** | — | Kırık bağlantı ve eksik varlıklar (aynı kökenli sayfalar taranır) |

**Güvenilir sonuçlar için tasarım kararları**
- Testler sırayla koşar: önce okumalar, sonra yazmalar, en son silmeler. Böylece bir `DELETE` önceki testleri bozmaz.
- Performans ölçümü hiçbir zaman veri oluşturmaz veya silmez.
- Kimlik bilgisiz silme denemeleri komşu bir kaynağa (ör. `/products/2`) yapılır; işlevsel test kendi kaynağını korur.
- Değerler önce spec'teki `example`/`default` alanlarından alınır, uydurulmaz.

**İnsan onay kilidi (Human-in-the-Loop)**
- Onaylanmamış yama `PermissionError` ile engellenir.
- Yamalar endpoint bazlı açık bir **kural kaydından** gelir. Eşleşen güvenli kural yoksa yama önerilmez, yalnızca kanıt sunulur.
- Dosya öneriden sonra değiştiyse yama reddedilir. Diff hunk hunk, bağlam doğrulanarak uygulanır; CRLF/LF korunur.
- Uygulamadan önce `.bughunter.bak` yedeği alınır. Yama hatayı çözmezse veya regresyon yaratırsa otomatik geri alınır.
- Yalnızca `BUGHUNTER_PATCH_ROOTS` altındaki dosyalar yamalanabilir.

**Raporlar ve geçmiş:** Her çalışma `reports/runs/` altına kaydedilir. Çalışmalar **HTML** (çevrimdışı, paylaşılabilir), **JUnit XML** (CI) veya **JSON** olarak dışa aktarılabilir. Token ve çerez gibi kimlik bilgileri diske ve raporlara asla yazılmaz.

---

## ⚡ Kurulum

```bash
git clone https://github.com/Ozlemoon/BugHunter.git
cd BugHunter
python -m venv .venv
# Windows: .\.venv\Scripts\activate    |   Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt     # yalnızca çalıştırmak için: requirements.txt
```

## 🎮 Kullanım

### 1. Uçtan uca demo
```bash
python main.py demo
```
| Servis | Adres |
| :--- | :--- |
| Kontrol paneli | http://127.0.0.1:8000 |
| Bilerek hatalı demo API | http://127.0.0.1:8001/openapi.json (admin: `Bearer admin-token`) |
| Bilerek hatalı demo web | http://127.0.0.1:8002 |

Panelde **Demo API adresi** düğmesine tıklayın ve **Testi başlat**'a basın. Bulguları inceleyip bir düzeltmeyi onaylayın. Demo hedefleri `playground/` klasörünü izler ve yamadan bir saniye sonra yeniden yüklenir. Regresyon testi bu sayede yamalı kodu ölçer.

> Demo yamaları `playground/*.py` dosyalarını gerçekten değiştirir. Panelden **Geri al** diyebilir veya `git checkout playground/` ile sıfırlayabilirsiniz.

### 2. Terminalde etkileşimli av
```bash
python main.py hunt --url http://127.0.0.1:8001/openapi.json --auth "Bearer admin-token"
python main.py hunt --type WEB --url http://127.0.0.1:8002/
```
Her düzeltme için diff gösterilir ve onay sorulur (varsayılan cevap **hayır**). Kendi projeniz için `--source yol/dosya.py` verin.

### 3. CI hattında başsız tarama
```bash
python main.py scan --url https://staging.ornek.com/openapi.json \
  --auth "Bearer $TOKEN" --junit reports/bughunter.xml --html reports/bughunter.html --fail-on HIGH
```
`scan` soru sormaz ve koda asla dokunmaz.

| Çıkış kodu | Anlamı |
| :--- | :--- |
| `0` | Eşiğe ulaşan bulgu yok |
| `1` | `--fail-on` (CRITICAL/HIGH/MEDIUM/LOW) seviyesinde veya üstünde bulgu var |
| `2` | Hedefe ulaşılamadı veya spec okunamadı |

GitHub Actions örneği:
```yaml
- run: pip install -r requirements.txt
- run: python main.py scan --url ${{ vars.STAGING_OPENAPI_URL }} --junit bughunter.xml --fail-on HIGH
  env:
    BUGHUNTER_REQUEST_TIMEOUT: "20"
- uses: actions/upload-artifact@v4
  if: always()
  with: { name: bughunter-report, path: bughunter.xml }
```

### 4. Geçmiş ve raporlar
```bash
python main.py runs                                  # son çalışmalar
python main.py report 20261120-163051-d9ff6b --format html -o rapor.html
```
Kanıt scriptleri bağımsız çalışır. Hata hâlâ yeniden üretiliyorsa `exit 1`, düzeldiyse `exit 0` döner. Kimlik bilgisi gerekiyorsa `BUGHUNTER_AUTH_HEADER` ortam değişkeninden okunur.

### 5. Proje belgelerine soru sorma (yerel RAG, isteğe bağlı)
[Ollama](https://ollama.com) gerektirir (`embeddinggemma` + `qwen3.5`).
```bash
python main.py rag index docs/*.txt
python main.py rag ask "İletişim formu gönderilince ne gösterilmeli?"
python -m core.rag_eval sorular.json --model qwen3.5    # erişim/alıntı kalitesi ölçümü
```

---

## 🐳 Docker
```bash
docker compose up --build                      # panel + demo hedefleri (127.0.0.1'e bağlı)
docker compose --profile rag up --build        # + yerel Ollama
```
Paneli başka makinelere açacaksanız mutlaka `BUGHUNTER_API_TOKEN` tanımlayın. Panel kaynak koda yama uygulayabilir.

## ⚙️ Yapılandırma
Tüm ayarlar `BUGHUNTER_*` ortam değişkenleri veya `.env` dosyasıyla verilir (bkz. [.env.example](.env.example)).

| Değişken | Varsayılan | Açıklama |
| :--- | :--- | :--- |
| `BUGHUNTER_HOST` / `BUGHUNTER_PORT` | `127.0.0.1` / `8000` | Panel adresi |
| `BUGHUNTER_API_TOKEN` | — | Tanımlıysa tüm `/api/*` istekleri `X-BugHunter-Token` başlığı ister |
| `BUGHUNTER_PATCH_ROOTS` | proje klasörü | Yamalanabilecek dizinler (`os.pathsep` ile ayrılır) |
| `BUGHUNTER_REQUEST_TIMEOUT` | `15` | İstek zaman aşımı (sn) |
| `BUGHUNTER_DEFAULT_SLA_MS` | `500` | Varsayılan SLA eşiği |
| `BUGHUNTER_PATCH_SETTLE_SECONDS` | `1.5` | Yamadan sonra hedefin yeniden yüklenmesi için bekleme |
| `BUGHUNTER_REPORTS_DIR` | `reports` | Çalışma geçmişi ve raporlar |
| `BUGHUNTER_OLLAMA_URL` | `http://127.0.0.1:11434` | RAG için Ollama |

## 🔌 REST API (panel arka ucu)
| Metot | Yol | Açıklama |
| :--- | :--- | :--- |
| `POST` | `/api/runs` | Yeni çalışma (`target_url`, `target_type`, `criteria`, `sla_threshold_ms`, `auth_header`, `source_file`, `crawl_pages`) |
| `GET` | `/api/runs`, `/api/runs/{id}` | Geçmiş ve çalışma ayrıntısı |
| `GET` | `/api/runs/{id}/report.{html,xml,json}` | Rapor indirme |
| `POST` | `/api/runs/{id}/fixes/{fix_id}/{approve,reject,rollback}` | İnsan onay kilidi |
| `GET` | `/healthz` | Sağlık kontrolü |

Etkileşimli belgeler: http://127.0.0.1:8000/docs

---

## 🧪 Demo hedeflerindeki bilinçli hatalar

| Hedef | Endpoint | Hata | Ajanın davranışı |
| :--- | :--- | :--- | :--- |
| API | `POST /orders` | Negatif adet kabul ediliyor, stok artıyor | 422 bekler, `quantity <= 0` kontrolü önerir |
| API | `POST /products` | Negatif fiyat/stok kabul ediliyor | 422 bekler, negatif değer kontrolü önerir |
| API | `GET /users/{id}/profile` | Olmayan profilde `KeyError` → 500 | 404 kontrolü önerir |
| API | `DELETE /products/{id}` | Kimlik doğrulama yok | Kimliksiz ve sahte token'lı silmeyi yakalar, admin token zorunluluğu önerir |
| API | `GET /products/heavy-search` | 1,3 sn gecikme (SLA aşımı) | SLA kanıtı sunar (otomatik yama yok) |
| WEB | `POST /submit-contact` | `@` içermeyen e-postada 500 | E-posta doğrulaması önerir |
| WEB | `GET /search?q=` | Yansıyan XSS | `html.escape` önerir |
| WEB | Tüm sayfalar | Güvenlik başlıkları yok | Başlık middleware'i önerir |
| WEB | `/legacy-api-docs`, logo | Kırık bağlantı / eksik varlık | Kanıtlar (otomatik yama yok) |

Test paketi bu tabloyu doğrular. Üretilen testler **tam olarak** bu hataları bulmalı, her biri doğru kurala eşlenmeli ve tüm yamalar uygulandıktan sonra yalnızca otomatik düzeltilemeyenler kalmalıdır.

## 📂 Proje yapısı
```
core/
  engine.py        Çalışma orkestrasyonu (HuntSession): planla → koştur → kanıtla → onayla → doğrula
  generator.py     Kriter bazlı, deterministik test üretici
  runner.py        HTTP koşturucu ve doğrulamalar (şema, XSS yansıması, başlıklar, SLA)
  analyzer.py      Önem derecesi, kök neden, bağımsız kanıt scripti
  patcher.py       Fix kural kaydı, hunk bazlı diff uygulama, insan onay kilidi
  verifier.py      Regresyon karşılaştırması ve otomatik geri alma
  reporting.py     JSON / JUnit / HTML raporları      storage.py  Çalışma geçmişi
  config.py        BUGHUNTER_* ayarları               redaction.py  Kimlik bilgisi maskeleme
  targets/         OpenAPI ayrıştırıcı, web tarayıcı   rag*.py      Yerel belge soru-cevap
web/               FastAPI paneli + arayüz
playground/        Bilerek hatalı demo API ve web uygulaması
cli.py, main.py    Komut satırı (hunt, scan, runs, report, web, demo, playground, rag)
tests/             Birim + uçtan uca testler
```

## 🛠️ Geliştirme
```bash
ruff check .
pytest --cov=core --cov=web
```

## 📄 Lisans
MIT
