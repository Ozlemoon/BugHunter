# BugHunter AI

[![CI](https://github.com/Ozlemoon/BugHunter/actions/workflows/ci.yml/badge.svg)](https://github.com/Ozlemoon/BugHunter/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

BugHunter, REST API'leri ve web sitelerini otomatik olarak test eden bir araçtır. Hedefin adresini verirsiniz; testleri kendisi üretip çalıştırır, bulduğu hataları kanıtıyla birlikte raporlar ve çözüm önerir. Koda bir düzeltme uygulaması gerekiyorsa bunu **yalnızca siz onaylarsanız** yapar.

Her şey kendi bilgisayarınızda çalışır. Ücretli bir servis veya API anahtarı gerekmez.

## Neler yapar?

- **Test eder:** OpenAPI/Swagger belgesi olan API'leri ve web sitelerini. İşlevsellik, sınır değerleri, güvenlik, yanıt süresi ve kırık bağlantı kontrolleri yapar.
- **Açıklar:** Her hatanın önem derecesini, kaynağını, nerede olduğunu ve nasıl düzeltileceğini gösterir. Hatayı yeniden üreten küçük bir kanıt scripti verir.
- **Karşılaştırır:** Aynı siteyi yeniden test ettiğinizde önceki teste göre hangi hataların yeni çıktığını, hangilerinin düzeldiğini gösterir.
- **Raporlar:** HTML, PDF, JUnit XML ve JSON rapor üretir. Kritik web hatalarının ekran görüntüsünü rapora ekler.
- **Haber verir:** Raporu e-postayla PDF olarak gönderir. İsterseniz belirli aralıklarla (örneğin 3 günde bir) özet e-postası atar.
- **Yardım eder:** Kullanım rehberi, soru sorulabilen bir asistan ve proje belgelerinde anlamına göre arama içerir (yerel Ollama ile).
- **Güvenli düzeltir:** Önerilen yamayı onaydan sonra uygular, tüm testleri yeniden çalıştırır, sorun çıkarsa yamayı geri alır.

## Kurulum

```bash
git clone https://github.com/Ozlemoon/BugHunter.git
cd BugHunter
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

## Kullanım

### Panel

```bash
python main.py web
```

Tarayıcıda **http://127.0.0.1:8000** adresini açın, bir hesap oluşturup giriş yapın. Sol menüde:

| Bölüm | Ne işe yarar |
| :--- | :--- |
| Testler | Adres girip testi başlatırsınız; sonuçlar, öneriler ve önceki testle karşılaştırma burada |
| Önceki testlerim | Eski testleriniz site site listelenir; iki testi karşılaştırabilirsiniz |
| Rapor gönder | Raporu e-postayla gönderme ve zamanlanmış özet raporlar |
| Rehber / Asistan | Kullanım kılavuzu ve sık sorulan sorular |
| Proje belgeleri | Gereksinim belgelerini yükleyip içinde arama |

Panel terminal açık kaldığı sürece çalışır.

### Denemek için hazır demo

```bash
python main.py demo
```

Paneli ve bilerek hatalı yazılmış iki örnek uygulamayı birlikte açar: bir API (http://127.0.0.1:8001) ve bir web sitesi (http://127.0.0.1:8002). Panelde örnek düğmelerinden birine basıp testi başlatmanız yeterli.

> Demo düzeltmeleri `playground/` klasöründeki dosyaları gerçekten değiştirir. Eski haline dönmek için panelden **Geri al** diyebilir veya `git checkout playground/` çalıştırabilirsiniz.

### Komut satırı ve CI

```bash
# Etkileşimli: her düzeltme için onay sorar
python main.py hunt --url http://127.0.0.1:8001/openapi.json

# CI için: soru sormaz, koda dokunmaz, rapor yazar
python main.py scan --url https://staging.ornek.com/openapi.json --junit sonuc.xml --pdf rapor.pdf --fail-on HIGH
```

`scan` komutu, `--fail-on` ile verilen önem derecesinde veya daha ciddi bir hata bulursa `1`, hedefe ulaşamazsa `2`, aksi halde `0` koduyla çıkar. Böylece pipeline'ı durdurabilir.

## Ayarlar

Ayarlar proje klasöründeki `.env` dosyasına yazılır. Örnek için [.env.example](.env.example) dosyasına bakın. En çok kullanılanlar:

| Ayar | Ne için |
| :--- | :--- |
| `BUGHUNTER_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM` | E-posta gönderimi (Gmail için uygulama şifresi gerekir) |
| `BUGHUNTER_PUBLIC_URL` | Panel bir sunucuda yayındaysa adresi; e-postadaki "Raporu görüntüle" bağlantısı bunu kullanır |
| `BUGHUNTER_ALLOW_SIGNUP` | `false` yapılırsa yeni hesap açılamaz |
| `BUGHUNTER_SCREENSHOTS` | `off` yapılırsa ekran görüntüsü alınmaz |
| `BUGHUNTER_OLLAMA_URL` | Asistan ve belge araması için Ollama adresi |

PDF rapor ve ekran görüntüleri için bilgisayarda Edge, Chrome veya Chromium kurulu olmalıdır. Asistan ve belge araması için [Ollama](https://ollama.com) ile `embeddinggemma` modeli gerekir; bunlar yoksa diğer her şey yine çalışır.

## Docker

```bash
docker compose up --build
```

Paneli başka bilgisayarlara açacaksanız `BUGHUNTER_API_TOKEN` tanımlayın, çünkü panel kaynak koda yama uygulayabilir.

## Proje yapısı

```
core/         Test üretme, çalıştırma, analiz, yama, rapor, e-posta ve veritabanı
web/          Panel (FastAPI ve arayüz)
playground/   Bilerek hatalı demo API ve web sitesi
docs/         Kullanım kılavuzu
tests/        Testler
main.py       Komut satırı girişi
```

## Geliştirme

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

## Lisans

MIT
