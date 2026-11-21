"""Portable trajectory files; robot-specific limits remain server-side."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .json_wire import loads

JointName = Annotated[str, Field(min_length=1, max_length=64)]


class Keyframe(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    time: float = Field(gt=0, le=600)
    positions: dict[JointName, float] = Field(min_length=1, max_length=32)
    name: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def optional_name(self) -> Keyframe:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Omit name instead of using null")
        return self


class Trajectory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    schema_version: Literal["1.0"]
    interpolation: Literal["native"]
    keyframes: list[Keyframe] = Field(min_length=1, max_length=1000)
    name: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def ordered(self) -> Trajectory:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Omit name instead of using null")
        if any(
            right.time <= left.time
            for left, right in zip(self.keyframes, self.keyframes[1:], strict=False)
        ):
            raise ValueError("Keyframe times must increase strictly")
        return self

    @classmethod
    def from_json(cls, content: str | bytes) -> Trajectory:
        return cls.model_validate(loads(content))

    def to_json(self) -> str:
        return self.model_dump_json(indent=2, exclude_none=True) + "\n"
