"""Local material ingestion and inspectable source scorecards."""
from __future__ import annotations
import hashlib
import io
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from .store import Store, DomainError, ConflictError, stable_id, safe_id, now_iso, parse_time

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
SOURCE_NAMES = {"tencent": "腾讯行情", "sse": "上海证券交易所", "szse": "深圳证券交易所", "icbc": "工商银行公开披露", "pbc": "中国人民银行", "nfra": "国家金融监督管理总局", "caixin": "财新", "manual": "人工提供"}


def _page_readable(text):
    # Some Chinese financial PDFs map glyphs to unrelated Unicode scripts.
    # Non-empty extraction is not proof that the page was actually read.
    letters = [c for c in text if c.isalpha()]
    suspicious = sum('\u0d80' <= c <= '\u0fff' or '\ue000' <= c <= '\uf8ff' or c == '\ufffd' for c in text)
    return bool(text) and not (suspicious >= 12 and suspicious / max(1, len(letters)) > .15)


def _meta(payload):
    title = str(payload.get("title", "")).strip()
    if not title or len(title) > 240:
        raise DomainError("请填写 1 至 240 字的材料标题")
    source = safe_id(str(payload.get("source_id", "manual")))
    url = str(payload.get("url") or "").strip()
    if url and (urlsplit(url).scheme not in ("https", "http") or not urlsplit(url).netloc):
        raise DomainError("来源链接只能使用 http 或 https")
    published = payload.get("published_at") or None
    if published:
        published = parse_time(published).isoformat()
        if parse_time(published) > parse_time(now_iso()):
            raise DomainError("材料公开时间不能在未来")
    return {"title": title, "source_id": source, "url": url, "published_at": published}


def add_text(store: Store, payload: dict):
    meta = _meta(payload)
    text = str(payload.get("text", "")).strip()
    if not text or len(text) > MAX_TEXT_CHARS:
        raise DomainError("正文为空或超过 200 万字符，请分文章导入")
    evidence_id = stable_id("text", payload.get("request_id"))
    fingerprint = hashlib.sha256((str(meta) + text).encode("utf-8")).hexdigest()
    with store.transaction():
        old = store.read(f"evidence/{evidence_id}.json")
        if old:
            if old["metadata"]["input_sha256"] != fingerprint:
                raise ConflictError("同一导入请求标识对应了不同内容")
            return old
        captured = now_iso()
        blob = f"blobs/{evidence_id}.txt"
        store.write_text(blob, text)
        evidence = {"id": evidence_id, "kind": "text", **meta, "captured_at": captured,
                    "available_at": captured, "status": "ready", "text": text,
                    "metadata": {"input_sha256": fingerprint, "original_path": str(store.path(blob)),
                    "original_relative_path": blob, "pages": [], "extraction_complete": True,
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "warnings": []}}
        return store.write(f"evidence/{evidence_id}.json", evidence)


