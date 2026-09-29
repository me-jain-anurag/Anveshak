"""Scoring policy (data/scoring_policy.yaml) — typed, validated, hashed."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, field_validator

from .domain import Frozen, RiskFlag, SourceClass
from .evidence import canonical_json, sha256_hex


class Band(Frozen):
    min: int = Field(ge=0, le=100)
    band: str


class Level(Frozen):
    min: int = Field(ge=0, le=100)
    level: str


class AttributionPoints(Frozen):
    entity_attested: int
    authority: int
    curated_first: int
    curated_each_additional: int
    curated_cap: int
    weak_only: int
    derived_penalty: int
    derived_cap: int


class PathPoints(Frozen):
    all_verified: int
    some_unverifiable: int
    some_error: int


class ProximityPoints(Frozen):
    by_hops: dict[int, int]
    beyond: int


class CorroborationPoints(Frozen):
    deposit_sweep_pattern: int


class ConfidencePolicy(Frozen):
    attribution: AttributionPoints
    path: PathPoints
    proximity: ProximityPoints
    corroboration: CorroborationPoints
    bands: tuple[Band, ...]

    @field_validator("bands")
    @classmethod
    def _ordered(cls, v: tuple[Band, ...]) -> tuple[Band, ...]:
        if not v or v[-1].min != 0 or list(v) != sorted(v, key=lambda b: -b.min):
            raise ValueError("bands must be ordered high→low and end with min 0")
        return v

    def band(self, score: int) -> str:
        return next(b.band for b in self.bands if score >= b.min)


class RiskPolicy(Frozen):
    flag_points: dict[RiskFlag, int]
    flag_source_percent: dict[SourceClass, int]
    exposure_percent_by_hops: dict[str, int]
    service_points: dict[str, int]
    typology_points: dict[str, int]
    typology_cap: int
    levels: tuple[Level, ...]

    def level(self, score: int) -> str:
        return next(l.level for l in self.levels if score >= l.min)

    def exposure_percent(self, hops: int) -> int:
        return self.exposure_percent_by_hops.get(str(hops), self.exposure_percent_by_hops["beyond"])


class RapidPassThrough(Frozen):
    max_minutes: int
    min_forward_percent: int


class FanOut(Frozen):
    min_recipients: int
    window_hours: int


class FanIn(Frozen):
    min_senders: int
    window_hours: int


class PeelChain(Frozen):
    min_steps: int
    min_keep_percent: int


class TypologyPolicy(Frozen):
    rapid_pass_through: RapidPassThrough
    fan_out: FanOut
    fan_in: FanIn
    peel_chain: PeelChain


class RoutingThresholds(Frozen):
    ready_min_confidence: int = Field(ge=0, le=100)
    freeze_min_confidence: int = Field(ge=0, le=100)


class ScoringPolicy(Frozen):
    version: str
    confidence: ConfidencePolicy
    risk: RiskPolicy
    routing: RoutingThresholds
    typologies: TypologyPolicy

    @classmethod
    def load(cls, path: Path) -> ScoringPolicy:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        doc["risk"]["exposure_percent_by_hops"] = {str(k): v for k, v in doc["risk"]["exposure_percent_by_hops"].items()}
        return cls.model_validate(doc)

    @property
    def snapshot(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))
