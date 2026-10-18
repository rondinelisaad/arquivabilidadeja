from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

_PRODUCT_TOKEN = re.compile(r"^[A-Za-z_-]+$")
_PERCENT_ESCAPE = re.compile(r"^[0-9A-Fa-f]{2}$")
_UNSAFE_URI_CHARACTER = re.compile(r"[\x00-\x20\x7f\\]")
_UNRESERVED = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


class RobotsError(ValueError):
    """Raised when a robots policy input cannot be interpreted safely."""


class RobotsDisposition(StrEnum):
    ALLOW = "allow"
    DISALLOW = "disallow"


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    product_token: str = "arquivabilidade-ja"
    policy_id: str = "rfc9309-respect-1"
    parser_version: str = "rfc9309-1"
    max_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.product_token, str) or not _PRODUCT_TOKEN.fullmatch(
            self.product_token
        ):
            raise RobotsError("robots product token is invalid")
        for name, value in (
            ("policy_id", self.policy_id),
            ("parser_version", self.parser_version),
        ):
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 128
                or any(ord(character) < 33 or ord(character) > 126 for character in value)
            ):
                raise RobotsError(f"{name} is invalid")
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or not 512 * 1024 <= self.max_bytes <= 2 * 1024 * 1024
        ):
            raise RobotsError("robots parsing limit must be between 512 KiB and 2 MiB")


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    disposition: RobotsDisposition
    matched_rule: str | None
    matched_octets: int
    matched_group_count: int
    applicable_rule_count: int
    policy_id: str
    parser_version: str
    product_token: str
    source_state: str

    @property
    def allowed(self) -> bool:
        return self.disposition is RobotsDisposition.ALLOW

    def to_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "matched_rule": self.matched_rule,
            "matched_octets": self.matched_octets,
            "matched_group_count": self.matched_group_count,
            "applicable_rule_count": self.applicable_rule_count,
            "policy_id": self.policy_id,
            "parser_version": self.parser_version,
            "product_token": self.product_token,
            "source_state": self.source_state,
        }


@dataclass(frozen=True, slots=True)
class _Rule:
    directive: str
    pattern: str
    octets: int

    def matches(self, target: str) -> bool:
        end_anchored = self.pattern.endswith("$")
        pattern = self.pattern[:-1] if end_anchored else self.pattern
        if not end_anchored:
            pattern += "*"
        return self._glob_matches(pattern, target)

    @staticmethod
    def _glob_matches(pattern: str, target: str) -> bool:
        pattern_index = 0
        target_index = 0
        star_index = -1
        retry_target_index = 0
        while target_index < len(target):
            if (
                pattern_index < len(pattern)
                and pattern[pattern_index] != "*"
                and pattern[pattern_index] == target[target_index]
            ):
                pattern_index += 1
                target_index += 1
            elif pattern_index < len(pattern) and pattern[pattern_index] == "*":
                star_index = pattern_index
                pattern_index += 1
                retry_target_index = target_index
            elif star_index >= 0:
                pattern_index = star_index + 1
                retry_target_index += 1
                target_index = retry_target_index
            else:
                return False
        while pattern_index < len(pattern) and pattern[pattern_index] == "*":
            pattern_index += 1
        return pattern_index == len(pattern)


@dataclass(frozen=True, slots=True)
class _Group:
    agents: tuple[str, ...]
    rules: tuple[_Rule, ...]