def add_file(store: Store, payload: dict, filename: str, content: bytes):
    if not content or len(content) > MAX_FILE_BYTES:
        raise DomainError("文件为空或超过 20 MiB")
    ext = Path(filename).suffix.lower()
    if ext not in (".pdf", ".txt", ".md"):
        raise DomainError("首版只接受 PDF、TXT 和 Markdown 文件")
    meta = _meta(payload)
    digest = hashlib.sha256(content).hexdigest()
    evidence_id = stable_id("file", payload.get("request_id"))
    fingerprint = hashlib.sha256((str(meta) + digest).encode()).hexdigest()
    old = store.read(f"evidence/{evidence_id}.json")
    if old:
        if old["metadata"]["input_sha256"] != fingerprint:
            raise ConflictError("同一文件导入请求标识对应不同内容")
        return old
    pages, warnings = [], []
    status = "ready"
    if ext == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise DomainError("文件内容不是有效 PDF")
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content), strict=False)
            if reader.is_encrypted and reader.decrypt("") == 0:
                raise DomainError("PDF 已加密，需用户提供可读取的副本")
            if len(reader.pages) > 500:
                raise DomainError("PDF 超过 500 页，请按期刊或文章拆分导入")
            for i, page in enumerate(reader.pages):
                try:
                    text = (page.extract_text() or "").strip()
                except Exception:
                    text = ""
                readable = _page_readable(text)
                pages.append({"page": i + 1, "text": text, "status": "ready" if readable else "needs_text"})
            text = "\n\n".join(f"[第 {p['page']} 页]\n{p['text']}" for p in pages if p["status"] == "ready")
        except ImportError as exc:
            raise DomainError("缺少 pypdf，请使用项目启动器配置的 Python 环境") from exc
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(f"PDF 无法读取：{type(exc).__name__}，原件未被当作已读资料") from exc
        empty_pages = [p["page"] for p in pages if not p["text"]]
        if empty_pages:
            warnings.append("以下页未提取到文字（可能为扫描页或图片页）：" + ", ".join(map(str, empty_pages)))
        garbled_pages = [p["page"] for p in pages if p["text"] and p['status'] != 'ready']
        if garbled_pages:
            warnings.append("以下页疑似字体编码乱码，未作为已读正文：" + ", ".join(map(str, garbled_pages)))
        if not any(p['status'] == 'ready' for p in pages) or len(garbled_pages) > len(pages) / 4:
            status = "needs_text"
            warnings.append("正文未能可靠提取，请提供文字版或先完成 OCR；当前不会进入研究证据")
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = content.decode("gb18030")
            except UnicodeDecodeError as exc:
                raise DomainError("文本编码无法识别，请另存为 UTF-8") from exc
        if not text.strip():
            status = "needs_text"
            warnings.append("没有可读取的正文")
    if len(text) > MAX_TEXT_CHARS:
        raise DomainError("提取正文超过 200 万字符，请分文章导入，不静默截断")
    with store.transaction():
        old = store.read(f"evidence/{evidence_id}.json")
        if old:
            if old["metadata"]["input_sha256"] != fingerprint:
                raise ConflictError("同一文件导入请求标识对应不同内容")
            return old
        captured = now_iso()
        blob = f"blobs/{evidence_id}{ext}"
        path = store.path(blob)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temp.write_bytes(content)
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()
        evidence = {"id": evidence_id, "kind": "pdf" if ext == ".pdf" else "text", **meta,
                    "captured_at": captured, "available_at": captured, "status": status, "text": text,
                    "metadata": {"filename": Path(filename).name, "sha256": digest, "input_sha256": fingerprint,
                    "original_path": str(path), "original_relative_path": blob, "pages": pages,
                    "extraction_complete": status == "ready" and not warnings, "warnings": warnings}}
        return store.write(f"evidence/{evidence_id}.json", evidence)


