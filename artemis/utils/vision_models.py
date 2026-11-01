"""Strict, model-supplied fields for MiniMax visual recognition."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from artemis.utils.coordinates import validate_bbox


class VisualRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, strict=True)
    bbox: list[float] = Field(min_length=4, max_length=4)

    @field_validator("bbox", mode="before")
    @classmethod
    def _bbox(cls, value):
        return validate_bbox(value)


class DetectionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["found", "ambiguous", "not_found"]
    candidates: list[VisualRegion]

    @model_validator(mode="after")
    def _cardinality(self):
        count = len(self.candidates)
        if (
            (self.status == "found" and count != 1)
            or (self.status == "ambiguous" and count < 2)
            or (self.status == "not_found" and count != 0)
        ):
            raise ValueError("found requires one candidate; ambiguous at least two; not_found none")
        return self


class OCRRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, strict=True)
    bbox: list[float] = Field(min_length=4, max_length=4)
    uncertain: bool = Field(default=False, strict=True)

    @field_validator("bbox", mode="before")
    @classmethod
    def _bbox(cls, value):
        return validate_bbox(value)


class OCRModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["complete", "partial", "empty"]
    regions: list[OCRRegion]

    @model_validator(mode="after")
    def _status(self):
        if self.status == "empty" and self.regions:
            raise ValueError("empty requires no regions")
        if self.status == "complete" and not self.regions:
            raise ValueError("use empty for a confirmed empty image")
        if self.status == "complete" and any(r.uncertain for r in self.regions):
            raise ValueError("uncertain characters require partial status")
        return self