class RobotsRules:
    """Parsed RFC 9309 groups with deterministic matching decisions."""

    def __init__(
        self,
        groups: tuple[_Group, ...],
        *,
        policy: RobotsPolicy,
        source_state: str = "parsed",
        fallback: RobotsDisposition | None = None,
    ) -> None:
        self._groups = groups
        self._policy = policy
        self._source_state = source_state
        self._fallback = fallback

    @classmethod
    def parse(cls, body: bytes, *, policy: RobotsPolicy = RobotsPolicy()) -> RobotsRules:
        if not isinstance(body, bytes):
            raise RobotsError("robots content must be bytes")
        if len(body) > policy.max_bytes:
            raise RobotsError("robots content exceeds the parsing limit")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RobotsError("robots content is not valid UTF-8") from exc
        return cls(cls._parse_groups(text), policy=policy)

    @classmethod
    def from_http(
        cls,
        status_code: int,
        body: bytes = b"",
        *,
        policy: RobotsPolicy = RobotsPolicy(),
    ) -> RobotsRules:
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
        ):
            raise RobotsError("robots HTTP status is invalid")
        if 200 <= status_code < 300:
            return cls.parse(body, policy=policy)
        if 400 <= status_code < 500:
            return cls(
                (),
                policy=policy,
                source_state="unavailable",
                fallback=RobotsDisposition.ALLOW,
            )
        if 500 <= status_code < 600:
            return cls.unreachable(policy=policy)
        raise RobotsError("robots redirects and provisional responses require resolution")

    @classmethod
    def unreachable(cls, *, policy: RobotsPolicy = RobotsPolicy()) -> RobotsRules:
        return cls(
            (),
            policy=policy,
            source_state="unreachable",
            fallback=RobotsDisposition.DISALLOW,
        )

    def decide(self, uri: str) -> RobotsDecision:
        target = self._target(uri)
        if target == "/robots.txt":
            return self._decision(
                RobotsDisposition.ALLOW,
                matched_rule=None,
                matched_octets=0,
                matched_groups=0,
                applicable_rules=0,
            )
        if self._fallback is not None:
            return self._decision(
                self._fallback,
                matched_rule=None,
                matched_octets=0,
                matched_groups=0,
                applicable_rules=0,
            )

        token = self._policy.product_token.casefold()
        exact = tuple(
            group for group in self._groups if token in {agent.casefold() for agent in group.agents}
        )
        applicable = exact or tuple(group for group in self._groups if "*" in group.agents)
        rules = tuple(rule for group in applicable for rule in group.rules)
        matches = tuple(rule for rule in rules if rule.matches(target))
        if not matches:
            return self._decision(
                RobotsDisposition.ALLOW,
                matched_rule=None,
                matched_octets=0,
                matched_groups=len(applicable),
                applicable_rules=len(rules),
            )
        longest = max(rule.octets for rule in matches)
        finalists = tuple(rule for rule in matches if rule.octets == longest)
        directive = "allow" if any(rule.directive == "allow" for rule in finalists) else "disallow"
        return self._decision(
            RobotsDisposition(directive),
            matched_rule=directive,
            matched_octets=longest,
            matched_groups=len(applicable),
            applicable_rules=len(rules),
        )

    def _decision(
        self,
        disposition: RobotsDisposition,
        *,
        matched_rule: str | None,
        matched_octets: int,
        matched_groups: int,
        applicable_rules: int,
    ) -> RobotsDecision:
        return RobotsDecision(
            disposition=disposition,
            matched_rule=matched_rule,
            matched_octets=matched_octets,
            matched_group_count=matched_groups,
            applicable_rule_count=applicable_rules,
            policy_id=self._policy.policy_id,
            parser_version=self._policy.parser_version,
            product_token=self._policy.product_token,
            source_state=self._source_state,
        )

    @classmethod
    def _parse_groups(cls, text: str) -> tuple[_Group, ...]:
        groups: list[_Group] = []
        agents: list[str] = []
        rules: list[_Rule] = []
        rules_started = False

        def finish() -> None:
            nonlocal agents, rules, rules_started
            if agents:
                groups.append(_Group(tuple(agents), tuple(rules)))
            agents = []
            rules = []
            rules_started = False

        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, value = (part.strip() for part in line.split(":", 1))
            field = field.casefold()
            if field == "user-agent":
                if rules_started:
                    finish()
                if _PRODUCT_TOKEN.fullmatch(value) or value == "*":
                    agents.append(value.casefold())
                continue
            if field not in {"allow", "disallow"} or not agents:
                continue
            rules_started = True
            if not value:
                continue
            try:
                pattern = cls._normalize_octets(value, pattern=True)
            except RobotsError:
                continue
            rules.append(
                _Rule(
                    directive=field,
                    pattern=pattern,
                    octets=cls._specificity(pattern),
                )
            )
        finish()
        return tuple(groups)

    @classmethod
    def _target(cls, uri: str) -> str:
        if (
            not isinstance(uri, str)
            or not 1 <= len(uri) <= 2048
            or _UNSAFE_URI_CHARACTER.search(uri)
        ):
            raise RobotsError("robots decision URI is invalid")
        try:
            parsed = urlsplit(uri)
            port = parsed.port
        except ValueError as exc:
            raise RobotsError("robots decision URI is invalid") from exc
        if parsed.scheme.lower() not in {"http", "https"} or parsed.hostname is None:
            raise RobotsError("robots decision URI must be absolute HTTP(S)")
        if parsed.username is not None or parsed.password is not None:
            raise RobotsError("robots decision URI credentials are forbidden")
        if not parsed.hostname.rstrip(".") or port == 0:
            raise RobotsError("robots decision URI authority is invalid")
        path_query = parsed.path or "/"
        if parsed.query:
            path_query += f"?{parsed.query}"
        return cls._normalize_octets(path_query, pattern=False)

    @staticmethod
    def _specificity(pattern: str) -> int:
        value = pattern.rstrip("$").replace("*", "")
        octets = 0
        index = 0
        while index < len(value):
            if (
                value[index] == "%"
                and index + 2 < len(value)
                and _PERCENT_ESCAPE.fullmatch(value[index + 1 : index + 3])
            ):
                index += 3
            else:
                index += 1
            octets += 1
        return octets

    @staticmethod
    def _normalize_octets(value: str, *, pattern: bool) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise RobotsError("robots path contains control characters")
        normalized: list[str] = []
        index = 0
        while index < len(value):
            character = value[index]
            if character == "%" and index + 2 < len(value):
                escape = value[index + 1 : index + 3]
                if _PERCENT_ESCAPE.fullmatch(escape):
                    octet = int(escape, 16)
                    if octet in _UNRESERVED:
                        normalized.append(chr(octet))
                    else:
                        normalized.append(f"%{escape.upper()}")
                    index += 3
                    continue
            if ord(character) > 127:
                normalized.extend(f"%{octet:02X}" for octet in character.encode("utf-8"))
            elif pattern and character in {"*", "$"}:
                normalized.append(character)
            else:
                normalized.append(character)
            index += 1
        return "".join(normalized)
