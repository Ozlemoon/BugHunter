"""
OpenAPI & Swagger Specification Parser.
Supports OpenAPI 3.x and Swagger 2.0 specifications from URLs or local files.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import httpx
import yaml

from core.models import EndpointSpec, ParameterSpec

HTTP_METHODS = ["get", "post", "put", "delete", "patch", "options", "head"]


class SpecError(ValueError):
    """The document could not be read as an OpenAPI / Swagger specification."""


class ApiTargetParser:
    """Parses OpenAPI and Swagger specifications into standardized EndpointSpec models."""

    def __init__(self, spec_source: str, timeout: float = 10.0, headers: Optional[Dict[str, str]] = None):
        """
        :param spec_source: URL (http/https) or local filepath (.json, .yaml, .yml)
        """
        self.spec_source = spec_source
        self.timeout = timeout
        self.headers = headers or {}
        self.raw_spec: Dict[str, Any] = {}
        self.endpoints: List[EndpointSpec] = []

    @property
    def is_remote(self) -> bool:
        return self.spec_source.startswith(("http://", "https://"))

    def load_spec(self) -> Dict[str, Any]:
        """Loads and parses the specification from remote URL or local file."""
        if self.is_remote:
            with httpx.Client(timeout=self.timeout, headers=self.headers) as client:
                resp = client.get(self.spec_source)
                resp.raise_for_status()
                content = resp.text
        else:
            with open(self.spec_source, encoding="utf-8") as f:
                content = f.read()
        try:
            spec = json.loads(content)
        except ValueError:
            try:
                spec = yaml.safe_load(content)
            except yaml.YAMLError as error:
                raise SpecError("Document is neither valid JSON nor YAML.") from error
        if not isinstance(spec, dict) or "paths" not in spec:
            raise SpecError("Document has no 'paths' section; is this an OpenAPI/Swagger spec?")
        self.raw_spec = spec
        return self.raw_spec

    def base_url(self) -> str:
        """
        Resolves the API base URL: the first `servers` entry (OpenAPI 3),
        host/basePath (Swagger 2), or the origin of the spec URL.
        """
        spec_origin = ""
        if self.is_remote:
            parsed = urlparse(self.spec_source)
            spec_origin = f"{parsed.scheme}://{parsed.netloc}"

        servers = self.raw_spec.get("servers") or []
        if servers and isinstance(servers[0], dict) and servers[0].get("url"):
            server = servers[0]["url"]
            if server.startswith(("http://", "https://")):
                return server.rstrip("/")
            if spec_origin:
                return urljoin(spec_origin + "/", server).rstrip("/")

        if self.raw_spec.get("swagger") and self.raw_spec.get("host"):
            scheme = (self.raw_spec.get("schemes") or ["https"])[0]
            return f"{scheme}://{self.raw_spec['host']}{self.raw_spec.get('basePath', '')}".rstrip("/")

        if spec_origin:
            return spec_origin
        raise SpecError("Cannot infer the API base URL; the spec has no absolute 'servers' entry.")

    def parse(self) -> List[EndpointSpec]:
        """Parses paths and operations from OpenAPI / Swagger spec."""
        if not self.raw_spec:
            self.load_spec()

        global_security = self.raw_spec.get("security") or []
        endpoints_list: List[EndpointSpec] = []

        for path_str, path_item in (self.raw_spec.get("paths") or {}).items():
            if not isinstance(path_item, dict):
                continue
            shared_params = path_item.get("parameters", [])

            for method_name, operation in path_item.items():
                if method_name.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                    continue

                parameters = self._parse_parameters(shared_params + operation.get("parameters", []))
                body_schema, body_required = self._parse_request_body(operation)
                security = operation.get("security", global_security)

                endpoints_list.append(
                    EndpointSpec(
                        path=path_str,
                        method=method_name.upper(),
                        summary=operation.get("summary"),
                        description=operation.get("description"),
                        parameters=parameters,
                        request_body_schema=body_schema,
                        request_body_required=body_required,
                        responses=operation.get("responses", {}),
                        success_response_schema=self._success_schema(operation),
                        requires_auth=bool(security) and any(bool(req) for req in security),
                        tags=operation.get("tags", []),
                    )
                )

        self.endpoints = endpoints_list
        return endpoints_list

    # ------------------------------------------------------------------
    # $ref resolution
    # ------------------------------------------------------------------
    def _resolve_ref(self, ref_str: str) -> Dict[str, Any]:
        """Resolves internal JSON pointer #/components/schemas/... or #/definitions/..."""
        if not ref_str.startswith("#/"):
            return {}
        curr: Any = self.raw_spec
        for part in ref_str[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(curr, dict) and part in curr:
                curr = curr[part]
            else:
                return {}
        return curr if isinstance(curr, dict) else {}

    def resolve(self, node: Any, depth: int = 0, seen: Optional[frozenset] = None) -> Any:
        """Recursively inlines $ref pointers (cycle- and depth-safe)."""
        seen = seen or frozenset()
        if depth > 20:
            return {}
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref in seen:
                    return {}
                return self.resolve(self._resolve_ref(ref), depth + 1, seen | {ref})
            return {key: self.resolve(value, depth + 1, seen) for key, value in node.items()}
        if isinstance(node, list):
            return [self.resolve(item, depth + 1, seen) for item in node]
        return node

    # ------------------------------------------------------------------
    # Operation pieces
    # ------------------------------------------------------------------
    def _parse_parameters(self, raw_params: List[Dict[str, Any]]) -> List[ParameterSpec]:
        params: Dict[tuple, ParameterSpec] = {}
        for p in raw_params:
            p = self.resolve(p)
            if not isinstance(p, dict) or not p.get("name"):
                continue
            schema = p.get("schema", {}) or {}
            spec = ParameterSpec(
                name=p["name"],
                in_location=p.get("in", "query"),
                required=p.get("required", False),
                param_type=self._schema_type(schema) or p.get("type", "string"),
                default=schema.get("default", p.get("default")),
                example=self._example(p, schema),
                description=p.get("description"),
            )
            params[(spec.name, spec.in_location)] = spec  # operation-level overrides path-level
        return list(params.values())

    @staticmethod
    def _schema_type(schema: Dict[str, Any]) -> Optional[str]:
        if schema.get("type"):
            return schema["type"] if isinstance(schema["type"], str) else schema["type"][0]
        for option in schema.get("anyOf", []) + schema.get("oneOf", []):
            if isinstance(option, dict) and option.get("type") not in (None, "null"):
                return option["type"]
        return None

    @staticmethod
    def _example(param: Dict[str, Any], schema: Dict[str, Any]) -> Any:
        if "example" in param:
            return param["example"]
        examples = param.get("examples")
        if isinstance(examples, dict):
            for example in examples.values():
                if isinstance(example, dict) and "value" in example:
                    return example["value"]
        if "example" in schema:
            return schema["example"]
        if isinstance(schema.get("examples"), list) and schema["examples"]:
            return schema["examples"][0]
        return None

    def _parse_request_body(self, operation: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], bool]:
        # OpenAPI 3.x
        if "requestBody" in operation:
            rb = self.resolve(operation["requestBody"])
            content = rb.get("content", {})
            for media_type in ["application/json", "multipart/form-data", "application/x-www-form-urlencoded"]:
                if media_type in content:
                    return content[media_type].get("schema", {}), bool(rb.get("required"))

        # Swagger 2.0 (body in parameters)
        for p in operation.get("parameters", []):
            p = self.resolve(p)
            if isinstance(p, dict) and p.get("in") == "body":
                return p.get("schema", {}), bool(p.get("required"))
        return None, False

    def _success_schema(self, operation: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        responses = operation.get("responses", {}) or {}
        for code in ("200", "201", "202"):
            response = self.resolve(responses.get(code) or responses.get(int(code)) or {})
            if not response:
                continue
            schema = response.get("content", {}).get("application/json", {}).get("schema") \
                or response.get("schema")  # Swagger 2.0
            if isinstance(schema, dict) and schema:
                return schema
        return None
