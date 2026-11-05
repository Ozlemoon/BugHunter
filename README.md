# 🎯 BugHunter AI — İnsan Onaylı Evrensel Yazılım Test ve Hata Analiz Ajanı
### *Universal Autonomous Testing & Bug Analysis Agent with Human-in-the-Loop Verification*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://python.org)
[![Tests: 11 Passed](https://img.shields.io/badge/Tests-11%20Passed-success.svg)](https://github.com/Ozlemoon/BugHunter)

> **API'ler, Web Siteleri, Web Uygulamaları ve Kod Depolarını kullanıcı tarafından belirlenen kriterlere göre otonom olarak test eden, hataları tespit edip kanıtlayan ve insan onayıyla güvenli kod düzeltmesi gerçekleştiren yeni nesil yapay zeka ajanı.**

---

## 🚀 Proje Vizyonu ve Temel Amaç

**BugHunter AI**; modern yazılım mühendisliğinde test süreçlerini otonomlaştırırken kontrolü test mühendisine bırakan **Human-in-the-Loop** merkezli bir test ve hata ayıklama ajanıdır. 

Yalnızca "test koşup hata bildirmekle" kalmaz; hatayı bağımsız olarak yeniden üreten minimal kanıt kodunu (reproduction script) çıkarır, çözüm için **Unified Diff** hazırlar, test mühendisinden açık onay ister ve düzeltmeyi test ortamında uygulayarak **regresyon (yan etki)** denetimi gerçekleştirir.

```text
┌─────────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌───────────────┐     ┌──────────────────┐
│ Kriter & Hedef  │ ──► │   Test Et    │ ──► │  Kanıt Topla │ ──► │  Onay İste   │ ──► │ Güvenli Düzelt│ ──► │ Yeniden Test Et  │
│ (API / Web / UI)│     │ (Autonomous) │     │ (Reproduce)  │     │ (HITL Gate)  │     │ (Safe Patch)  │     │ (No Regression)  │
└─────────────────┘     └──────────────┘     └──────────────┘     └──────────────┘     └───────────────┘     └──────────────────┘
```

---

## 🌟 Öne Çıkan Özgün Yetenekler

1. **Evrensel Çoklu Hedef (Multi-Target Architecture):**
   - **REST API'ler & Mikroservisler:** OpenAPI 3.x / Swagger 2.0 dokümanları üzerinden otomatik şema çözümleme ve endpoint testleri.
   - **Web Siteleri & Web Uygulamaları:** Canlı URL üzerinden form doğrulamaları, kırık link tespiti (broken links), eksik varlıklar (assets) ve DOM bütünlüğü.
   - **Kod Depoları (Repositories):** Kaynak kod düzeyinde kurallar ve test üretimi.

2. **Kriter Bazlı Test Motoru (Criteria-Driven Engine):**
   - 🟢 **FUNCTIONAL (Fonksiyonel):** Happy Path ve beklenen veri sözleşmeleri.
   - 🟡 **BOUNDARY (Sınır Durumları):** Negatif değerler, aşırı uzun veriler, null / boş parametreler.
   - 🔴 **SECURITY (Güvenlik):** Yetkisiz silme/güncelleme denemeleri (Broken Object Level Authorization - BOLA), eksik auth kontrolleri.
   - 🔵 **PERFORMANCE (SLA):** Yanıt sürelerinin belirlenen milisaniye eşiğinin (örn. `<500ms`) altında kalma garantisi.
   - 🟣 **WEB_VALIDATION (Web & Form Bütünlüğü):** Bozuk bağlantılar, kırık görseller, form alanlarının hatalı girdi karşısındaki direnci.

3. **Çalıştırılabilir Bağımsız Kanıt (Reproduction Script):**
   - Her hata için tek başına çalıştırılabilen (`python reproduce_bug.py`) bağımsız bir kanıt scripti oluşturur.

4. **Kesin İnsan Onayı Güvenlik Kilidi (Human-in-the-Loop Gate):**
   - Ajan insan onayı almadan kaynak koda ASLA dokunmaz.
   - Onaylanmamış bir yama uygulanmak istendiğinde anında `PermissionError` fırlatır.
   - Tüm işlemlerden önce otomatik `.bughunter.bak` yedeği alır; istendiğinde anında `rollback` (geri alma) imkanı sunar.

5. **Regresyon ve Yan Etki Denetimi:**
   - Düzeltme sonrası sadece hatanın çözüldüğünü teyit etmekle kalmaz; önceden geçen tüm testleri yeniden çalıştırarak yeni bir hatanın tetiklenip tetiklenmediğini (`CLEAN_FIX` vs `REGRESSION_DETECTED`) garanti eder.

6. **%100 Ücretsiz ve Açık Kaynak:**
   - Ücretli herhangi bir API anahtarına veya aboneliğe ihtiyaç duymaz. Tamamen yerel çalışabilen açık kaynak kütüphaneler üzerine inşa edilmiştir.

---

## 📂 Proje Dizin Yapısı

```
BugHunter/
├── core/                           # Temel Ajan Motoru
│   ├── targets/                    # Hedef Adaptörleri
│   │   ├── api_target.py           # OpenAPI / Swagger çözümleyici
│   │   └── web_target.py           # Web sitesi / DOM / Form çözümleyici
│   ├── models.py                   # Pydantic v2 veri modelleri & kriterler
│   ├── generator.py                # Kriter bazlı otonom test üretici
│   ├── runner.py                   # Dinamik test koşturucu & kural denetleyici
│   ├── analyzer.py                 # Hata analiz & bağımsız kanıt üretici
│   ├── patcher.py                  # Unified Diff & İnsan Onay Kilidi (Patcher)
│   └── verifier.py                 # Regresyon doğrulama & karşılaştırma motoru
├── playground/                     # Canlı Demo Hedefleri (Bilerek hatalı)
│   ├── sample_api.py               # 4 gerçekçi hata içeren REST API
│   ├── sample_web.py               # Form açığı & kırık link içeren Web uygulaması
│   ├── runner.py                   # Hedefleri ayağa kaldıran servis
│   └── templates/                  # Demo web sayfaları
├── web/                            # İnteraktif Web Kontrol Paneli
│   ├── app.py                      # Dashboard backend (FastAPI)
│   └── templates/                  # Tailwind CSS modern gösterge paneli
│       └── dashboard.html
├── tests/                          # Kapsamlı Otomasyon Testleri (11 Test)
│   ├── test_parser_and_generator.py
│   ├── test_runner_and_analyzer.py
│   ├── test_patcher.py
│   ├── test_verifier.py
│   └── test_web_dashboard.py
├── cli.py                          # Renkli Terminal Arayüzü (Rich CLI)
├── main.py                         # Master Launcher (Demo, Web, CLI)
├── requirements.txt                # Bağımlılıklar
└── README.md
```

---

## ⚡ Hızlı Başlangıç ve Kurulum

### 1. Depoyu Klonlayın ve Sanal Ortamı Hazırlayın
```bash
git clone https://github.com/Ozlemoon/BugHunter.git
cd BugHunter

# Sanal ortamı oluşturun ve aktifleştirin
python -m venv venv
# Windows:
.\venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# Bağımlılıkları yükleyin
pip install -r requirements.txt
```

---

## 🎮 Çalıştırma Modları

### 🌟 1. Tek Tıkla Canlı Demo (Önerilen)
Hem bilerek hatalı demo hedeflerini hem de interaktif web panelini aynı anda başlatır:
```bash
python main.py demo
```
- **Web Kontrol Paneli:** `http://127.0.0.1:8000`
- **Hedef Demo API:** `http://127.0.0.1:8001/openapi.json`
- **Hedef Demo Web Sitesi:** `http://127.0.0.1:8002`

Tarayıcınızda `http://127.0.0.1:8000` adresine gidin, kriterleri seçin ve **"🚀 Otonom Test Avını Başlat"** butonuna tıklayın!

---

### 💻 2. Terminal Üzerinden İnteraktif CLI
Terminali tercih eden test mühendisleri için renkli `rich` terminal arayüzü:
```bash
# Demo hedeflerini arka planda başlatın (isteğe bağlı):
python main.py playground

# Farklı bir terminalde CLI'ı başlatın:
python main.py cli --url http://127.0.0.1:8001/openapi.json --type API
```
Terminal üzerinden testlerin koşumunu izleyebilir, üretilen Unified Diff yamasını inceleyebilir ve terminal sorusuna `y` (evet) diyerek onayı verebilirsiniz:
```text
🛡️ [HUMAN-IN-THE-LOOP ONAY KİLİDİ]
Bu kod düzeltmesini sample_api.py dosyasına uygulamayı onaylıyor musunuz? [Y/n]: y
✔ Düzeltme uygulandı! (Yedek: sample_api.py.bughunter.bak)
[6/6] 🔄 Regresyon doğrulaması koşturuluyor...
✅ Target defect BUG-API-BOUND-NEG-ORDER is completely resolved with ZERO regressions.
```

---

### 🧪 3. Birim ve Entegrasyon Testlerini Çalıştırma
Tüm motorların doğruluğunu test etmek için:
```bash
pytest tests/
```
Çıktı: `11 passed in ~1.9s` 🚀

---

## 🎯 Test Edilebilen Gerçekçi Hatalar (Playground)

BugHunter AI'ın gücünü test edebilmeniz için `playground/` altına bilerek şu gerçek dünya hataları yerleştirilmiştir:

| Hedef | Endpoint / Sayfa | Hata Tipi | Ajanın Davranışı |
| :--- | :--- | :--- | :--- |
| **API** | `POST /orders` | **Boundary / Logic Bug:** Negatif adet (`quantity: -5`) kabul edilip stok artırılıyor. | Ajan 422 yerine 201 döndüğünü yakalar, `if order.quantity <= 0` kontrolü ekleyen diff sunar. |
| **API** | `GET /users/{id}/profile` | **Server Crash (500):** Profili olmayan kullanıcıda doğrudan `KeyError` ile çökme. | 500 hatasını yakalar, güvenli `not in USER_PROFILES_DB` 404 kontrolü ekleyen diff önerir. |
| **API** | `DELETE /products/{id}` | **Broken Auth:** Yetkisiz kullanıcılar ürün silebiliyor. | DELETE isteğinin auth'suz 200 döndüğünü yakalar, admin token zorunluluğu getiren diff hazırlar. |
| **API** | `GET /products/heavy-search` | **SLA / Performance:** Yanıt 1.3 saniye sürerek SLA standardını aşıyor. | Gecikmeyi ölçer ve SLA aşım kanıtı sunar. |
| **WEB** | `POST /submit-contact` | **Form Validation Crash (500):** E-posta '@' içermediğinde split hatasıyla çökme. | Formu test eder, 500 aldığını kanıtlar ve geçerli e-posta doğrulaması önerir. |
| **WEB** | `GET /legacy-api-docs` | **Broken Link:** Menüdeki link 404 sayfasına gidiyor. | Web tarayıcı kırık bağlantıyı izole eder ve raporlar. |

---

## 📄 Lisans
Bu proje **MIT Lisansı** altında lisanslanmıştır. Tamamen ücretsiz, açık kaynak ve genişletilebilir olarak geliştirilmiştir.
