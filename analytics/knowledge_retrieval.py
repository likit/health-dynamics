from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
GUIDANCE_PATH = BASE_DIR / "knowledge" / "ncd_guidance.md"

KEYWORD_MAP = {
    "glucose": ["glucose"],
    "hba1c": ["hba1c"],
    "bmi": ["bmi"],
    "ldl": ["ldl"],
    "hdl": ["hdl"],
    "triglyceride": ["triglyceride", "triglycerides", "tg"],
    "cholesterol": ["cholesterol", "chol"],
    "blood pressure": ["blood pressure", "bp"],
    "liver enzymes": ["liver enzymes", "ast", "alt", "alk"],
    "kidney markers": ["kidney markers", "cre", "creatinine", "bun"],
    "uric acid": ["uric acid", "uric"],
    "metabolic syndrome": ["metabolic syndrome", "multiple abnormal markers"],
    "persistent abnormal status": ["persistent abnormal", "abnormal_to_abnormal"],
    "progressive deterioration": ["progressive deterioration", "worsening", "normal_to_abnormal", "borderline_to_abnormal"],
    "recovery pattern": ["recovery", "improving", "abnormal_to_normal", "abnormal_to_borderline"],
    "department-level intervention": ["department", "intervention", "worsening department"],
}


@dataclass
class GuidanceNote:
    topic: str
    text: str


def _load_guidance_sections() -> dict[str, str]:
    if not GUIDANCE_PATH.exists():
        return {}

    sections: dict[str, str] = {}
    current_heading: str | None = None
    current_lines: list[str] = []

    for raw_line in GUIDANCE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if line.startswith("# "):
            if current_heading is not None:
                sections[current_heading.lower()] = " ".join(part.strip() for part in current_lines if part.strip()).strip()
            current_heading = line[2:].strip()
            current_lines = []
            continue
        current_lines.append(line)

    if current_heading is not None:
        sections[current_heading.lower()] = " ".join(part.strip() for part in current_lines if part.strip()).strip()

    return sections


def retrieve_guidance(*keywords: str, limit: int = 3) -> list[GuidanceNote]:
    sections = _load_guidance_sections()
    if not sections:
        return []

    matched_topics: list[str] = []
    lowered_keywords = [keyword.lower() for keyword in keywords if keyword]

    for topic, aliases in KEYWORD_MAP.items():
        if any(alias in keyword for alias in aliases for keyword in lowered_keywords):
            matched_topics.append(topic)

    notes: list[GuidanceNote] = []
    seen: set[str] = set()
    for topic in matched_topics:
        if topic in seen:
            continue
        section_text = sections.get(topic)
        if not section_text:
            continue
        notes.append(GuidanceNote(topic=topic.title(), text=section_text))
        seen.add(topic)
        if len(notes) >= limit:
            break

    return notes
