# 🎯 BugHunter AI — İnsan Onaylı Evrensel Yazılım Test ve Hata Analiz Ajanı
### *Universal Autonomous Testing & Bug Analysis Agent with Human-in-the-Loop Verification*

> **API'ler, Web Siteleri, Web Uygulamaları ve Kod Depolarını kullanıcı tarafından belirlenen kriterlere göre otonom olarak test eden, hataları tespit edip kanıtlayan ve insan onayıyla güvenli kod düzeltmesi gerçekleştiren yeni nesil yapay zeka ajanı.**

---

## 🚀 Proje Vizyonu ve Genişletilmiş Kapsam

BugHunter AI; yalnızca tek bir repoya veya sadece API'lere sıkışmış geleneksel araçların ötesine geçerek, **herhangi bir yazılım projesini (API, Web Sitesi, Web Uygulaması, Kod Deposu)** kullanıcının belirlediği özel test kriterlerine göre otonom analiz eden evrensel bir test ve hata ayıklama ajanıdır.

### 🎯 Desteklenen Hedef Sistemler (Multi-Target Architecture)
1. **REST API & Mikroservisler:** OpenAPI / Swagger spesifikasyonları veya canlı endpoint'ler üzerinden şema doğrulaması, veri manipülasyonu ve yetki testleri.
2. **Web Uygulamaları ve Web Siteleri:** Herhangi bir web sitesinin veya web uygulamasının URL'si üzerinden; form doğrulamaları, kırık linkler (broken links), HTTP başlıkları/güvenlik konfigürasyonları, DOM bütünlüğü ve yanıt süreleri testleri.
3. **Kod Depoları & Modüller (Code Repositories):** Kaynak kod düzeyinde fonksiyonlar, sınıflar ve iş mantığı için kriter bazlı birim (unit) ve entegrasyon testlerinin otomatik üretimi ve koşturulması.

---

## ⚙️ Kriter Bazlı Test Motoru (Criteria-Driven Testing Engine)

Kullanıcı, BugHunter AI'ya hedefi ve çalıştırmak istediği test kriterlerini açıkça belirtebilir:

- 🟢 **Fonksiyonel Test Kriterleri (Functional):** Temel iş akışları, beklenen veri dönüşleri, kullanıcı yolculukları.
- 🟡 **Sınır ve Kenar Durum Kriterleri (Boundary & Edge Cases):** Negatif sayılar, aşırı uzun metinler, null/boş girdiler, tip uyuşmazlıkları.
- 🔴 **Güvenlik ve Zafiyet Kriterleri (Security & Robustness):** Yetkisiz erişim denemeleri (BOLA/IDOR), SQL Injection / XSS girdi kontrolleri, güvensiz HTTP başlıkları, hassas veri sızıntısı.
- 🔵 **Performans ve Yanıt Süresi Kriterleri (Performance & SLA):** Maksimum yanıt süresi (örn. <500ms), sunucu yük direnci ve durum kodu sözleşmeleri.
- 🟣 **Web & Form Bütünlük Kriterleri (Web & Form Validation):** Bozuk görsel ve link tespiti, formların boş veya hatalı gönderim davranışı, hata mesajlarının doğruluğu.

---

## 🔄 Temel Döngü (The Core Loop)

```text
┌─────────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌───────────────┐     ┌──────────────────┐
│ Kriter & Hedef  │ ──► │   Test Et    │ ──► │  Kanıt Topla │ ──► │  Onay İste   │ ──► │ Güvenli Düzelt│ ──► │ Yeniden Test Et  │
│  (API/Web/Repo) │     │ (Autonomous) │     │ (Reproduce)  │     │  (HITL Gate) │     │ (Safe Patch)  │     │  (No Regression) │
└─────────────────┘     └──────────────┘     └──────────────┘     └──────────────┘     └───────────────┘     └──────────────────┘
```