def _preview(text, query="", limit=320):
    text = str(text or "").strip()
    if not text:
        return ""
    folded = text.casefold()
    terms = [term.casefold() for term in str(query or "").split() if term]
    positions = [folded.find(term) for term in terms]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return text[:limit]
    start = max(0, min(positions) - limit // 3)
    end = min(len(text), start + limit)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _evidence_summary(evidence, query=""):
    metadata = evidence.get("metadata") or {}
    summary = {
        key: evidence.get(key)
        for key in (
            "id",
            "kind",
            "source_id",
            "title",
            "url",
            "published_at",
            "captured_at",
            "available_at",
            "status",
        )
    }
    summary.update(
        {
            "preview": _preview(evidence.get("text", ""), query),
            "warnings": metadata.get("warnings", []),
            "category": metadata.get("category") or "",
            "article_id": metadata.get("article_id") or "",
            "issue_label": metadata.get("issue_label") or "",
            "published_at_precision": metadata.get("published_at_precision") or "unknown",
        }
    )
    return summary


def _evidence_sort_key(evidence):
    for field in ("published_at", "available_at", "captured_at"):
        value = evidence.get(field)
        if value:
            try:
                return parse_time(value).timestamp(), str(evidence.get("id") or "")
            except DomainError:
                continue
    return float("-inf"), str(evidence.get("id") or "")


def evidence_summaries(store):
    return [
        _evidence_summary(evidence)
        for evidence in sorted(store.list("evidence"), key=_evidence_sort_key, reverse=True)
    ]


def search_evidence(
    store,
    query="",
    source_id="",
    category="",
    issue="",
    status="",
    offset=0,
    limit=25,
):
    """Search frozen evidence text and metadata without an LLM or external service."""
    query = str(query or "").strip()
    source_id = str(source_id or "").strip()
    category = str(category or "").strip()
    issue = str(issue or "").strip()
    status = str(status or "").strip()
    if source_id:
        safe_id(source_id)
    if status and status not in {"ready", "needs_text", "error"}:
        raise DomainError("status 只能是 ready、needs_text 或 error")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise DomainError("offset 必须是非负整数")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise DomainError("limit 必须是 1 至 100 的整数")

    documents = store.list("evidence")
    source_scope = [
        item for item in documents if not source_id or item.get("source_id") == source_id
    ]
    categories = sorted(
        {
            str((item.get("metadata") or {}).get("category") or "").strip()
            for item in source_scope
            if str((item.get("metadata") or {}).get("category") or "").strip()
        }
    )
    issues = sorted(
        {
            str((item.get("metadata") or {}).get("issue_label") or "").strip()
            for item in source_scope
            if str((item.get("metadata") or {}).get("issue_label") or "").strip()
        },
        reverse=True,
    )
    terms = [term.casefold() for term in query.split() if term]
    matches = []
    for item in source_scope:
        metadata = item.get("metadata") or {}
        if category and str(metadata.get("category") or "") != category:
            continue
        if issue and str(metadata.get("issue_label") or "") != issue:
            continue
        if status and item.get("status") != status:
            continue
        if terms:
            searchable = "\n".join(
                str(value or "")
                for value in (
                    item.get("title"),
                    metadata.get("author"),
                    metadata.get("category"),
                    metadata.get("article_id"),
                    metadata.get("article_stable_id"),
                    metadata.get("issue_label"),
                    item.get("text"),
                )
            ).casefold()
            if not all(term in searchable for term in terms):
                continue
        matches.append(item)
    matches.sort(key=_evidence_sort_key, reverse=True)
    return {
        "items": [
            _evidence_summary(item, query=query) for item in matches[offset : offset + limit]
        ],
        "total": len(matches),
        "offset": offset,
        "limit": limit,
        "categories": categories,
        "issues": issues,
    }


def add_check(store, payload):
    source_id = safe_id(str(payload.get("source_id", "")))
    verdict = payload.get("verdict")
    claim = str(payload.get("claim", "")).strip()
    refs = payload.get("evidence_ids", [])
    if verdict not in ("correct", "incorrect", "unverified") or not claim:
        raise DomainError("请填写待核验事实和正确、错误或待核验状态")
    if verdict != "unverified" and not refs:
        raise DomainError("确定事实正误必须给出核验依据，不能凭股价表现评分")
    for ref in refs:
        e = store.read(f"evidence/{safe_id(ref)}.json")
        if not e or e["status"] != "ready":
            raise DomainError("核验依据不存在或尚未成功读取")
    record = {"id": stable_id("check", payload.get("request_id")), "source_id": source_id,
              "claim": claim, "verdict": verdict, "evidence_ids": refs, "note": str(payload.get("note", ""))}
    with store.transaction():
        old = store.read(f"source_checks/{record['id']}.json")
        if old:
            if any(old.get(k) != v for k, v in record.items()):
                raise ConflictError("事实核验请求已经保存且内容不同")
            return old
        record["recorded_at"] = now_iso()
        return store.write(f"source_checks/{record['id']}.json", record)


def _source_result_records(store):
    base = store.path("source_results")
    if not base.exists():
        return []
    records = []
    for path in sorted(base.rglob("*.json")):
        if path.resolve().is_relative_to(base.resolve()):
            records.append(store.read(path.relative_to(store.root)))
    return records


def _prediction_observations(store, runs):
    observations_by_source = {}
    seen = set()
    if runs is None:
        for record in _source_result_records(store):
            if record.get("mode", "live") == "replay":
                continue
            if not isinstance(record.get("direction_hit"), bool):
                continue
            source = str(record.get("source_id") or "unknown")
            key = (
                source,
                record.get("run_id"),
                record.get("symbol"),
                record.get("review_date"),
            )
            if key in seen:
                continue
            seen.add(key)
            observations_by_source.setdefault(source, []).append(record)
        return observations_by_source

    for run in runs:
        if run.get("mode", "live") == "replay":
            continue
        frozen = {
            item.get("id"): item
            for item in run.get("evidence", [])
            if isinstance(item, dict) and item.get("id")
        }
        plans = (run.get("decision") or {}).get("plans", [])
        for review in run.get("reviews", []):
            for observation in review.get("observations", []):
                if observation.get("status") not in {
                    "resolved",
                    "evaluated",
                    "complete",
                    "completed",
                }:
                    continue
                plan_refs = {
                    ref
                    for plan in plans
                    if plan.get("symbol") == observation.get("symbol")
                    for ref in plan.get("evidence_ids", [])
                }
                by_source = {}
                for ref in plan_refs:
                    frozen_item = frozen.get(ref)
                    if frozen_item:
                        source = str(frozen_item.get("source_id") or "unknown")
                        by_source.setdefault(source, []).append(ref)
                for source, evidence_ids in by_source.items():
                    key = (
                        source,
                        run.get("id"),
                        observation.get("symbol"),
                        observation.get("review_date"),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    observations_by_source.setdefault(source, []).append(
                        {
                            "run_id": run.get("id"),
                            "evidence_ids": sorted(evidence_ids),
                            **observation,
                        }
                    )
    return observations_by_source


def source_scores(store, runs=None):
    evidence = store.list("evidence")
    prediction_by_source = _prediction_observations(store, runs)
    sources = (
        set(SOURCE_NAMES)
        | {str(item.get("source_id") or "unknown") for item in evidence}
        | set(prediction_by_source)
    )
    checks = store.list("source_checks")
    results = []
    for source in sorted(sources):
        documents = [item for item in evidence if item.get("source_id") == source]
        article_keys = {
            (item.get("metadata") or {}).get("article_stable_id")
            or (item.get("metadata") or {}).get("article_id")
            or item.get("id")
            for item in documents
        }
        facts = [item for item in checks if item.get("source_id") == source]
        correct = sum(item.get("verdict") == "correct" for item in facts)
        incorrect = sum(item.get("verdict") == "incorrect" for item in facts)

        acquisition_lags = []
        import_lags = []
        date_only_imports = 0
        for document in documents:
            metadata = document.get("metadata") or {}
            acquired_at = metadata.get("news_acquired_at")
            published_at = document.get("published_at")
            if acquired_at and published_at:
                try:
                    acquisition_lags.append(
                        (parse_time(acquired_at) - parse_time(published_at)).total_seconds()
                        / 3600
                    )
                except DomainError:
                    pass
            if not published_at or not document.get("available_at"):
                continue
            if metadata.get("published_at_precision") == "date":
                date_only_imports += 1
                continue
            try:
                import_lags.append(
                    (
                        parse_time(document["available_at"]) - parse_time(published_at)
                    ).total_seconds()
                    / 3600
                )
            except DomainError:
                pass

        observations = prediction_by_source.get(source, [])
        scored = [
            item for item in observations if isinstance(item.get("direction_hit"), bool)
        ]
        fact_samples = correct + incorrect
        results.append(
            {
                "id": source,
                "name": SOURCE_NAMES.get(source, source),
                "document_count": len(documents),
                "article_count": len(article_keys),
                "version_count": len(documents),
                "facts": {
                    "samples": fact_samples,
                    "correct": correct,
                    "incorrect": incorrect,
                    "unverified": sum(
                        item.get("verdict") == "unverified" for item in facts
                    ),
                    "accuracy": correct / fact_samples if fact_samples else None,
                    "checks": facts,
                    "label": "事实可靠性只依据带证据的事实核验；不因后续股价涨跌改变。",
                },
                "timeliness": {
                    "samples": len(acquisition_lags),
                    "mean_delay_hours": (
                        sum(acquisition_lags) / len(acquisition_lags)
                        if acquisition_lags
                        else None
                    ),
                    "news_acquisition": {
                        "samples": len(acquisition_lags),
                        "mean_delay_hours": (
                            sum(acquisition_lags) / len(acquisition_lags)
                            if acquisition_lags
                            else None
                        ),
                    },
                    "import_delay": {
                        "samples": len(import_lags),
                        "mean_delay_hours": (
                            sum(import_lags) / len(import_lags) if import_lags else None
                        ),
                        "date_only_samples": date_only_imports,
                    },
                    "label": "导入延迟只表示资料进入工作台的耗时，不冒充新闻获取延迟；日期精度不足的样本不计算小时均值。",
                },
                "prediction": {
                    "samples": len(scored),
                    "hits": sum(item["direction_hit"] for item in scored),
                    "hit_rate": (
                        sum(item["direction_hit"] for item in scored) / len(scored)
                        if scored
                        else None
                    ),
                    "observations": observations,
                    "label": "按冻结 run 的事前引用记录价格方向关联；回放隔离，重复版本不增加样本，不代表来源造成结果。",
                },
                "overall_score": None,
                "automatic_weight": False,
                "status": (
                    "待评"
                    if not fact_samples + len(scored)
                    else "展示分项样本，尚不自动调权"
                ),
            }
        )
    return results
