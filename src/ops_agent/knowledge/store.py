"""Loads the markdown knowledge base, chunks it by section, and answers search queries with citations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from ops_agent.knowledge.bm25 import BM25Index

_HEADING = re.compile(r"^(#{2,3})\s+(.*)$", re.MULTILINE)


class DocSection(BaseModel):
    heading: str
    anchor: str
    text: str


class Document(BaseModel):
    id: str
    title: str
    type: str
    owner: str
    trust: str
    tags: list[str]
    sections: list[DocSection]
    content: str


class DocHit(BaseModel):
    doc_id: str
    title: str
    section: str
    citation: str
    snippet: str
    score: float
    trust: str


@dataclass(frozen=True, slots=True)
class _Chunk:
    doc_id: str
    section: DocSection


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "intro"


def parse_markdown(raw: str) -> Document:
    if not raw.startswith("---\n"):
        raise ValueError("document is missing front matter")
    header, body = raw[4:].split("\n---\n", 1)
    meta: dict[str, str] = {}
    for line in header.splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    body = body.strip()
    # Drop the H1 title line; split into H2/H3 sections.
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        body = "\n".join(lines[1:]).strip()
    sections: list[DocSection] = []
    matches = list(_HEADING.finditer(body))
    if not matches or matches[0].start() > 0:
        intro = body[: matches[0].start()] if matches else body
        if intro.strip():
            sections.append(DocSection(heading="Overview", anchor="overview", text=intro.strip()))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[m.end() : end].strip()
        sections.append(DocSection(heading=m.group(2).strip(), anchor=_slug(m.group(2)), text=text))
    return Document(
        id=meta["id"],
        title=meta["title"],
        type=meta.get("type", "unknown"),
        owner=meta.get("owner", "unknown"),
        trust=meta.get("trust", "unverified"),
        tags=[t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
        sections=sections,
        content=body,
    )


class DocStore:
    def __init__(self, documents: list[Document]) -> None:
        self._docs = {d.id: d for d in sorted(documents, key=lambda d: d.id)}
        self._chunks = [_Chunk(d.id, s) for d in self._docs.values() for s in d.sections]
        self._index = BM25Index(
            [f"{self._docs[c.doc_id].title} {c.section.heading} {c.section.text}" for c in self._chunks]
        )

    @classmethod
    def from_dir(cls, path: Path) -> DocStore:
        files = sorted(path.glob("*.md"))
        if not files:
            raise FileNotFoundError(f"no markdown documents in {path}; run `make seed` first")
        return cls([parse_markdown(f.read_text()) for f in files])

    def __len__(self) -> int:
        return len(self._docs)

    def get(self, doc_id: str) -> Document | None:
        return self._docs.get(doc_id)

    def search(self, query: str, limit: int = 5, include_unverified: bool = True) -> list[DocHit]:
        hits: list[DocHit] = []
        for idx, score in self._index.top_k(query, k=len(self._chunks)):
            chunk = self._chunks[idx]
            doc = self._docs[chunk.doc_id]
            if not include_unverified and doc.trust != "verified":
                continue
            snippet = " ".join(chunk.section.text.split())
            hits.append(
                DocHit(
                    doc_id=doc.id,
                    title=doc.title,
                    section=chunk.section.heading,
                    citation=f"{doc.id}#{chunk.section.anchor}",
                    snippet=snippet[:400] + ("..." if len(snippet) > 400 else ""),
                    score=round(score, 4),
                    trust=doc.trust,
                )
            )
            if len(hits) >= limit:
                break
        return hits
