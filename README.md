# 🎯 BugHunter AI — İnsan Onaylı Otonom Yazılım Test ve Hata Analiz Ajanı

> **API dokümantasyonlarını analiz eden, otonom testler üreten, yazılım hatalarını kanıtlayan ve insan onayıyla güvenli kod düzeltmesi gerçekleştiren yeni nesil yapay zeka ajanı.**

---

## 🚀 Proje Vizyonu ve Amacı

BugHunter AI; modern yazılım geliştirme süreçlerinde API testlerini otomatize etmek, beklenmeyen sistem hatalarını (edge cases, boundary errors, güvenlik açıkları) yakalamak ve tespit edilen hataları **test mühendisinin kontrolünü merkeze alarak** gidermek için tasarlanmıştır.

### 🔄 Temel Döngü
```text
┌──────────┐     ┌───────────┐     ┌───────────┐     ┌─────────────┐     ┌──────────┐     ┌──────────────────┐
│ Test Et  │ ──► │  Araştır  │ ──► │  Kanıtla  │ ──► │  Onay Al    │ ──► │  Düzelt  │ ──► │ Yeniden Test Et  │
└──────────┘     └───────────┘     └───────────┘     └─────────────┘     └──────────┘     └──────────────────┘
```

1. **Test Et:** OpenAPI / Swagger dokümantasyonunu okuyarak pozitif, negatif ve sınır (boundary) test senaryoları türetir ve hedefe karşı çalıştırır.
2. **Araştır:** Başarısız olan yanıtları, durum kodlarını ve logları derinlemesine inceler; kök nedeni (root cause) saptar.
3. **Kanıtla:** Hatayı bağımsız, izole bir ortamda yeniden üreten (reproduction) script hazırlar; beklenen ve gerçekleşen davranışları kanıtlarıyla raporlar.
4. **Onay Al (Human-in-the-Loop):** Çözüm için bir kod yaması (diff) hazırlar. Kullanıcı onayı olmadan kaynak koda ASLA dokunmaz.
5. **Düzelt:** Yalnızca test/sandbox ortamında onaylanan yamayı uygular.
6. **Yeniden Test Et:** Düzeltmeyi bağımsız testlerle doğrular ve regresyon (yeni bir hatanın tetiklenip tetiklenmediği) kontrolü yapar.

---

## 🌟 Özgünlük ve Fark Yaratan Yönleri

- **Tamamen Yerel ve Ücretsiz Çalışabilme:** Yerel LLM'ler (Ollama vb.) veya ücretsiz API kotaları (Google Gemini, Groq) ve kural tabanlı hibrit motor desteği ile sıfır maliyetle çalışabilir.
- **İnsan Onayı (Human-in-the-Loop Gate):** Kod değişiklikleri doğrudan commit edilmez veya ana koda yazılmaz. Test mühendisi görsel diff üzerinden "Onayla" demeden düzeltme uygulanmaz.
- **Tekrarlanabilir Kanıt (Reproducibility Evidence):** Hata sadece "test fail oldu" denilerek geçilmez; hatayı tek başına tetikleyen minimal çalıştırılabilir kanıt kodu sunulur.
- **Regresyon Güvencesi:** Düzeltme uygulandıktan sonra sistem sadece o testi değil, tüm test havuzunu yeniden koşturarak yan etki oluşup oluşmadığını denetler.

---

## 🛠️ Sistem Mimarisi

- **API Documentation Parser:** OpenAPI (v3.x), Swagger (v2.0) ve REST JSON spesifikasyonlarını okur.
- **Autonomous Test Generator:** Sınır değerleri (Boundary analysis), eksik alanlar, tip uyumsuzlukları ve yetkilendirme testlerini otomatik üretir.
- **Bug Reproduction & Evidence Engine:** Başarısız testleri izole eder ve bağımsız çalıştırma kanıtı üretir.
- **Human Approval & Diff Engine:** Renkli unified diff formatında düzeltme önerisi sunar ve kullanıcı onayını bekler.
- **Regression Verification Engine:** Yama sonrası eski ve yeni test sonuçlarını kıyaslar.
- **Interactive Dashboard:** Testlerin, hata kanıtlarının ve onay mekanizmalarının yönetildiği web arayüzü.

---

## 📂 Proje Yapısı

```
BugHunter/
├── core/                   # Temel ajan mantığı
│   ├── parser.py           # OpenAPI / Swagger çözümleyici
│   ├── generator.py        # Otonom test senaryosu üretici
│   ├── runner.py           # Test koşturucu ve yanıt toplayıcı
│   ├── analyzer.py         # Hata analizi ve kanıt toplama motoru
│   ├── patcher.py          # Kod düzeltme ve yama (diff) motoru
│   └── verifier.py         # Regresyon ve doğrulama motoru
├── playground/             # Canlı demo için bilerek hatalı mini API
│   ├── app.py              # FastAPI örnek servis (demo hataları içeren)
│   └── openapi.json        # Örnek API spesifikasyonu
├── web/                    # İnteraktif Web Kontrol Paneli
│   ├── static/             # CSS / JS / Dashboard varlıkları
│   └── templates/          # HTML şablonları
├── tests/                  # BugHunter'ın kendi birim testleri
├── cli.py                  # Komut satırı arayüzü
├── main.py                 # Uygulama başlatıcı
├── requirements.txt        # Bağımlılıklar (Tamamı ücretsiz ve açık kaynak)
└── README.md
```

---

## 📄 Lisans
MIT License.
