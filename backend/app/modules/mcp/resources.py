"""MCP resources and prompts of a hosted site (06 "/mcp/{slug}"), beside its eight tools.

Resources: the overview, each guide page, and any source file through a template. Their URIs name the
site (`codehover://<slug>/...`), so a client with several codehover servers never mixes them up; an
optional `?language=<code>` reads an approved language variant. Contents are the same Markdown the tools
return, through the same functions, so a resource never shows more than the site does.

Prompts are fixed instructions that start common tasks with the tools. Argument values are the user's
own text, passed back to their client; the server runs nothing.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import ObjectStore
from app.core.errors import ApiError
from app.engine.prompts import LANGUAGE_PATTERN
from app.modules.mcp import tools
from app.modules.projects import service
from app.modules.published.router import Site

SCHEME = "codehover"
MARKDOWN = "text/markdown"
GUIDE_SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")


class NotFound(Exception):
    """A resource URI this site does not have (JSON-RPC -32002)."""


def _uri(slug: str, path: str) -> str:
    return f"{SCHEME}://{slug}/{path}"


async def listing(db: AsyncSession, objects: ObjectStore, site: Site, slug: str) -> list[dict[str, Any]]:
    ctx = await tools.context_for(db, objects, site, None)
    await ctx.ready()
    index = await service.guide_index(db, objects, site.reader, ctx.build_id)
    found = [
        {
            "uri": _uri(slug, "overview"),
            "name": "overview",
            "title": "Overview",
            "description": "What the repository is, its key concepts and a reading order.",
            "mimeType": MARKDOWN,
        }
    ]
    for page in index["pages"]:
        if page.get("status") not in (None, "written"):
            continue
        found.append(
            {
                "uri": _uri(slug, f"guide/{page['slug']}"),
                "name": f"guide/{page['slug']}",
                "title": page["title"],
                "description": page.get("purpose") or "A guide page.",
                "mimeType": MARKDOWN,
            }
        )
    return found


def templates(slug: str) -> list[dict[str, Any]]:
    return [
        {
            "uriTemplate": _uri(slug, "files/{path}"),
            "name": "file",
            "title": "Source file",
            "description": "A source file's explanation, definitions and first lines; `path` is"
            " repository-relative, with `/` written as %2F or as is.",
            "mimeType": MARKDOWN,
        }
    ]


def _parse(uri: str, slug: str) -> tuple[str, str, str | None]:
    """(kind, name, language) of a URI of this site; NotFound otherwise."""
    parts = urlsplit(uri)
    if parts.scheme != SCHEME or parts.netloc != slug or parts.fragment:
        raise NotFound(uri)
    query = parse_qs(parts.query, max_num_fields=4)
    languages = query.get("language", [])
    language = languages[0] if languages else None
    if language is not None and not re.fullmatch(LANGUAGE_PATTERN, language):
        raise NotFound(uri)
    path = unquote(parts.path.lstrip("/"))
    if path == "overview":
        return "overview", "", language
    if path.startswith("guide/") and GUIDE_SLUG.fullmatch(path.removeprefix("guide/")):
        return "guide", path.removeprefix("guide/"), language
    if path.startswith("files/") and 0 < len(path) - len("files/") <= 1024:
        return "file", path.removeprefix("files/"), language
    raise NotFound(uri)


async def read(db: AsyncSession, objects: ObjectStore, site: Site, slug: str, uri: str) -> dict[str, Any]:
    """`resources/read` contents. NotFound for a URI the site does not have."""
    kind, name, language = _parse(uri, slug)
    try:
        ctx = await tools.context_for(db, objects, site, language)
        if kind == "overview":
            body = await tools.overview(ctx, tools.NoArgs())
        elif kind == "guide":
            body = await tools.read_guide_page(ctx, tools.GuideArgs(slug=name))
        else:
            summary = await tools.get_file(ctx, tools.PathArgs(path=name))
            lines = await tools.read_lines(ctx, tools.LinesArgs(path=name))
            body = f"{summary}\n\n{lines}"
    except (tools.ToolError, ValidationError) as error:
        raise NotFound(uri) from error
    except ApiError as error:
        if error.status_code in (404, 409):
            raise NotFound(uri) from error
        raise
    return {"contents": [{"uri": uri, "mimeType": MARKDOWN, "text": tools._clip(body)}]}


# ---------------------------------------------------------------- prompts
PROMPTS: dict[str, dict[str, Any]] = {
    "tour": {
        "title": "Tour the repository",
        "description": "A guided tour: what the repository does, how it is organized, where to start.",
        "arguments": [],
        "text": (
            "Give me a guided tour of this repository using the codehover tools of this server. Start with"
            " overview(), then list_guide(), and read the guide pages that explain the overall design. Tell"
            " me what the repository does, how it is organized, and which files to read first, citing file"
            " paths and line numbers. Check important claims against the source with read_lines()."
        ),
    },
    "explain": {
        "title": "Explain a definition or file",
        "description": "How one definition or file works and who uses it.",
        "arguments": [
            {"name": "target", "description": "a definition's name or a file path", "required": True}
        ],
        "text": (
            "Explain {target} in this repository using the codehover tools of this server. Find it with"
            " search() if needed, read it with get_definition() (or get_file() for a file), see who uses it"
            " with find_references(), and confirm details with read_lines(). Explain what it does, how, and"
            " why it matters to its callers, citing file paths and line numbers."
        ),
    },
    "where": {
        "title": "Find where something happens",
        "description": "Locate the code responsible for a behavior.",
        "arguments": [
            {"name": "question", "description": "what you are looking for, in your words", "required": True}
        ],
        "text": (
            "Using the codehover tools of this server, find where this happens in the repository: {question}"
            "\n\nSearch with search() using a few different terms, read candidates with get_definition() and"
            " read_lines(), and answer with the file paths and line ranges responsible, plus a short"
            " explanation of each."
        ),
    },
}
MAX_ARGUMENT_CHARS = 2000


def prompt_listing() -> list[dict[str, Any]]:
    return [
        {"name": name, "title": p["title"], "description": p["description"], "arguments": p["arguments"]}
        for name, p in PROMPTS.items()
    ]


class InvalidPrompt(Exception):
    pass


def prompt(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """`prompts/get` result. KeyError for an unknown prompt, InvalidPrompt for missing arguments."""
    entry = PROMPTS[name]
    values: dict[str, str] = {}
    for argument in entry["arguments"]:
        value = arguments.get(argument["name"])
        if not isinstance(value, str) or not value.strip():
            if argument["required"]:
                raise InvalidPrompt(f"missing argument: {argument['name']}")
            value = ""
        values[argument["name"]] = value.strip()[:MAX_ARGUMENT_CHARS]
    text = entry["text"]
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return {
        "description": entry["description"],
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }
