"""
BugHunter AI Core Domain Models.
Defines schemas for targets, test criteria, test cases, execution results,
bug evidence, and fix proposals.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, PrivateAttr


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        return {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}[self.value]


class TargetType(str, Enum):
    API = "API"
    WEB = "WEB"
    REPO = "REPO"


class TestCriterion(str, Enum):
    __test__ = False
    FUNCTIONAL = "FUNCTIONAL"           # Happy paths, expected response schemas
    BOUNDARY = "BOUNDARY"               # Edge cases, negative numbers, empty/null values
    SECURITY = "SECURITY"               # Broken auth, input injections, unauthorized access
    PERFORMANCE = "PERFORMANCE"         # Latency thresholds & SLA criteria (<500ms)
    WEB_VALIDATION = "WEB_VALIDATION"   # Broken links, broken assets, form validation


class AssertionType(str, Enum):
    STATUS_CODE_EQUALS = "STATUS_CODE_EQUALS"
    STATUS_CODE_IN = "STATUS_CODE_IN"
    RESPONSE_TIME_UNDER_MS = "RESPONSE_TIME_UNDER_MS"
    BODY_CONTAINS = "BODY_CONTAINS"
    BODY_MATCHES_SCHEMA = "BODY_MATCHES_SCHEMA"
    HEADER_EXISTS = "HEADER_EXISTS"
    NO_SERVER_ERROR = "NO_SERVER_ERROR"  # Status code must not be 500-599
    NOT_REFLECTED_IN_HTML = "NOT_REFLECTED_IN_HTML"  # Probe string must not echo unescaped in HTML


class TestAssertion(BaseModel):
    __test__ = False
    assertion_type: AssertionType
    expected_value: Any
    description: str


class TestCase(BaseModel):
    __test__ = False
    id: str
    target_type: TargetType
    criterion: TestCriterion
    title: str
    description: str
    method: str = "GET"                 # GET, POST, PUT, DELETE, etc.
    url: str                            # Full URL or path
    endpoint: Optional[str] = None      # Templated path, e.g. /users/{user_id}/profile
    headers: Dict[str, str] = Field(default_factory=dict)
    query_params: Dict[str, Any] = Field(default_factory=dict)
    payload: Optional[Any] = None       # JSON dict, form data, or raw body
    is_form_data: bool = False
    assertions: List[TestAssertion] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    severity: Optional[Severity] = None  # Overrides the analyzer's default severity


# --- Parsed Target Specifications ---

class ParameterSpec(BaseModel):
    name: str
    in_location: str                    # query, path, header, cookie
    required: bool = False
    param_type: str = "string"
    default: Optional[Any] = None
    example: Optional[Any] = None
    description: Optional[str] = None


class EndpointSpec(BaseModel):
    path: str
    method: str
    summary: Optional[str] = None
    description: Optional[str] = None
    parameters: List[ParameterSpec] = Field(default_factory=list)
    request_body_schema: Optional[Dict[str, Any]] = None
    request_body_required: bool = False
    responses: Dict[str, Any] = Field(default_factory=dict)
    success_response_schema: Optional[Dict[str, Any]] = None
    requires_auth: bool = False         # Operation declares a security requirement
    tags: List[str] = Field(default_factory=list)


class FormFieldSpec(BaseModel):
    name: str
    field_type: str = "text"
    required: bool = False
    placeholder: Optional[str] = None
    value: Optional[str] = None


class WebFormSpec(BaseModel):
    action: str
    method: str = "POST"
    fields: List[FormFieldSpec] = Field(default_factory=list)


class WebLinkSpec(BaseModel):
    href: str
    text: str
    tag_name: str = "a"                 # a, img, script, link


class WebPageSpec(BaseModel):
    url: str
    title: Optional[str] = None
    links: List[WebLinkSpec] = Field(default_factory=list)
    assets: List[WebLinkSpec] = Field(default_factory=list)
    forms: List[WebFormSpec] = Field(default_factory=list)
    response_headers: Dict[str, str] = Field(default_factory=dict)


# --- Test Execution and Bug Evidence Models ---

class TestResult(BaseModel):
    __test__ = False
    test_id: str
    passed: bool
    status_code: Optional[int] = None
    elapsed_ms: float = 0.0
    response_body: Optional[str] = None
    response_headers: Dict[str, str] = Field(default_factory=dict)
    failed_assertions: List[str] = Field(default_factory=list)
    error_message: Optional[str] = None
    # The whole response body, kept in memory during a run only (never saved): screenshots
    # render exactly what the test received instead of sending the request again.
    _full_body: Optional[str] = PrivateAttr(default=None)


class BugEvidence(BaseModel):
    bug_id: str
    title: str
    target_type: TargetType
    criterion: TestCriterion
    target_url: str
    method: str
    test_id: Optional[str] = None
    endpoint: Optional[str] = None      # Templated path used to match fix rules
    status_code: Optional[int] = None
    tags: List[str] = Field(default_factory=list)
    payload: Optional[Any] = None
    expected_behavior: str
    actual_behavior: str
    reproduction_code: str              # Minimal standalone Python or curl script
    root_cause_hypothesis: str
    severity: Severity = Severity.MEDIUM


class FixStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"


class FixProposal(BaseModel):
    fix_id: str
    bug_id: str
    target_file: str
    title: str
    explanation: str
    unified_diff: str
    approved: bool = False
    patch_available: bool = True        # False when no safe automatic fix is known
    rule: Optional[str] = None          # Name of the fix rule that produced the patch
    source_sha256: Optional[str] = None  # Hash of target_file when the diff was produced
    status: FixStatus = FixStatus.PENDING


class RegressionReport(BaseModel):
    __test__ = False
    fix_id: str
    bug_id: str
    bug_fixed: bool
    before_pass_count: int
    before_fail_count: int
    after_pass_count: int
    after_fail_count: int
    regressions_detected: List[str] = Field(default_factory=list)
    verdict: str                        # CLEAN_FIX, REGRESSION_DETECTED, FIX_UNSUCCESSFUL
    summary: str
    rolled_back: bool = False