1. **Hedef ve Kriter Girişi:** Kullanıcı test edilecek hedefi (API dokümanı, Canlı URL veya Kod Deposu) ve test kriterlerini tanımlar.
2. **Otonom Test Üretimi ve Koşumu:** Ajan, kriterlere uygun test senaryolarını dinamik üretir ve hedef üzerinde çalıştırır.
3. **Araştırma ve Kanıt Toplama:** Başarısız testleri izole eder, hatayı bağımsız olarak yeniden üreten minimal bir kanıt scripti (reproduction script) ve "Beklenen vs Gerçekleşen" kanıt raporu oluşturur.
4. **İnsan Onayı (Human-in-the-Loop Gate):** Çözüm için oluşturulan renkli kod yamasını (diff) test mühendisine sunar. Onay gelmeden kaynak koda ASLA dokunulmaz.
5. **Güvenli Düzeltme:** Onaylanan yama yalnızca kontrollü test/sandbox ortamında uygulanır.
6. **Regresyon Doğrulaması:** Ajan, yapılan düzeltmenin diğer özellikleri bozup bozmadığını anlamak için tüm test paketini yeniden çalıştırır ve "Önce / Sonra" karşılaştırma raporu sunar.

---

## 🛠️ Sistem Mimarisi ve Modüller

```
BugHunter/
├── core/
│   ├── targets/            # Hedef adaptörleri
│   │   ├── api_target.py       # REST / OpenAPI hedef yöneticisi
│   │   ├── web_target.py       # Web sitesi / Web uygulaması hedef yöneticisi
│   │   └── repo_target.py      # Kod deposu hedef yöneticisi
│   ├── criteria/           # Kriter motoru
│   │   ├── base.py             # Kriter temel arayüzü
│   │   ├── functional.py       # Fonksiyonel kural motoru
│   │   ├── boundary.py         # Sınır & kenar durum motoru
│   │   └── security.py         # Güvenlik & doğrulama motoru
│   ├── generator.py        # Otonom test senaryosu üretici
│   ├── runner.py           # Dinamik test koşturucu
│   ├── analyzer.py         # Kök neden analizi & kanıt toplama motoru
│   ├── patcher.py          # Unified Diff ve yama motoru
│   └── verifier.py         # Regresyon ve doğrulama motoru
├── playground/             # Demo hedefleri (API + Web Uygulaması)
│   ├── sample_api.py       # Bilerek hatalar barındıran örnek REST API
│   ├── sample_web.py       # Bilerek form/link hataları barındıran örnek web sitesi
│   └── templates/          # Web sitesi şablonları
├── web/                    # İnteraktif Web Kontrol Paneli
│   ├── app.py              # BugHunter Web Dashboard backend
│   ├── static/             # CSS / JS / Diff görüntüleyici
│   └── templates/          # Gösterge paneli ve onay ekranı
├── tests/                  # BugHunter'ın kendi iç testleri
├── cli.py                  # Terminal arayüzü
├── main.py                 # Ana başlatıcı
├── requirements.txt        # %100 Ücretsiz & Açık kaynak bağımlılıklar
└── README.md
```

---

## 🌟 Özgünlük ve Farkı

- **Çoklu Hedef (Multi-Target):** Sadece API değil; web siteleri, web uygulamaları ve doğrudan kaynak kod depolarını aynı çatı altında test edebilme.
- **Kriter Odaklı (Criteria-Driven):** Kullanıcının tanımladığı spesifik test standartlarını (SLA süreleri, OWASP kontrolleri, form kuralları vb.) hedef alarak test türetme.
- **Tekrarlanabilir Kanıt (Reproducibility Evidence):** Hatanın bağımsız olarak tek başına tetiklenebildiğini kanıtlayan kod ve izole senaryo.
- **Kesin İnsan Onayı (Human-in-the-Loop):** Kod değişiklikleri asla insan denetimi ve onayı olmadan canlıya veya depoya uygulanmaz.
- **Regresyon Denetimi:** Düzeltmenin yan etki yaratıp yaratmadığı bağımsız olarak teyit edilir.
- **%100 Ücretsiz ve Yerel Çalışabilir:** Ücretli hiçbir API'ye bağımlı olmadan yerel ve açık kaynak araçlarla tam performans çalışır.

---

## 📄 Lisans
MIT License.
