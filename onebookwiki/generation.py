"""Resumable, evidence-grounded chapter and book generation."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .checkpoints import CheckpointStore, digest, project_lock
from .chunking import count_tokens
from .citations import validate_evidence
from .index import LocalIndex
from .ledger import append_usage
from .models import BookSynthesis, ChapterInterpretation, EvidenceRef, Rollup, book_from_dict, chapter_from_dict, rollup_from_dict, to_dict
from .prompts import book_prompt, chapter_prompt, rollup_prompt
from .providers import GenerationResponse, ProviderUnavailable, generate_response


class GenerationError(RuntimeError):
    pass


ProgressCallback = Callable[[str], None]


@dataclass
class GenerationOptions:
    provider: str = "none"
    model: str | None = None
    language: str = "zh-CN"
    max_input_tokens: int = 12000
    max_output_tokens: int = 1800
    rollup_size: int = 4
    retries: int = 1
    run_id: str | None = None
    input_rate: float | None = None
    output_rate: float | None = None
    dry_run: bool = False
    progress: ProgressCallback | None = None


def _progress(options: GenerationOptions, message: str) -> None:
    if options.progress is not None:
        options.progress(message)


def _clean_response(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    if start >= 0:
        end = cleaned.rfind("}")
        return cleaned[start : end + 1] if end >= start else cleaned[start:]
    return cleaned


def _strict_json_from_text(text: str) -> dict[str, Any]:
    cleaned = _clean_response(text)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GenerationError(f"LLM response contains invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise GenerationError("LLM response must be a JSON object")
    return value


def _escape_embedded_quotes(text: str) -> str:
    """Escape quotes inside values when the following token is not a JSON delimiter."""
    output: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            output.append(char)
            escaped = False
            continue
        if char == "\\" and in_string:
            output.append(char)
            escaped = True
            continue
        if char != '"':
            output.append(char)
            continue
        if not in_string:
            in_string = True
            output.append(char)
            continue
        lookahead = index + 1
        while lookahead < len(text) and text[lookahead].isspace():
            lookahead += 1
        if lookahead < len(text) and text[lookahead] not in ',:}]"':
            output.append('\\"')
        else:
            in_string = False
            output.append(char)
    return "".join(output)


def _stdlib_repair_json(text: str) -> str:
    """Repair common comma and unescaped-quote defects without accepting prose."""
    cleaned = _escape_embedded_quotes(_clean_response(text))
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    # Most malformed structured responses omit the comma before the next object key.
    return re.sub(r'([}\]"0-9]|true|false|null)\s*(?="[^"\\]*(?:\\.[^"\\]*)*"\s*:)', r"\1,", cleaned, flags=re.IGNORECASE)


def _repaired_json_from_text(text: str) -> dict[str, Any]:
    candidates: list[Any] = []
    try:
        from json_repair import repair_json
    except ImportError:
        pass
    else:
        try:
            candidates.append(repair_json(_clean_response(text)))
        except Exception:  # noqa: BLE001 - optional parser boundary
            pass
    candidates.append(_stdlib_repair_json(text))
    last_error: Exception | None = None
    for repaired in candidates:
        try:
            value = json.loads(repaired) if isinstance(repaired, str) else repaired
        except (TypeError, json.JSONDecodeError) as exc:
            last_error = exc
            continue
        if isinstance(value, dict):
            return value
        last_error = GenerationError("LLM response after repair must be a JSON object")
    if isinstance(last_error, json.JSONDecodeError):
        raise GenerationError(f"LLM response contains invalid JSON after repair: {last_error.msg}") from last_error
    raise GenerationError(str(last_error or "JSON repair produced no result"))


def _parse_json_response(text: str) -> tuple[dict[str, Any], bool, str | None]:
    try:
        return _strict_json_from_text(text), False, None
    except GenerationError as strict_error:
        try:
            return _repaired_json_from_text(text), True, str(strict_error)
        except GenerationError as repair_error:
            raise GenerationError(f"{strict_error}; repair unsuccessful: {repair_error}") from repair_error



def _chunks_by_chapter(root: Path) -> dict[int, list[dict]]:
    chunks = LocalIndex(root).load()
    result: dict[int, list[dict]] = {}
    for chunk in chunks:
        result.setdefault(int(chunk.get("chapter", 0)), []).append(chunk)
    for values in result.values():
        values.sort(key=lambda item: (int(item.get("start_line", 0)), str(item.get("chunk_id", ""))))
    return result


def _bounded(chunks: list[dict], budget: int) -> list[dict]:
    selected: list[dict] = []
    used = 0
    for chunk in chunks:
        size = int(chunk.get("token_count") or count_tokens(str(chunk.get("text", ""))))
        if selected and used + size > budget:
            break
        if not selected and size > budget:
            text = str(chunk.get("text", ""))
            while len(text) > 1 and count_tokens(text) > budget:
                text = text[: max(1, len(text) // 2)]
            copied = dict(chunk)
            copied["text"] = text
            selected.append(copied)
            break
        selected.append(chunk)
        used += size
    return selected


def _response(root: Path, prompt: str, options: GenerationOptions, store: CheckpointStore, node_id: str, stage: str) -> GenerationResponse:
    error: Exception | None = None
    for attempt in range(1, options.retries + 2):
        store.mark(node_id, "running", stage=stage, prompt_hash=digest(prompt), model_hash=digest({"provider": options.provider, "model": options.model, "language": options.language}))
        _progress(options, f"{node_id}: request attempt {attempt}/{options.retries + 1}")
        try:
            response = generate_response(prompt, options.provider, options.model, options.max_output_tokens)
            append_usage(root, run_id=store.run_id, node_id=node_id, stage=stage, attempt=attempt, provider=options.provider, model=response.model or options.model or "unknown", usage=response.usage, estimated=response.estimated_usage, input_rate=options.input_rate, output_rate=options.output_rate, prompt=prompt)
            total = (response.usage or {}).get("total_tokens", "?")
            estimate = " estimated" if response.estimated_usage else ""
            _progress(options, f"{node_id}: response received ({total} tokens{estimate})")
            return response
        except Exception as exc:  # noqa: BLE001 - retries are part of the run contract
            error = exc
            detail = " ".join(str(exc).strip().split()) or "no additional message"
            _progress(options, f"{node_id}: request failed ({type(exc).__name__}: {detail})")
            append_usage(root, run_id=store.run_id, node_id=node_id, stage=stage, attempt=attempt, provider=options.provider, model=options.model or "unknown", status="failed", input_rate=options.input_rate, output_rate=options.output_rate, prompt=prompt, error_type=type(exc).__name__, error_detail=detail)
    message = str(error).strip() if error is not None else "LLM generation request failed"
    store.mark(node_id, "failed", error=message)
    _progress(options, f"{node_id}: all request attempts failed; checkpoint saved")
    raise GenerationError(message)


def _parse_node_response(response: GenerationResponse, store: CheckpointStore, node_id: str, options: GenerationOptions) -> dict[str, Any]:
    try:
        value, repaired, strict_error = _parse_json_response(response.text)
    except GenerationError as error:
        preview = " ".join(response.text.strip().split())[:240]
        message = f"{error}; response length={len(response.text)}, preview={preview!r}"
        store.mark(node_id, "failed", error=message)
        _progress(options, f"{node_id}: failed while parsing JSON; checkpoint saved")
        raise GenerationError(message) from error
    if repaired:
        store.node(node_id).update(repaired_json=True, strict_parse_error=strict_error)
        store.save()
        _progress(options, f"{node_id}: repaired malformed JSON")
    else:
        _progress(options, f"{node_id}: parsed strict JSON")
    return value


def _refs_for(chunks: list[dict], chapter: int) -> list[EvidenceRef]:
    return [
        EvidenceRef(
            evidence_id=f"C{chapter}E{index}",
            chunk_id=str(chunk.get("chunk_id", "")),
            source_path=str(chunk.get("source_path", "")),
            chapter=int(chunk.get("chapter", 0)),
            start_line=int(chunk.get("start_line", 0)),
            end_line=int(chunk.get("end_line", 0)),
            quote="",
            locator=dict(chunk.get("locator") or {}),
        )
        for index, chunk in enumerate(chunks, 1)
    ]


def _claim_ids(value: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for key in ("evidence_examples", "important_quotations", "cross_chapter_connections", "claims", "themes", "concepts", "arguments", "tensions"):
        for item in value.get(key, []) or []:
            if isinstance(item, dict):
                result.update(str(entry) for entry in item.get("evidence_ids", []) or [])
    return result


def generate_chapters(root: Path, options: GenerationOptions | None = None, chapters: list[int] | None = None) -> CheckpointStore:
    options = options or GenerationOptions()
    store = CheckpointStore(root, options.run_id)
    by_chapter = _chunks_by_chapter(root)
    selected_numbers = chapters or sorted(by_chapter)
    if options.dry_run:
        for number in selected_numbers:
            chunks = _bounded(by_chapter[number], options.max_input_tokens)
            node_id = f"chapter:{number}"
            store.node(node_id).update(status="pending", input_tokens=sum(int(c.get("token_count", 0)) for c in chunks))
        store.save()
        _progress(options, f"generation plan: {len(selected_numbers)} chapter node(s) pending")
        return store
    _progress(options, f"chapter generation: {len(selected_numbers)} chapter(s) selected")
    with project_lock(root):
        for position, number in enumerate(selected_numbers, 1):
            chunks = _bounded(by_chapter[number], options.max_input_tokens)
            if not chunks:
                continue
            title = f"Chapter {number}"
            raw = root / str(chunks[0].get("source_path", ""))
            if raw.is_file():
                first = raw.read_text(encoding="utf-8").splitlines()
                if first and first[0].startswith("# "):
                    title = first[0][2:].strip().removeprefix(f"Chapter {number}: ").strip() or title
            prompt = chapter_prompt(number, title, chunks, options.language)
            node_id = f"chapter:{number}"
            input_hash = digest([(c.get("chunk_id"), c.get("content_hash")) for c in chunks])
            model_hash = digest({"provider": options.provider, "model": options.model, "language": options.language, "version": 1})
            artifact = root / ".onebookwiki" / "artifacts" / "chapters" / f"{number:04d}.json"
            if store.reusable(node_id, input_hash, digest(prompt), model_hash, artifact):
                _progress(options, f"chapter {position}/{len(selected_numbers)} ({number}): reused existing artifact")
                continue
            _progress(options, f"chapter {position}/{len(selected_numbers)} ({number}): generating ({len(chunks)} evidence chunk(s))")
            response = _response(root, prompt, options, store, node_id, "chapter")
            value = _parse_node_response(response, store, node_id, options)
            ids = _claim_ids(value)
            valid_ids = {f"C{number}E{index}" for index in range(1, len(chunks) + 1)}
            unknown = ids - valid_ids
            if unknown:
                store.mark(node_id, "failed", error=f"unknown evidence ids: {sorted(unknown)}")
                _progress(options, f"chapter {position}/{len(selected_numbers)} ({number}): failed evidence validation")
                raise GenerationError(f"chapter {number} contains unknown evidence ids: {sorted(unknown)}")
            refs = _refs_for(chunks, number)
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(json.dumps({**value, "chapter": number, "title": title, "evidence": to_dict(refs), "source_fingerprint": input_hash, "generator_fingerprint": model_hash}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
            store.mark(node_id, "completed", input_hash=input_hash, prompt_hash=digest(prompt), model_hash=model_hash, artifact_path=str(artifact.relative_to(root)), artifact_hash=artifact_hash)
            _progress(options, f"chapter {position}/{len(selected_numbers)} ({number}): completed")
    return store


def _load_chapters(root: Path) -> list[ChapterInterpretation]:
    directory = root / ".onebookwiki" / "artifacts" / "chapters"
    result = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        result.append(chapter_from_dict(json.loads(path.read_text(encoding="utf-8"))))
    return result


def synthesize_book(root: Path, options: GenerationOptions | None = None) -> CheckpointStore:
    options = options or GenerationOptions()
    store = CheckpointStore(root, options.run_id)
    chapters = _load_chapters(root)
    if not chapters and options.dry_run:
        by_chapter = _chunks_by_chapter(root)
        chapters = [
            ChapterInterpretation(
                chapter=number,
                title=f"Chapter {number}",
                executive_summary="pending generation",
                core_thesis="pending generation",
                evidence=[
                    EvidenceRef(
                        evidence_id=f"C{number}E{index}",
                        chunk_id=str(chunk.get("chunk_id", "")),
                        source_path=str(chunk.get("source_path", "")),
                        chapter=number,
                        start_line=int(chunk.get("start_line", 0)),
                        end_line=int(chunk.get("end_line", 0)),
                        locator=dict(chunk.get("locator") or {}),
                    )
                    for index, chunk in enumerate(_bounded(values, options.max_input_tokens), 1)
                ],
            )
            for number, values in sorted(by_chapter.items())
        ]
    if not chapters:
        raise GenerationError("no chapter artifacts; generate chapters first")
    cards = [{"chapter": item.chapter, "title": item.title, "summary": item.executive_summary, "thesis": item.core_thesis, "claims": to_dict(item.claims)} for item in chapters]
    chunks_by_chapter = _chunks_by_chapter(root)
    rollups: list[Rollup] = []
    for start in range(0, len(cards), max(1, options.rollup_size)):
        group = cards[start : start + max(1, options.rollup_size)]
        node_id = f"rollup:{start + 1}-{start + len(group)}"
        prompt = rollup_prompt(group, options.language)
        input_hash = digest(group)
        model_hash = digest({"provider": options.provider, "model": options.model, "language": options.language, "version": 1})
        artifact = root / ".onebookwiki" / "artifacts" / "rollups" / f"{start + 1:04d}-{start + len(group):04d}.json"
        if not options.dry_run:
            if not store.reusable(node_id, input_hash, digest(prompt), model_hash, artifact):
                _progress(options, f"{node_id}: generating ({len(group)} chapter card(s))")
                response = _response(root, prompt, options, store, node_id, "rollup")
                value = _parse_node_response(response, store, node_id, options)
                ids = _claim_ids(value)
                valid = {
                    f"C{item['chapter']}E{index}"
                    for item in group
                    for index, _ in enumerate(_bounded(chunks_by_chapter[item["chapter"]], options.max_input_tokens), 1)
                }
                unknown = ids - valid
                if unknown:
                    unknown_values = sorted(unknown)
                    store.mark(node_id, "failed", error=f"unknown evidence ids: {unknown_values}")
                    _progress(options, f"{node_id}: failed evidence validation")
                    raise GenerationError(f"rollup contains unknown evidence ids: {unknown_values}; allowed ids include {sorted(valid)[:12]}")
                value.update(node_id=node_id, chapters=[item["chapter"] for item in group], evidence=[])
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                store.mark(node_id, "completed", input_hash=input_hash, prompt_hash=digest(prompt), model_hash=model_hash, artifact_path=str(artifact.relative_to(root)), artifact_hash=hashlib.sha256(artifact.read_bytes()).hexdigest(), dependencies=[f"chapter:{item['chapter']}" for item in group])
                _progress(options, f"{node_id}: completed")
            else:
                _progress(options, f"{node_id}: reused existing artifact")
        elif not store.node(node_id).get("status"):
            store.node(node_id).update(status="pending", input_tokens=count_tokens(prompt))
        if artifact.is_file():
            rollups.append(rollup_from_dict(json.loads(artifact.read_text(encoding="utf-8"))))
    title = "Untitled book"
    source = root / ".onebookwiki" / "source.json"
    if source.is_file():
        try:
            title = str(json.loads(source.read_text(encoding="utf-8")).get("title") or title)
        except (OSError, ValueError):
            pass
    prompt = book_prompt([to_dict(item) for item in rollups], cards, title, options.language)
    node_id = "book:synthesis"
    artifact = root / ".onebookwiki" / "artifacts" / "book.json"
    input_hash = digest([to_dict(item) for item in rollups] + cards)
    model_hash = digest({"provider": options.provider, "model": options.model, "language": options.language, "version": 1})
    if not options.dry_run and not store.reusable(node_id, input_hash, digest(prompt), model_hash, artifact):
        _progress(options, "book synthesis: generating")
        response = _response(root, prompt, options, store, node_id, "book")
        value = _parse_node_response(response, store, node_id, options)
        value.update(title=title, evidence=[])
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        store.mark(node_id, "completed", input_hash=input_hash, prompt_hash=digest(prompt), model_hash=model_hash, artifact_path=str(artifact.relative_to(root)), artifact_hash=hashlib.sha256(artifact.read_bytes()).hexdigest(), dependencies=[item.node_id for item in rollups])
        _progress(options, "book synthesis: completed")
    elif not options.dry_run:
        _progress(options, "book synthesis: reused existing artifact")
    elif options.dry_run:
        store.node(node_id).update(status="pending", input_tokens=count_tokens(prompt))
    store.save()
    return store


def generate_wiki(root: Path, options: GenerationOptions | None = None) -> CheckpointStore:
    options = options or GenerationOptions()
    store = generate_chapters(root, options)
    return synthesize_book(root, GenerationOptions(**{**options.__dict__, "run_id": store.run_id}))


def resume_generation(root: Path, options: GenerationOptions | None = None, run_id: str | None = None) -> CheckpointStore:
    options = options or GenerationOptions()
    latest = run_id or (CheckpointStore.latest(root).run_id if CheckpointStore.latest(root) else None)
    return generate_wiki(root, GenerationOptions(**{**options.__dict__, "run_id": latest}))
