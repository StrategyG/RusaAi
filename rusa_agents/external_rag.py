"""Policy-bound client for a normalized external RAG retrieval API.

This module accepts only administrator-configured endpoints and allowlisted
hosts. Retrieved passages remain untrusted evidence, never instructions.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class ExternalRAGError(RuntimeError):
    """Safe external-RAG error without provider response bodies or credentials."""


@dataclass(frozen=True)
class ExternalRAGEvidence:
    evidence_id: str
    text: str
    source_uri: str
    title: str
    revision: str
    content_sha256: str
    locator: str
    score: float | None = None
    metadata: dict | None = None


def validate_endpoint(endpoint: str, allowed_hosts: set[str] | frozenset[str]) -> str:
    parsed = urlsplit(endpoint)
    host = (parsed.hostname or "").lower()
    allowlist = {item.strip().lower() for item in allowed_hosts}
    if (
        parsed.scheme != "https"
        or not host
        or host not in allowlist
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1/retrieve"
    ):
        raise ValueError("External RAG endpoint must be an allowlisted HTTPS /v1/retrieve URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("External RAG endpoint must not target a private or non-global IP address")
    return endpoint


def _validate_evidence(item: object) -> ExternalRAGEvidence:
    if not isinstance(item, dict):
        raise ExternalRAGError("External RAG returned an invalid evidence record")
    required = {"id", "text", "source"}
    if not required.issubset(item):
        raise ExternalRAGError("External RAG evidence is missing required fields")
    source = item["source"]
    if not isinstance(source, dict):
        raise ExternalRAGError("External RAG source metadata is invalid")
    evidence_id, text = item["id"], item["text"]
    uri, title = source.get("uri"), source.get("title")
    revision, digest, locator = source.get("revision"), source.get("content_sha256"), source.get("locator")
    parsed_uri = urlsplit(uri) if isinstance(uri, str) else None
    if (
        not isinstance(evidence_id, str) or not evidence_id or len(evidence_id) > 256
        or not isinstance(text, str) or not text.strip() or len(text) > 20_000
        or parsed_uri is None or parsed_uri.scheme != "https" or not parsed_uri.hostname
        or parsed_uri.username or parsed_uri.password
        or not isinstance(title, str) or not title.strip() or len(title) > 500
        or not isinstance(revision, str) or not revision.strip() or len(revision) > 256
        or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not isinstance(locator, str) or not locator.strip() or len(locator) > 1000
    ):
        raise ExternalRAGError("External RAG evidence failed provenance validation")
    score = item.get("score")
    if score is not None and (
        isinstance(score, bool) or not isinstance(score, (int, float))
        or not math.isfinite(score) or not 0 <= score <= 1
    ):
        raise ExternalRAGError("External RAG ranking score is invalid")
    metadata = item.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ExternalRAGError("External RAG metadata must be an object")
    return ExternalRAGEvidence(
        evidence_id=evidence_id,
        text=text,
        source_uri=uri,
        title=title,
        revision=revision,
        content_sha256=digest,
        locator=locator,
        score=float(score) if score is not None else None,
        metadata=metadata,
    )


class ExternalRAGClient:
    def __init__(
        self,
        *,
        endpoint: str,
        bearer_token: str,
        allowed_hosts: set[str] | frozenset[str],
        timeout_seconds: float = 15.0,
        max_response_bytes: int = 1_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.endpoint = validate_endpoint(endpoint, allowed_hosts)
        if not isinstance(bearer_token, str) or len(bearer_token) < 32 or len(bearer_token) > 4096:
            raise ValueError("External RAG bearer token must contain 32–4096 characters")
        if not 1 <= timeout_seconds <= 60 or not 1024 <= max_response_bytes <= 5_000_000:
            raise ValueError("External RAG client limits are invalid")
        self._token = bearer_token
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.transport = transport

    async def search(self, query: str, *, top_k: int = 5, filters: dict | None = None) -> list[ExternalRAGEvidence]:
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError("External RAG query must contain 1–4000 characters")
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise ValueError("External RAG top_k must be between 1 and 20")
        filters = filters or {}
        try:
            encoded_filters = json.dumps(filters, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("External RAG filters must be finite JSON data") from exc
        if not isinstance(filters, dict) or len(encoded_filters) > 8000:
            raise ValueError("External RAG filters are invalid or too large")
        payload = {"schema_version": "1.0", "query": query, "top_k": top_k, "filters": filters}
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                async with client.stream("POST", self.endpoint, json=payload, headers=headers) as response:
                    response.raise_for_status()
                    body_bytes = bytearray()
                    async for chunk in response.aiter_bytes():
                        body_bytes.extend(chunk)
                        if len(body_bytes) > self.max_response_bytes:
                            raise ExternalRAGError("External RAG response exceeded the configured size limit")
                    body = json.loads(bytes(body_bytes))
        except ExternalRAGError:
            raise
        except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
            raise ExternalRAGError("External RAG provider is unavailable or returned invalid JSON") from exc

        if (
            not isinstance(body, dict)
            or body.get("schema_version") != "1.0"
            or not isinstance(body.get("results"), list)
            or len(body["results"]) > top_k
        ):
            raise ExternalRAGError("External RAG response does not match contract v1")
        evidence = [_validate_evidence(item) for item in body["results"]]
        seen: set[str] = set()
        unique: list[ExternalRAGEvidence] = []
        for item in evidence:
            identity = hashlib.sha256(
                (item.evidence_id + "|" + item.content_sha256).encode("utf-8")
            ).hexdigest()
            if identity not in seen:
                seen.add(identity)
                unique.append(item)
        return unique
