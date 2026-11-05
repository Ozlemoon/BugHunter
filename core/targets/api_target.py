"""
OpenAPI & Swagger Specification Parser.
Supports OpenAPI 3.x and Swagger 2.0 specifications from URLs or local files.
"""

import json
from typing import Any, Dict, List, Optional
import httpx
import yaml

from core.models import EndpointSpec, ParameterSpec


class ApiTargetParser:
    """Parses OpenAPI and Swagger specifications into standardized EndpointSpec models."""

    def __init__(self, spec_source: str):
        """
        :param spec_source: URL (http/https) or local filepath (.json, .yaml, .yml)
        """
        self.spec_source = spec_source
        self.raw_spec: Dict[str, Any] = {}
        self.endpoints: List[EndpointSpec] = []

    def load_spec(self) -> Dict[str, Any]:
        """Loads and parses the specification from remote URL or local file."""
        if self.spec_source.startswith("http://") or self.spec_source.startswith("https://"):
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(self.spec_source)
                resp.raise_for_status()
                try:
                    self.raw_spec = resp.json()
                except Exception:
                    self.raw_spec = yaml.safe_load(resp.text)
        else:
            with open(self.spec_source, "r", encoding="utf-8") as f:
                content = f.read()
                try:
                    self.raw_spec = json.loads(content)
                except Exception:
                    self.raw_spec = yaml.safe_load(content)
        return self.raw_spec

    def parse(self) -> List[EndpointSpec]:
        """Parses paths and operations from OpenAPI / Swagger spec."""
        if not self.raw_spec:
            self.load_spec()

        paths = self.raw_spec.get("paths", {})
        endpoints_list: List[EndpointSpec] = []

        for path_str, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue

            for method_name, operation in path_item.items():
                method_name_lower = method_name.lower()
                if method_name_lower not in ["get", "post", "put", "delete", "patch", "options", "head"]:
                    continue

                if not isinstance(operation, dict):
                    continue

                parameters = self._parse_parameters(operation.get("parameters", []))
                request_body = self._parse_request_body(operation)
                responses = operation.get("responses", {})
                tags = operation.get("tags", [])

                endpoints_list.append(
                    EndpointSpec(
                        path=path_str,
                        method=method_name.upper(),
                        summary=operation.get("summary"),
                        description=operation.get("description"),
                        parameters=parameters,
                        request_body_schema=request_body,
                        responses=responses,
                        tags=tags
                    )
                )

        self.endpoints = endpoints_list
        return endpoints_list

    def _resolve_ref(self, ref_str: str) -> Dict[str, Any]:
        """Resolves internal JSON pointer #/components/schemas/... or #/definitions/..."""
        if not ref_str.startswith("#/"):
            return {}
        parts = ref_str.lstrip("#/").split("/")
        curr = self.raw_spec
        for p in parts:
            if isinstance(curr, dict) and p in curr:
                curr = curr[p]
            else:
                return {}
        return curr if isinstance(curr, dict) else {}

    def _parse_parameters(self, raw_params: List[Dict[str, Any]]) -> List[ParameterSpec]:
        params: List[ParameterSpec] = []
        for p in raw_params:
            if "$ref" in p:
                p = self._resolve_ref(p["$ref"])
            if not isinstance(p, dict):
                continue

            param_name = p.get("name", "")
            in_loc = p.get("in", "query")
            required = p.get("required", False)
            schema = p.get("schema", {})
            param_type = schema.get("type", p.get("type", "string"))
            default_val = schema.get("default", p.get("default"))

            params.append(
                ParameterSpec(
                    name=param_name,
                    in_location=in_loc,
                    required=required,
                    param_type=param_type,
                    default=default_val,
                    description=p.get("description")
                )
            )
        return params

    def _parse_request_body(self, operation: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # OpenAPI 3.x
        if "requestBody" in operation:
            rb = operation["requestBody"]
            if "$ref" in rb:
                rb = self._resolve_ref(rb["$ref"])
            content = rb.get("content", {})
            for media_type in ["application/json", "multipart/form-data", "application/x-www-form-urlencoded"]:
                if media_type in content:
                    schema = content[media_type].get("schema", {})
                    if "$ref" in schema:
                        schema = self._resolve_ref(schema["$ref"])
                    return schema

        # Swagger 2.0 (body in parameters)
        for p in operation.get("parameters", []):
            if p.get("in") == "body":
                schema = p.get("schema", {})
                if "$ref" in schema:
                    schema = self._resolve_ref(schema["$ref"])
                return schema
        return None
