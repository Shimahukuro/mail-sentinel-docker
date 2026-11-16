"""受信時の認証結果の取得と、共通のSPF・DMARCポリシー。"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from email.parser import BytesHeaderParser
from typing import Protocol


_AUTHSERV = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*", re.ASCII | re.IGNORECASE)
_TOKEN = r"[a-z][a-z0-9_-]*"
_QUOTED = r'"(?:[^"\\]|\\.)*"'
_RESULT = re.compile(
    rf"({_TOKEN})(?:/1)?[ \t]*=[ \t]*({_TOKEN})"
    rf'(?:[ \t]+(?:{_TOKEN}(?:\.{_TOKEN})?)[ \t]*=[ \t]*(?:{_QUOTED}|[^ \t"();\\]+))*',
    re.ASCII | re.IGNORECASE,
)
_METHODS = ("spf", "dmarc", "dkim")
_RESULTS = {"pass", "fail", "softfail", "neutral", "none", "temperror", "permerror",
            "policy", "hardfail", "bestguesspass"}


@dataclass(frozen=True)
class Evidence:
    reason: str
    source: str | None = None
    spf: str | None = None
    dmarc: str | None = None
    dkim: str | None = None

    @property
    def trusted(self) -> bool:
        return self.source is not None


class AuthenticationSource(Protocol):
    def read(self, raw: bytes) -> Evidence: ...


def _clauses(value: str) -> Iterator[str]:
    """引用符や入れ子のコメント内のセミコロンを区切りとして扱わない。"""
    value = re.sub(r"\r?\n(?=[ \t])", "", value)
    buffer: list[str] = []
    depth = 0
    quoted = escaped = False
    for char in value:
        if char != "\t" and not " " <= char <= "~":
            raise ValueError
        if escaped:
            if quoted:
                buffer.append(char)
            escaped = False
        elif depth:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "\\":
                escaped = True
        elif quoted:
            buffer.append(char)
            if char == '"':
                quoted = False
            elif char == "\\":
                escaped = True
        elif char == "(":
            depth = 1
            buffer.append(" ")
        elif char == '"':
            quoted = True
            buffer.append(char)
        elif char == ";":
            yield "".join(buffer).strip()
            buffer.clear()
        elif char in ")\\":
            raise ValueError
        else:
            buffer.append(char)
    if depth or quoted or escaped:
        raise ValueError
    yield "".join(buffer).strip()


@dataclass(frozen=True)
class ScrubbedAuthenticationResults:
    """外部の認証ヘッダーを除去することを確認済みの受信経路から取得する。"""

    authserv_id: str

    def read(self, raw: bytes) -> Evidence:
        message = BytesHeaderParser().parsebytes(raw)
        if message.defects:
            return Evidence("malformed_headers")
        results: dict[str, str] | None = None
        for name, value in message.raw_items():
            if name.lower() != "authentication-results":
                continue
            try:
                clauses = _clauses(value)
                server = next(clauses).lower().split()
                if not server or server[0] not in (self.authserv_id, f'"{self.authserv_id}"'):
                    continue
                if results is not None:
                    return Evidence("ambiguous_results")
                if server[1:] not in ([], ["1"]):
                    raise ValueError
                results = {}
                methods = list(clauses)
                if len(methods) == 1 and methods[0].lower() == "none":
                    continue
                if not methods:
                    raise ValueError
                for method in methods:
                    match = _RESULT.fullmatch(method)
                    if not match:
                        raise ValueError
                    name, result = (token.lower() for token in match.groups())
                    if name in _METHODS:
                        if name in ("spf", "dmarc") and name in results:
                            return Evidence("ambiguous_results")
                        # 複数のDKIM署名が異なる結果なら、診断上は不明とする。
                        results[name] = result if results.get(name, result) == result else ""
            except ValueError:
                return Evidence("malformed_authentication_results")
        if results is None:
            return Evidence("missing_authentication_results")
        return Evidence("verified_ingress", "authentication_results", **{
            method: results.get(method) if results.get(method) in _RESULTS else None
            for method in _METHODS
        })


@dataclass(frozen=True)
class AuthenticationPolicy:
    spf_fail_score: float = 0.5
    dmarc_fail_score: float = 0.5

    def evaluate(self, evidence: Evidence) -> tuple[bool, float, str | None]:
        if not evidence.trusted:
            return False, 0.0, None
        spf_failed, dmarc_failed = evidence.spf == "fail", evidence.dmarc == "fail"
        if spf_failed and dmarc_failed:
            return True, 0.0, "AUTH_SPF_DMARC_FAILURE"
        if spf_failed:
            return False, self.spf_fail_score, "AUTH_SPF_FAIL_SCORE"
        if dmarc_failed:
            return False, self.dmarc_fail_score, "AUTH_DMARC_FAIL_SCORE"
        return False, 0.0, None


@dataclass(frozen=True)
class AuthenticationSettings:
    reason: str
    source: AuthenticationSource | None = None
    policy: AuthenticationPolicy = AuthenticationPolicy()

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> AuthenticationSettings:
        source = env.get("AUTH_SOURCE", "disabled")
        if source == "disabled":
            return cls("disabled")
        if source != "authentication_results":
            return cls("unsupported_source")
        if env.get("AUTH_TRUST_BOUNDARY") != "verified_scrubbed_ingress":
            return cls("boundary_unverified")
        if env.get("IMAP_TLS_MODE") not in ("implicit", "starttls"):
            return cls("imap_tls_required")
        authserv_id = env.get("AUTH_AUTHSERV_ID", "").strip().lower()
        if not _AUTHSERV.fullmatch(authserv_id):
            return cls("invalid_authserv_id")
        try:
            spf, dmarc = (float(env.get(key, "0.5"))
                          for key in ("AUTH_SPF_FAIL_SCORE", "AUTH_DMARC_FAIL_SCORE"))
            if any(not math.isfinite(score) or score < 0 for score in (spf, dmarc)):
                raise ValueError
        except ValueError:
            return cls("invalid_authentication_scores")
        return cls("configured", ScrubbedAuthenticationResults(authserv_id),
                   AuthenticationPolicy(spf, dmarc))

    def read(self, raw: bytes) -> Evidence:
        if self.source is None:
            return Evidence(self.reason)
        return self.source.read(raw)
