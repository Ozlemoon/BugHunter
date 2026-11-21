"""
Remediation advice for findings on targets whose source code we do not have.

Every failed test gets a short Turkish headline, a priority that matches its
risk, concrete steps and (where useful) a small example. Low-risk results and
results that are probably caused by the test itself (e.g. a made-up id that
does not exist on a real API) say so honestly instead of sounding alarming.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from core.models import AssertionType, BugEvidence, Severity, TestCase, TestCriterion, TestResult

PRIORITY = {
    Severity.CRITICAL: "Acil: yayına almadan önce düzeltin",
    Severity.HIGH: "Yüksek: bu sürümde düzeltin",
    Severity.MEDIUM: "Orta: planlı olarak düzeltin",
    Severity.LOW: "Düşük: isteğe bağlı iyileştirme",
}


RECORD_CRASH_EXAMPLE = (
    "record = DB.get(item_id)\n"
    "if record is None:\n"
    '    raise HTTPException(status_code=404, detail="Kayıt bulunamadı")'
)
FORM_CRASH_EXAMPLE = (
    'if not email or "@" not in email:\n'
    '    raise HTTPException(status_code=422, detail="Geçerli bir e-posta adresi girin")'
)


class ErrorSource(str, Enum):
    """Where a defect most likely comes from, so the reader knows which team/file to look at."""
    SERVER_CRASH = "SERVER_CRASH"
    INPUT_VALIDATION = "INPUT_VALIDATION"
    AUTH = "AUTH"
    OUTPUT_ENCODING = "OUTPUT_ENCODING"
    SERVER_CONFIG = "SERVER_CONFIG"
    API_CONTRACT = "API_CONTRACT"
    PERFORMANCE = "PERFORMANCE"
    CONTENT = "CONTENT"
    NETWORK = "NETWORK"
    TEST_SETUP = "TEST_SETUP"
    UNKNOWN = "UNKNOWN"


# source -> (label, side of the system, general recommendation)
SOURCE_INFO: Dict[ErrorSource, Tuple[str, str, str]] = {
    ErrorSource.SERVER_CRASH: (
        "İşlenmemiş sunucu hatası", "Sunucu tarafı · uygulama kodu",
        "Sunucu beklenmeyen girdide çöküyor. Önce sunucu kayıtlarındaki (log) yığın izlerini inceleyin; "
        "girdiyi doğrulayıp 4xx döndürün ve genel bir hata yakalayıcıyla iç ayrıntıları yanıtta gizleyin."),
    ErrorSource.INPUT_VALIDATION: (
        "Eksik girdi doğrulaması", "Sunucu tarafı · istek doğrulama",
        "Gelen veriler sunucuda doğrulanmıyor. Gövde ve parametre doğrulamasını şema tabanlı tek bir "
        "katmanda toplayın (ör. Pydantic, Joi, Bean Validation); tarayıcıdaki doğrulamaya güvenmeyin."),
    ErrorSource.AUTH: (
        "Kimlik doğrulama / yetkilendirme", "Sunucu tarafı · erişim kontrolü",
        "Erişim kontrolü eksik veya zayıf. Kimlik ve yetki kontrolünü bütün endpoint'lere uygulanan tek bir "
        "ara katmana taşıyın ve varsayılan davranışı 'reddet' yapın."),
    ErrorSource.OUTPUT_ENCODING: (
        "Çıktı kaçışlama (XSS)", "Sunucu tarafı · HTML şablonu",
        "Kullanıcı girdisi sayfaya kaçışlanmadan yazılıyor. Otomatik kaçışlama yapan bir şablon motoru "
        "kullanın ve ek savunma olarak Content-Security-Policy ekleyin."),
    ErrorSource.SERVER_CONFIG: (
        "Sunucu yapılandırması", "Sunucu / ters vekil (Nginx, CDN)",
        "Güvenlik başlıkları eksik. Bunları tek yerden, ters vekil sunucuda veya uygulamanın ara katmanında "
        "ekleyin; böylece yeni sayfalar da otomatik korunur."),
    ErrorSource.API_CONTRACT: (
        "API sözleşmesi uyuşmazlığı", "Sunucu kodu ↔ OpenAPI belgesi",
        "Gerçek yanıtlar belgeyle uyuşmuyor. Belgeyi koddan otomatik üretin veya CI hattında sözleşme testi "
        "çalıştırın ki ikisi ayrışmasın."),
    ErrorSource.PERFORMANCE: (
        "Yavaş yanıt", "Sunucu veya ağ",
        "Bazı istekler eşiği aşıyor. Ölçümü tekrarlayın; kalıcıysa yavaş veritabanı sorgularını, eksik "
        "indeksleri ve önbelleğe alınabilecek okumaları inceleyin."),
    ErrorSource.CONTENT: (
        "Kırık bağlantı / eksik dosya", "Site içeriği · dağıtım",
        "Sayfalar artık var olmayan adreslere bağlanıyor. Her dağıtımdan sonra otomatik bağlantı kontrolü "
        "çalıştırın; taşınan sayfalara 301 yönlendirmesi ekleyin."),
    ErrorSource.NETWORK: (
        "Erişim / ağ sorunu", "Ortam · ağ",
        "Bazı isteklere yanıt alınamadı. Adresin doğru olduğunu, sunucunun ayakta olduğunu ve güvenlik "
        "duvarı / hız sınırının BugHunter'ı engellemediğini kontrol edin."),
    ErrorSource.TEST_SETUP: (
        "Test verisi veya kimlik bilgisi", "Test yapılandırması (uygulama hatası değil)",
        "Bu sonuçlar büyük olasılıkla testin kullandığı örnek değerlerden veya verilmeyen kimlik bilgisinden "
        "kaynaklanıyor. OpenAPI belgesine gerçek `example` değerleri ekleyin; gerekiyorsa Authorization "
        "başlığını girip testi tekrarlayın."),
    ErrorSource.UNKNOWN: (
        "Beklenmeyen davranış", "Belirsiz",
        "Kanıt scriptiyle hatayı tekrar üretip yanıtı inceleyin."),
}


class Remediation(BaseModel):
    headline: str                       # Turkish one-line description of the problem
    severity: Severity                  # final risk (lowered to LOW when not noteworthy)
    priority: str
    action: str                         # what to do, in one or two sentences
    steps: List[str] = Field(default_factory=list)
    example: Optional[str] = None
    example_language: Optional[str] = None
    noteworthy: bool = True             # False: probably not a real problem / safe to ignore
    note: Optional[str] = None          # caveat shown next to the advice
    source: ErrorSource = ErrorSource.UNKNOWN
    source_label: str = ""              # e.g. "Eksik girdi doğrulaması"
    source_side: str = ""               # e.g. "Sunucu tarafı · istek doğrulama"
    location: str = ""                  # exact place: endpoint handler, fields, page


class Recommendation(BaseModel):
    """One piece of run-level advice: all findings that share an error source."""
    source: ErrorSource
    label: str
    side: str
    severity: Severity
    count: int
    noteworthy: bool
    advice: str
    locations: List[str] = Field(default_factory=list)


def _where(test: TestCase) -> str:
    return f"{test.method} {test.endpoint or test.url}"


def _path(test: TestCase) -> str:
    return test.endpoint or urlparse(test.url).path or "/"


def _fields(test: TestCase) -> List[str]:
    names = list(test.query_params)
    if isinstance(test.payload, dict):
        names += list(test.payload)
    return list(dict.fromkeys(str(n) for n in names))


def _negative_fields(test: TestCase) -> List[str]:
    values = dict(test.query_params)
    if isinstance(test.payload, dict):
        values.update(test.payload)
    return [str(k) for k, v in values.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v < 0]


def _location(test: TestCase, fields: Optional[List[str]] = None, label: str = "alanlar") -> str:
    """A precise, human-readable pointer to the code that has to change."""
    fields = _fields(test) if fields is None else fields
    is_form = test.is_form_data or "forms" in test.tags
    base = (f"Form gönderimi: {test.method} {_path(test)}" if is_form
            else f"{test.method} {_path(test)} işleyicisi")
    return f"{base} · {label}: {', '.join(fields)}" if fields else base


def recommend(remediations: Iterable[Remediation]) -> List[Recommendation]:
    """Groups findings by error source into run-level advice, most urgent first."""
    groups: Dict[ErrorSource, List[Remediation]] = {}
    for r in remediations:
        groups.setdefault(r.source, []).append(r)
    out = []
    for source, items in groups.items():
        label, side, advice = SOURCE_INFO[source]
        locations = list(dict.fromkeys(r.location for r in items if r.location))
        out.append(Recommendation(
            source=source, label=label, side=side, advice=advice, count=len(items),
            severity=max((r.severity for r in items), key=lambda s: s.rank),
            noteworthy=any(r.noteworthy for r in items), locations=locations[:6],
        ))
    out.sort(key=lambda r: (not r.noteworthy, -r.severity.rank, -r.count))
    return out


def _failed(result: TestResult, kind: AssertionType, test: TestCase) -> List[str]:
    """Failure messages produced by assertions of one type (same order as the test's assertions)."""
    messages = []
    for assertion in test.assertions:
        if assertion.assertion_type != kind:
            continue
        for message in result.failed_assertions:
            if kind == AssertionType.HEADER_EXISTS and f"'{assertion.expected_value}'" in message:
                messages.append(str(assertion.expected_value))
            elif kind != AssertionType.HEADER_EXISTS and message not in messages and _matches(kind, message):
                messages.append(message)
    return messages


def _matches(kind: AssertionType, message: str) -> bool:
    return {
        AssertionType.BODY_MATCHES_SCHEMA: "schema" in message or "JSON" in message,
        AssertionType.RESPONSE_TIME_UNDER_MS: "Response time" in message,
    }.get(kind, False)


def advise(test: TestCase, result: TestResult, evidence: BugEvidence) -> Remediation:
    severity = evidence.severity
    status = result.status_code
    tags = set(test.tags)
    where = _where(test)

    def build(headline: str, action: str, steps: List[str], example: Optional[str] = None,
              language: Optional[str] = None, noteworthy: bool = True, note: Optional[str] = None, *,
              source: ErrorSource, location: Optional[str] = None) -> Remediation:
        final = severity if noteworthy else Severity.LOW
        label, side, _ = SOURCE_INFO[source]
        return Remediation(headline=headline, severity=final, priority=PRIORITY[final], action=action,
                           steps=steps, example=example, example_language=language,
                           noteworthy=noteworthy, note=note, source=source, source_label=label,
                           source_side=side, location=location if location is not None else _location(test))

    # --- The request never got an answer ---------------------------------
    if result.error_message:
        return build(
            f"Hedefe ulaşılamadı: {where}",
            "İstek yanıt almadı; bu bir uygulama hatası olmayabilir, önce erişilebilirliği doğrulayın.",
            ["Adresin doğru ve sunucunun çalışır durumda olduğunu kontrol edin.",
             "Yavaş bir hedefse BUGHUNTER_REQUEST_TIMEOUT değerini artırıp testi tekrarlayın.",
             "Güvenlik duvarı veya hız sınırlaması (rate limit) istekleri engelliyor olabilir."],
            noteworthy=False,
            note="Bağlantı sorunları çoğunlukla ortamdan kaynaklanır; tekrar ederse sunucu kayıtlarını inceleyin.",
            source=ErrorSource.NETWORK,
            location=f"{urlparse(test.url).netloc or test.url} · {result.error_message}",
        )

    # --- Server crashed ---------------------------------------------------
    if status is not None and 500 <= status <= 599:
        return build(
            f"Sunucu çöküyor (HTTP {status}): {where}",
            "Sunucu beklenmeyen girdide işlenmemiş bir hata fırlatıyor. Girdiyi doğrulayıp istemciye 4xx "
            "dönmeli, hatayı kayda almalı ve yığın izini (stack trace) asla yanıtta göstermemelidir.",
            ["Sunucu kayıtlarında bu isteğe ait hatayı (stack trace) bulun.",
             "Hatanın kaynağı olan girdiyi doğrulayın: eksik kayıt → 404, geçersiz veri → 400/422.",
             "Sözlük/dizi erişimlerini varlık kontrolüyle yapın (ör. `.get()` veya `in`).",
             "Kanıt scriptini (reproduce_bug.py) çalıştırarak düzeltmeyi doğrulayın; 0 ile bitmeli."],
            example=FORM_CRASH_EXAMPLE if "forms" in tags else RECORD_CRASH_EXAMPLE,
            language="python",
            source=ErrorSource.SERVER_CRASH,
            location=_location(test, label="çökmeye yol açan girdiler"),
        )

    # --- Security --------------------------------------------------------
    if "xss" in tags:
        return build(
            f"Yansıyan XSS açığı: {where}",
            "Kullanıcı girdisi kaçışlanmadan HTML'e yazılıyor; saldırgan hazırladığı bir bağlantıyla "
            "kurbanın tarayıcısında kod çalıştırabilir.",
            ["Girdiyi HTML'e yazarken her zaman kaçışlayın (şablon motorunun otomatik kaçışını kullanın).",
             "Girdiyi elle birleştirerek HTML üretmeyin (f-string, string concat).",
             "Ek savunma olarak Content-Security-Policy başlığı ekleyin."],
            example='from html import escape\nheading = f"<h1>Sonuçlar: {escape(query)}</h1>"',
            language="python",
            source=ErrorSource.OUTPUT_ENCODING,
            location=f"{_path(test)} sayfasının HTML çıktısı · yansıyan parametre: {', '.join(_fields(test)) or '?'}",
        )
    if "security-headers" in tags:
        missing = _failed(result, AssertionType.HEADER_EXISTS, test)
        names = ", ".join(missing) or "güvenlik başlıkları"
        return build(
            f"Eksik güvenlik başlıkları ({names})",
            "Tarayıcıya ek koruma sağlayan HTTP başlıkları gönderilmiyor. Tek başına bir açık değildir; "
            "başka bir açığın etkisini azaltan bir sağlamlaştırmadır.",
            [f"Eksik başlıklar: {names}.",
             "Başlıkları uygulama yerine ters vekil sunucuda (Nginx, CDN) tek yerden ekleyebilirsiniz.",
             "Content-Security-Policy'yi önce 'Report-Only' modunda deneyin; harici script/stilleri bozabilir."],
            example=('add_header X-Content-Type-Options "nosniff" always;\n'
                     'add_header X-Frame-Options "DENY" always;\n'
                     'add_header Content-Security-Policy "default-src \'self\'" always;'),
            language="nginx",
            note="Düşük risk: kullanıcı verisi işlemeyen, giriş formu olmayan sitelerde ertelenebilir.",
            source=ErrorSource.SERVER_CONFIG,
            location=f"{test.url} yanıt başlıkları · eksik: {names}",
        )
    if "invalid-token" in tags:
        return build(
            f"Geçersiz token kabul ediliyor: {where}",
            "Endpoint sahte veya süresi dolmuş kimlik bilgisini reddetmiyor; token yalnızca var olup "
            "olmadığına bakılarak doğrulanıyor olabilir.",
            ["Token'ın imzasını, süresini ve kapsamını (scope/rol) sunucu tarafında doğrulayın.",
             "Doğrulama başarısızsa 401, yetki yetersizse 403 döndürün.",
             "Kontrolü tek bir ara katmanda (middleware/dependency) toplayın ki hiçbir endpoint atlanmasın."],
            source=ErrorSource.AUTH,
            location=_location(test, fields=[]) + " · token doğrulama adımı",
        )
    if "broken-auth" in tags:
        return build(
            f"Kimlik doğrulaması olmadan işlem yapılabiliyor: {where}",
            "Kimlik bilgisi gönderilmeden yapılan istek başarıyla işlendi. Herkes bu işlemi yapabilir.",
            ["Bu endpoint için kimlik doğrulamasını zorunlu kılın; kimlik yoksa 401 döndürün.",
             "Kullanıcının bu kaynak üzerinde yetkisi olup olmadığını kontrol edin; yoksa 403 döndürün.",
             "OpenAPI belgesine `security` gereksinimini ekleyin ki istemciler ve testler bunu bilsin."],
            example=('def require_admin(authorization: str = Header(None)):\n'
                     '    if not is_valid_admin_token(authorization):\n'
                     '        raise HTTPException(status_code=401, detail="Yetkisiz")\n\n'
                     '@app.delete("/products/{product_id}", dependencies=[Depends(require_admin)])'),
            language="python",
            source=ErrorSource.AUTH,
            location=_location(test, fields=[]) + " · yetki kontrolü yok",
        )

    # --- Input validation -------------------------------------------------
    if "negative-values" in tags:
        return build(
            f"Negatif sayılar kabul ediliyor: {where}",
            "Miktar/fiyat gibi alanlara negatif değer gönderildiğinde istek reddedilmedi. Bu, stok veya "
            "tutar hesaplarını bozabilir.",
            ["Sayısal alanlara sunucu tarafında alt sınır koyun (ör. adet > 0, fiyat ≥ 0).",
             "Geçersiz değerde 422 veya 400 döndürün.",
             "Sınırları OpenAPI şemasına da yazın (`minimum`, `exclusiveMinimum`)."],
            example="class OrderCreate(BaseModel):\n    quantity: int = Field(gt=0)\n    price: float = Field(ge=0)",
            language="python",
            source=ErrorSource.INPUT_VALIDATION,
            location=_location(test, fields=_negative_fields(test) or None, label="negatif kabul edilen alanlar"),
        )
    if "missing-required" in tags:
        return build(
            f"Zorunlu alanlar olmadan istek kabul ediliyor: {where}",
            "Belgede zorunlu olarak işaretlenen alanlar gönderilmediği halde istek işlendi.",
            ["Gövde şemasını sunucu tarafında doğrulayın; eksik alanlarda 422 döndürün.",
             "Belgedeki `required` listesinin gerçek davranışla aynı olduğundan emin olun."],
            source=ErrorSource.INPUT_VALIDATION,
            location=_location(test, fields=[]) + " · zorunlu alan kontrolü",
        )
    if "wrong-type" in tags:
        return build(
            f"Yanlış tipte veri kabul ediliyor: {where}",
            "Sayı beklenen alana metin gönderildiğinde istek reddedilmedi.",
            ["Gelen veriyi tipine göre doğrulayın (şema doğrulama kütüphanesi kullanın).",
             "Dönüştürülemeyen değerlerde 422 döndürün; sessizce varsayılan değere çevirmeyin."],
            source=ErrorSource.INPUT_VALIDATION,
            location=_location(test, label="tip kontrolü yapılmayan alanlar"),
        )
    if "error-handling" in tags:
        return build(
            f"Olmayan kayıt için uygun yanıt verilmiyor: {where}",
            f"Var olmayan bir kimlikle yapılan istek HTTP {status} döndü; 404 bekleniyordu.",
            ["Kayıt bulunamadığında 404 döndürün.",
             "Boş bir nesneyle 200 döndürmek istemcileri yanıltır; hatayı açıkça bildirin."],
            source=ErrorSource.API_CONTRACT,
            location=_location(test, fields=[]) + " · kayıt bulunamadı durumu",
        )

    # --- Performance ------------------------------------------------------
    if test.criterion == TestCriterion.PERFORMANCE:
        threshold = next((a.expected_value for a in test.assertions
                          if a.assertion_type == AssertionType.RESPONSE_TIME_UNDER_MS), None)
        near = threshold is not None and result.elapsed_ms < float(threshold) * 1.5
        return build(
            f"Yavaş yanıt ({result.elapsed_ms:.0f} ms, eşik {threshold:.0f} ms): {where}" if threshold
            else f"Yavaş yanıt: {where}",
            "Yanıt süresi belirlenen eşiği aştı.",
            ["Ölçümü birkaç kez tekrarlayın; tek ölçüm ağ dalgalanmasından etkilenebilir.",
             "Sunucu tarafında yavaş sorguları bulun (indeks eksikliği, N+1 sorguları).",
             "Sık okunan veriyi önbelleğe alın; büyük listeleri sayfalayın (pagination)."],
            noteworthy=not near,
            note=("Süre eşiğe çok yakın; uzak sunucularda bu fark ağ gecikmesinden kaynaklanabilir. "
                  "Eşiği 2000–3000 ms yapıp tekrar ölçün.") if near else None,
            source=ErrorSource.PERFORMANCE,
            location=f"{_where(test)} · ölçülen {result.elapsed_ms:.0f} ms",
        )

    # --- Web integrity ----------------------------------------------------
    if "assets" in tags:
        return build(
            f"Eksik dosya (HTTP {status}): {test.url}",
            "Sayfanın yüklediği bir görsel, script veya stil dosyası bulunamadı.",
            ["Dosya yolunu düzeltin veya eksik dosyayı sunucuya yükleyin.",
             "Bu bir script/stil dosyasıysa sayfanın görünümü veya işlevi bozulmuş olabilir; öncelik verin."],
            noteworthy=evidence.severity.rank >= Severity.MEDIUM.rank,
            source=ErrorSource.CONTENT,
            location=f"Eksik dosya: {test.url}",
        )
    if "links" in tags:
        return build(
            f"Kırık bağlantı (HTTP {status}): {test.url}",
            "Sayfadaki bir bağlantı çalışmayan bir adrese gidiyor.",
            ["Bağlantıyı güncelleyin veya kaldırın.",
             "Sayfa taşındıysa eski adresten yeni adrese kalıcı yönlendirme (301) ekleyin."],
            note="Kullanıcı deneyimi ve arama motoru sıralaması için önemlidir; güvenlik riski taşımaz.",
            source=ErrorSource.CONTENT,
            location=f"Kırık bağlantı hedefi: {test.url}",
        )

    # --- Functional mismatch ---------------------------------------------
    schema_errors = _failed(result, AssertionType.BODY_MATCHES_SCHEMA, test)
    if schema_errors:
        return build(
            f"Yanıt, belgedeki şemayla uyuşmuyor: {where}",
            "Yanıtın yapısı OpenAPI belgesinde tanımlanan şemadan farklı; istemciler hatalı veri okuyabilir.",
            [*schema_errors[:3],
             "Ya yanıtı belgeye uygun hale getirin ya da belgeyi gerçek yanıta göre güncelleyin."],
            source=ErrorSource.API_CONTRACT,
            location=_location(test, fields=[]) + " · yanıt gövdesi",
        )
    if status in (400, 404, 422) and test.criterion == TestCriterion.FUNCTIONAL:
        return build(
            f"Geçerli sayılan istek reddedildi (HTTP {status}): {where}",
            "BugHunter belgedeki tanıma göre geçerli bir istek gönderdi ama sunucu reddetti.",
            ["Kanıt scriptindeki isteği inceleyin: kullanılan kimlik/değerler gerçekte var mı?",
             "OpenAPI belgesine parametreler için gerçek `example` değerleri ekleyin; BugHunter bunları kullanır.",
             "İstek gerçekten geçerliyse sunucunun doğrulama kuralını kontrol edin."],
            noteworthy=False,
            note=("Muhtemelen yanlış alarm: BugHunter, belgede örnek değer yoksa uydurma değerler (ör. id=1) "
                  "kullanır ve bu kayıt hedefte bulunmayabilir."),
            source=ErrorSource.TEST_SETUP,
            location=_location(test, label="test değerleri"),
        )
    if status in (401, 403) and test.criterion == TestCriterion.FUNCTIONAL:
        return build(
            f"Erişim reddedildi (HTTP {status}): {where}",
            "Endpoint kimlik doğrulaması istiyor ve test kimlik bilgisi olmadan veya yetersiz yetkiyle çalıştı.",
            ["Panelde 'Authorization başlığı' alanına geçerli bir token girip testi tekrarlayın."],
            noteworthy=False,
            note="Uygulama hatası değil; kimlik bilgisi verilmediği için beklenen bir sonuç.",
            source=ErrorSource.TEST_SETUP,
            location=_location(test, fields=[]) + " · kimlik bilgisi gerekiyor",
        )

    reasons = "; ".join(result.failed_assertions) or "beklenen davranış gerçekleşmedi"
    return build(
        f"Beklenmeyen davranış: {where}",
        f"Test başarısız oldu: {reasons}.",
        ["Kanıt scriptini çalıştırıp yanıtı inceleyin.",
         "Beklenen davranış belgede doğru tanımlanmış mı kontrol edin."],
        source=ErrorSource.UNKNOWN,
    )

