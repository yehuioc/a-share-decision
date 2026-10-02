"""Read-only adapter from the local Caixin archive into frozen evidence versions."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from .store import ConflictError, DomainError, Store, now_iso, parse_time, stable_id


DEFAULT_CAIXIN_ROOT = Path(
    r"data/sources/caixin"
)
DEFAULT_CAIXIN_RAW_ROOT = Path(
    r"data/sources/caixin-raw"
)
ADAPTER_VERSION = "caixin-evidence-v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _safe_relative(path_value, root: Path) -> tuple[str | None, Path | None]:
    value = str(path_value or "").replace("\\", "/").strip()
    if not value or Path(value).is_absolute():
        return value or None, None
    candidate = (root / Path(value)).resolve()
    if not candidate.is_relative_to(root):
        return value, None
    return value, candidate


def _body_readable(text: str) -> bool:
    if not text.strip():
        return False
    letters = [char for char in text if char.isalpha()]
    suspicious = sum(
        "\u0d80" <= char <= "\u0fff"
        or "\ue000" <= char <= "\uf8ff"
        or char == "\ufffd"
        for char in text
    )
    return not (suspicious >= 12 and suspicious / max(1, len(letters)) > 0.15)


def _paragraph_locations(text: str) -> list[dict]:
    locations = []
    for match in re.finditer(r"\S[\s\S]*?(?=(?:\r?\n){2,}|\Z)", text):
        start, end = match.span()
        locations.append(
            {
                "paragraph": len(locations) + 1,
                "start": start,
                "end": end,
                "start_line": text.count("\n", 0, start) + 1,
                "end_line": text.count("\n", 0, end) + 1,
            }
        )
    return locations


def _published_precision(value) -> str:
    if not value:
        return "unknown"
    raw = str(value).strip()
    if re.search(r"T00:00:00(?:\.0+)?(?:Z|\+00:00)$", raw):
        return "date"
    return "datetime"


def _issue(metadata: dict, document: dict) -> tuple[dict, str]:
    source = document.get("issue") if isinstance(document.get("issue"), dict) else {}
    issue = {
        "year": source.get("year", metadata.get("issue_year")),
        "annual_issue": source.get("annual_issue", metadata.get("annual_issue")),
        "total_issue": source.get("total_issue", metadata.get("total_issue")),
        "publication_date": source.get(
            "publication_date", metadata.get("publication_date")
        ),
        "official_issue_url": source.get(
            "official_issue_url", metadata.get("official_issue_url")
        ),
    }
    year = issue.get("year")
    annual = issue.get("annual_issue")
    total = issue.get("total_issue")
    if year is not None and annual is not None:
        label = f"{year}年第{annual}期"
        if total is not None:
            label += f"（总第{total}期）"
    elif total is not None:
        label = f"总第{total}期"
    else:
        label = ""
    return issue, label


def _warning(code: str, detail: str) -> dict:
    return {"code": code, "message": detail}


def _normalise_hash(value) -> str:
    return str(value or "").strip().lower()


def _build_evidence(source_file: Path, root: Path, raw_root: Path, synced_at: str):
    source_bytes = source_file.read_bytes()
    try:
        document = json.loads(source_bytes.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DomainError(f"清洗 JSON 无法读取：{source_file.name}（{type(exc).__name__}）") from exc
    if not isinstance(document, dict) or not isinstance(document.get("metadata"), dict):
        raise DomainError(f"清洗 JSON 缺少 metadata 对象：{source_file.name}")

    metadata = document["metadata"]
    warnings: list[dict] = []
    if metadata.get("platform") != "caixin":
        warnings.append(_warning("wrong_platform", "来源平台不是 caixin，已隔离为待补正文"))

    stable_article_id = str(metadata.get("stable_id") or "").strip()
    article_id = str(metadata.get("article_id") or document.get("article_id") or "").strip()
    if not stable_article_id or not article_id:
        warnings.append(_warning("missing_article_id", "缺少稳定文章标识或 article_id"))

    body = document.get("faithful_body")
    if not isinstance(body, str):
        body = ""
        warnings.append(_warning("invalid_body", "faithful_body 不是文本"))
    claimed_body_hash = _normalise_hash(document.get("faithful_body_sha256"))
    actual_body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if not claimed_body_hash or claimed_body_hash != actual_body_hash:
        warnings.append(_warning("body_hash_mismatch", "faithful_body 哈希校验失败"))
    if not body.strip():
        warnings.append(_warning("empty_body", "清洗正文为空，不能作为已读事实"))
    elif not _body_readable(body):
        warnings.append(_warning("garbled_body", "清洗正文疑似乱码，不能作为已读事实"))

    extraction_status = str(metadata.get("extraction_status") or "unknown")
    body_completeness = str(metadata.get("body_completeness") or "unknown")
    pagination_completeness = str(metadata.get("pagination_completeness") or "unknown")
    body_integrity = metadata.get("body_integrity")
    if not isinstance(body_integrity, dict):
        body_integrity = {"status": "unknown"}
    if extraction_status != "complete":
        label = (
            "平台限制导致正文不可用"
            if extraction_status == "platform_limited"
            else f"正文提取状态不是 complete：{extraction_status}"
        )
        warnings.append(_warning(extraction_status, label))
    if body_completeness != "complete":
        warnings.append(
            _warning("body_incomplete", f"正文完整性不是 complete：{body_completeness}")
        )
    if pagination_completeness != "complete":
        warnings.append(
            _warning(
                "pagination_incomplete",
                f"分页完整性不是 complete：{pagination_completeness}",
            )
        )
    if body_integrity.get("status") != "complete":
        warnings.append(
            _warning(
                "body_integrity_failed",
                f"正文完整性校验状态不是 complete：{body_integrity.get('status') or 'unknown'}",
            )
        )
    missing_pages = _as_list(metadata.get("missing_pages"))
    if missing_pages:
        warnings.append(
            _warning(
                "missing_pages",
                "缺少分页：" + "、".join(str(page) for page in missing_pages),
            )
        )

    raw_paths = _as_list(metadata.get("raw_paths"))
    raw_hashes = _as_list(metadata.get("raw_sha256"))
    raw_actual_hashes: list[str | None] = []
    if not raw_paths:
        warnings.append(_warning("missing_raw_paths", "没有可定位的原始 HTML 路径"))
    if len(raw_paths) != len(raw_hashes):
        warnings.append(_warning("raw_hash_count_mismatch", "原件路径与原件哈希数量不一致"))
    for index, path_value in enumerate(raw_paths):
        relative, resolved = _safe_relative(path_value, raw_root)
        if resolved is None:
            raw_actual_hashes.append(None)
            warnings.append(
                _warning("raw_path_outside_root", f"原件路径越界或无效：{relative or '<empty>'}")
            )
            continue
        if not resolved.is_file():
            raw_actual_hashes.append(None)
            warnings.append(_warning("raw_missing", f"原件不存在：{relative}"))
            continue
        actual = _sha256_file(resolved)
        raw_actual_hashes.append(actual)
        claimed = _normalise_hash(raw_hashes[index] if index < len(raw_hashes) else None)
        if not claimed or claimed != actual:
            warnings.append(_warning("raw_hash_mismatch", f"原件哈希校验失败：{relative}"))

    published_at = metadata.get("published_at") or None
    if published_at:
        try:
            parse_time(published_at)
        except DomainError:
            warnings.append(_warning("invalid_published_at", "公开时间不是含时区的 ISO 8601"))
            published_at = None
    source_url = str(metadata.get("source_url") or "").strip()
    if source_url:
        parsed_url = urlsplit(source_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            warnings.append(_warning("invalid_source_url", "来源链接不是有效的 http/https 地址"))
            source_url = ""

    issue, issue_label = _issue(metadata, document)
    category = str(metadata.get("category") or document.get("category") or "").strip()
    version_material = {
        "adapter": ADAPTER_VERSION,
        "stable_id": stable_article_id,
        "article_id": article_id,
        "title": metadata.get("title"),
        "author": metadata.get("author"),
        "published_at": metadata.get("published_at"),
        "source_url": metadata.get("source_url"),
        "category": category,
        "issue": issue,
        "faithful_body_sha256_claimed": claimed_body_hash,
        "faithful_body_sha256_actual": actual_body_hash,
        "raw_paths": [str(value).replace("\\", "/") for value in raw_paths],
        "raw_sha256_claimed": [_normalise_hash(value) for value in raw_hashes],
        "raw_sha256_actual": raw_actual_hashes,
        "extraction_status": extraction_status,
        "body_completeness": body_completeness,
        "pagination_completeness": pagination_completeness,
        "body_integrity": body_integrity,
        "page_count": metadata.get("page_count"),
        "expected_page_count": metadata.get("expected_page_count"),
        "missing_pages": missing_pages,
        "validation_codes": sorted({item["code"] for item in warnings}),
    }
    version_hash = hashlib.sha256(
        json.dumps(
            version_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    article_component = re.sub(r"[^A-Za-z0-9_.-]", "-", article_id).strip("-.")
    if not article_component:
        article_component = hashlib.sha256(stable_article_id.encode("utf-8")).hexdigest()[:16]
    evidence_id = f"caixin-{article_component[:60]}-{version_hash[:20]}"
    status = "ready" if not warnings else "needs_text"

    raw_pages = []
    for page in _as_list(metadata.get("raw_pages")):
        if isinstance(page, dict):
            raw_pages.append(
                {
                    key: page.get(key)
                    for key in ("page", "raw_path", "sha256")
                    if page.get(key) is not None
                }
            )
    evidence = {
        "id": evidence_id,
        "kind": "article",
        "source_id": "caixin",
        "title": str(metadata.get("title") or article_id or source_file.stem),
        "url": source_url,
        "published_at": published_at,
        "captured_at": synced_at,
        "available_at": synced_at,
        "status": status,
        "text": body,
        "metadata": {
            "adapter_version": ADAPTER_VERSION,
            "article_stable_id": stable_article_id,
            "article_id": article_id,
            "author": metadata.get("author"),
            "category": category,
            "issue": issue,
            "issue_label": issue_label,
            "published_at_precision": _published_precision(metadata.get("published_at")),
            "archived_at": metadata.get("archived_at"),
            "source_key": metadata.get("source_key") or document.get("source_key"),
            "distilled_relative_path": source_file.relative_to(root).as_posix(),
            "source_json_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "faithful_body_sha256": actual_body_hash,
            "faithful_body_sha256_claimed": claimed_body_hash,
            "raw_paths": [str(value).replace("\\", "/") for value in raw_paths],
            "raw_sha256": [_normalise_hash(value) for value in raw_hashes],
            "raw_actual_sha256": raw_actual_hashes,
            "raw_pages": raw_pages,
            "page_count": metadata.get("page_count"),
            "expected_page_count": metadata.get("expected_page_count"),
            "missing_pages": missing_pages,
            "extraction_status": extraction_status,
            "body_completeness": body_completeness,
            "pagination_completeness": pagination_completeness,
            "body_integrity": body_integrity,
            "paragraphs": _paragraph_locations(body),
            "version_hash": version_hash,
            "validation_codes": sorted({item["code"] for item in warnings}),
            "warnings": [item["message"] for item in warnings],
            "extraction_complete": status == "ready",
        },
    }
    return evidence


def _caixin_documents(store: Store) -> list[dict]:
    return [item for item in store.list("evidence") if item.get("source_id") == "caixin"]


def _latest_publication(documents: list[dict]) -> tuple[str | None, str | None]:
    dated = []
    for item in documents:
        if item.get("published_at"):
            try:
                dated.append((parse_time(item["published_at"]), item["published_at"]))
            except DomainError:
                continue
    if not dated:
        return None, None
    value = max(dated, key=lambda pair: pair[0])[1]
    return value, str(value).split("T", 1)[0]


def _gap_summary(codes: Counter) -> list[dict]:
    labels = {
        "platform_limited": "平台限制，正文不可用",
        "partial_missing_pages": "来源缺页",
        "body_incomplete": "正文不完整",
        "body_integrity_failed": "正文完整性校验未通过",
        "pagination_incomplete": "分页不完整",
        "missing_pages": "缺少明确页码",
        "empty_body": "正文为空",
        "garbled_body": "正文疑似乱码",
        "body_hash_mismatch": "正文哈希错误",
        "raw_hash_mismatch": "原件哈希错误",
        "raw_missing": "原件缺失",
        "raw_path_outside_root": "原件路径越界或无效",
        "invalid_source_file": "清洗 JSON 无法解析",
    }
    return [
        {"code": code, "label": labels.get(code, code), "count": count}
        for code, count in sorted(codes.items())
        if count
    ]


def _status_from_store(store: Store, saved: dict | None = None) -> dict:
    saved = saved or store.read("source_status/caixin.json", {}) or {}
    documents = _caixin_documents(store)
    articles = {
        (item.get("metadata") or {}).get("article_stable_id")
        or (item.get("metadata") or {}).get("article_id")
        or item["id"]
        for item in documents
    }
    latest, latest_date = _latest_publication(documents)
    snapshot = saved.get("source_snapshot") or {
        "total": 0,
        "ready": 0,
        "blocked": 0,
        "invalid": 0,
    }
    root = saved.get("root") or str(DEFAULT_CAIXIN_ROOT)
    raw_root = saved.get("raw_root") or str(DEFAULT_CAIXIN_RAW_ROOT)
    if saved.get("last_sync"):
        connection = "ready" if not snapshot.get("blocked") and not snapshot.get("invalid") else "needs_attention"
    else:
        connection = "available" if Path(root).is_dir() else "source_missing"
    return {
        "source_id": "caixin",
        "name": "财新",
        "connection_status": connection,
        "root": root,
        "raw_root": raw_root,
        "last_sync": saved.get("last_sync"),
        "last_request_id": saved.get("last_request_id"),
        "total": snapshot.get("total", 0),
        "ready": snapshot.get("ready", 0),
        "blocked": snapshot.get("blocked", 0),
        "source_snapshot": snapshot,
        "library": {
            "article_count": len(articles),
            "version_count": len(documents),
            "ready_versions": sum(item.get("status") == "ready" for item in documents),
            "blocked_versions": sum(item.get("status") != "ready" for item in documents),
        },
        "latest_published_at": latest,
        "latest_published_date": latest_date,
        "gaps": saved.get("gaps", []),
        "auto_online_update": False,
        "update_mode": "manual_local_sync",
        "preserves_old_versions": True,
        "notes": [
            "只读取本地 ingestion 清洗材料，不联网抓取，也不修改来源文件。",
            "available_at 是实际接入工作台的时间；公开日期不会回填为历史可知时间。",
        ],
    }


def caixin_status(store: Store) -> dict:
    """Return durable connection state plus counts derived from frozen evidence."""
    return _status_from_store(store)


def sync_caixin(
    store: Store,
    request_id: str | None = None,
    *,
    root: str | Path | None = None,
    raw_root: str | Path | None = None,
) -> dict:
    """Freeze every local Caixin projection as an append-only evidence version."""
    from .settings import settings
    if not settings(store)["channels"]["caixin_enabled"]:
        raise DomainError("财新接入已暂停；仅在用户明确保留该研究渠道后同步，已有资料仍可查看")
    source_root = Path(root or DEFAULT_CAIXIN_ROOT).resolve()
    source_raw_root = Path(raw_root or DEFAULT_CAIXIN_RAW_ROOT).resolve()
    if not source_root.is_dir():
        raise DomainError(f"财新清洗目录不存在：{source_root}")
    if not source_raw_root.is_dir():
        raise DomainError(f"财新原件目录不存在：{source_raw_root}")

    sync_record_id = stable_id("caixin-sync", request_id) if request_id is not None else None
    parameters = {"root": str(source_root), "raw_root": str(source_raw_root)}
    if sync_record_id:
        previous = store.read(f"source_syncs/caixin/{sync_record_id}.json")
        if previous:
            if previous.get("parameters") != parameters:
                raise ConflictError("同一财新同步 request_id 对应了不同来源目录")
            return previous["result"]

    synced_at = now_iso()
    files = sorted(source_root.rglob("*.json"))
    evidence_by_id: dict[str, dict] = {}
    invalid_files: list[dict] = []
    gap_codes: Counter = Counter()
    for source_file in files:
        resolved = source_file.resolve()
        if not resolved.is_relative_to(source_root):
            invalid_files.append({"file": source_file.name, "reason": "清洗文件路径越界"})
            gap_codes["invalid_source_file"] += 1
            continue
        try:
            evidence = _build_evidence(resolved, source_root, source_raw_root, synced_at)
        except (OSError, DomainError) as exc:
            invalid_files.append(
                {"file": resolved.relative_to(source_root).as_posix(), "reason": str(exc)}
            )
            gap_codes["invalid_source_file"] += 1
            continue
        evidence_by_id[evidence["id"]] = evidence
        gap_codes.update((evidence.get("metadata") or {}).get("validation_codes") or [])

    ready = sum(item["status"] == "ready" for item in evidence_by_id.values())
    blocked = len(evidence_by_id) - ready
    created = 0
    existing = 0
    with store.transaction():
        if sync_record_id:
            previous = store.read(f"source_syncs/caixin/{sync_record_id}.json")
            if previous:
                if previous.get("parameters") != parameters:
                    raise ConflictError("同一财新同步 request_id 对应了不同来源目录")
                return previous["result"]
        for evidence_id, evidence in evidence_by_id.items():
            old = store.read(f"evidence/{evidence_id}.json")
            if old is not None:
                metadata = old.get("metadata") or {}
                if (
                    old.get("source_id") != "caixin"
                    or metadata.get("version_hash") != evidence["metadata"]["version_hash"]
                    or metadata.get("article_stable_id")
                    != evidence["metadata"]["article_stable_id"]
                ):
                    raise ConflictError(f"财新证据标识冲突，未覆盖旧记录：{evidence_id}")
                existing += 1
                continue
            store.write(f"evidence/{evidence_id}.json", evidence)
            created += 1

        snapshot = {
            "total": len(files),
            "ready": ready,
            "blocked": blocked,
            "invalid": len(invalid_files),
        }
        saved = {
            "source_id": "caixin",
            "root": str(source_root),
            "raw_root": str(source_raw_root),
            "last_sync": synced_at,
            "last_request_id": request_id,
            "source_snapshot": snapshot,
            "gaps": _gap_summary(gap_codes),
            "invalid_files": invalid_files,
        }
        store.write("source_status/caixin.json", saved, overwrite=True)
        status = _status_from_store(store, saved)
        result = {
            "source_id": "caixin",
            "request_id": request_id,
            "sync_id": sync_record_id,
            "synced_at": synced_at,
            "scanned": len(files),
            "created": created,
            "existing": existing,
            "ready": ready,
            "blocked": blocked,
            "invalid": len(invalid_files),
            "gaps": saved["gaps"],
            "invalid_files": invalid_files,
            "status": status,
        }
        if sync_record_id:
            store.write(
                f"source_syncs/caixin/{sync_record_id}.json",
                {"id": sync_record_id, "parameters": parameters, "result": result},
            )
    return result
