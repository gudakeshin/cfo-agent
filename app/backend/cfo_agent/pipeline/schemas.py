"""Step schemas and shared types for the reasoning pipeline."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Connection(BaseModel):
    signal_ids: list[str] = Field(description="Signal IDs involved")
    relationship_ids: list[str] = Field(default_factory=list)
    narrative: str
    kra_ids: list[str] = Field(default_factory=list)


class ConnectOutput(BaseModel):
    connections: list[Connection]


class SectionBlock(BaseModel):
    text: str
    citations: list[str] = Field(default_factory=list)


class SynthesizeItemOutput(BaseModel):
    title: str
    what: SectionBlock
    why: SectionBlock
    connects: SectionBlock
    action: SectionBlock


class HeadlineOutput(BaseModel):
    headline: str
