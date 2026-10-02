import hashlib
import json
from pathlib import Path

import pytest

from app.caixin import caixin_status, sync_caixin
from app.evidence import search_evidence
from app.store import ConflictError, Store
from app.settings import update_settings


def _write_projection(
    distilled_root: Path,
    raw_root: Path,
    *,
    article_id="1001",
    body="第一段。\n\n第二段包含独特检索词：供应链韧性。",
    extraction_status="complete",
    pagination="complete",
    missing_pages=None,
    raw_path=None,
    raw_hash=None,
    body_hash=None,
    category="金融",
    title=None,
):
    distilled_root.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    raw_path = raw_path or f"2026/{article_id}.html"
    if ".." not in raw_path:
        raw_file = raw_root / raw_path
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        raw_bytes = f"<html>raw article {article_id}; cookie=PRIVATE-DO-NOT-COPY</html>".encode()
        raw_file.write_bytes(raw_bytes)
        actual_raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    else:
        actual_raw_hash = "0" * 64
    body_hash = body_hash or hashlib.sha256(body.encode()).hexdigest()
    raw_hash = raw_hash or actual_raw_hash
    missing_pages = missing_pages or []
    document = {
        "metadata": {
            "platform": "caixin",
            "stable_id": f"caixin-article:{article_id}",
            "article_id": article_id,
            "title": title or f"测试文章 {article_id}",
            "author": "测试作者",
            "published_at": "2026-09-01T00:00:00+00:00",
            "archived_at": None,
            "source_url": f"https://weekly.caixin.com/2026-09-01/{article_id}.html",
            "raw_paths": [raw_path],
            "raw_sha256": [raw_hash],
            "extraction_status": extraction_status,
            "source_key": "weekly.caixin.com:weekly",
            "category": category,
            "issue_year": 2026,
            "annual_issue": 35,
            "total_issue": 1222,
            "publication_date": "2026-09-03T00:00:00+00:00",
            "official_issue_url": "https://weekly.caixin.com/2026/cw1222/",
            "page_count": 1,
            "expected_page_count": 1 if not missing_pages else 2,
            "missing_pages": missing_pages,
            "body_completeness": (
                "complete" if extraction_status != "platform_limited" else "platform_limited"
            ),
            "pagination_completeness": pagination,
            "body_integrity": {
                "status": "complete" if body else "platform_limited",
                "char_count": len(body),
                "paragraph_count": 2 if body else 0,
            },
            "raw_pages": [{"page": 1, "raw_path": raw_path, "sha256": raw_hash}],
        },
        "faithful_body": body,
        "faithful_body_sha256": body_hash,
        "ai_enrichment": {
            "summary": "AI-ONLY-SECRET-SUMMARY",
            "tags": ["AI-ONLY-TAG"],
        },
        "article_id": article_id,
        "category": category,
        "issue": {
            "year": 2026,
            "annual_issue": 35,
            "total_issue": 1222,
            "publication_date": "2026-09-03T00:00:00+00:00",
            "official_issue_url": "https://weekly.caixin.com/2026/cw1222/",
        },
    }
    target = distilled_root / category / f"{article_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return target


def test_sync_freezes_versions_searches_full_text_and_is_idempotent(tmp_path):
    source = tmp_path / "source"
    raw = tmp_path / "raw"
    source_file = _write_projection(source, raw)
    _write_projection(
        source,
        raw,
        article_id="1002",
        body="",
        extraction_status="platform_limited",
        category="时政",
    )
    original_source = source_file.read_bytes()
    store = Store(tmp_path / "data")
    update_settings(store, {"channels": {"caixin_enabled": True}})
    frozen_run = {"id": "existing-run", "evidence": [{"id": "old"}], "decision": "不改"}
    store.write("runs/existing-run.json", frozen_run)

    first = sync_caixin(store, request_id="first", root=source, raw_root=raw)
    assert first["scanned"] == 2
    assert first["created"] == 2
    assert first["ready"] == 1
    assert first["blocked"] == 1
    assert first["invalid"] == 0
    assert store.read("runs/existing-run.json") == frozen_run
    assert source_file.read_bytes() == original_source

    documents = [item for item in store.list("evidence") if item["source_id"] == "caixin"]
    ready = next(item for item in documents if item["status"] == "ready")
    blocked = next(item for item in documents if item["status"] == "needs_text")
    assert ready["available_at"] == first["synced_at"]
    assert ready["available_at"] != ready["published_at"]
    assert ready["metadata"]["published_at_precision"] == "date"
    assert ready["metadata"]["issue_label"] == "2026年第35期（总第1222期）"
    assert ready["text"][
        ready["metadata"]["paragraphs"][1]["start"] : ready["metadata"]["paragraphs"][1]["end"]
    ].startswith("第二段")
    assert "正文为空" in " ".join(blocked["metadata"]["warnings"])
    frozen_json = json.dumps(documents, ensure_ascii=False)
    assert "AI-ONLY-SECRET-SUMMARY" not in frozen_json
    assert "PRIVATE-DO-NOT-COPY" not in frozen_json

    result = search_evidence(
        store, query="供应链韧性", source_id="caixin", category="金融", limit=10
    )
    assert result["total"] == 1
    assert result["items"][0]["id"] == ready["id"]
    assert "供应链韧性" in result["items"][0]["preview"]
    assert result["categories"] == ["时政", "金融"]
    assert result["issues"] == ["2026年第35期（总第1222期）"]
    assert "text" not in result["items"][0]
    assert "authority_level" not in result["items"][0]

    same_request = sync_caixin(store, request_id="first", root=source, raw_root=raw)
    assert same_request == first
    repeat = sync_caixin(store, root=source, raw_root=raw)
    assert repeat["created"] == 0 and repeat["existing"] == 2
    assert next(item for item in store.list("evidence") if item["id"] == ready["id"]) == ready

    _write_projection(
        source,
        raw,
        body="第一段修订。\n\n第二段保留供应链韧性，但内容版本已变化。",
    )
    revised = sync_caixin(store, root=source, raw_root=raw)
    assert revised["created"] == 1 and revised["existing"] == 1
    status = caixin_status(store)
    assert status["library"] == {
        "article_count": 2,
        "version_count": 3,
        "ready_versions": 2,
        "blocked_versions": 1,
    }
    assert store.read(f"evidence/{ready['id']}.json") == ready


def test_sync_quarantines_hash_path_missing_page_and_garbled_failures(tmp_path):
    source = tmp_path / "source"
    raw = tmp_path / "raw"
    _write_projection(source, raw, article_id="hash", body_hash="f" * 64)
    _write_projection(source, raw, article_id="path", raw_path="../outside.html")
    _write_projection(
        source,
        raw,
        article_id="pages",
        extraction_status="partial_missing_pages",
        pagination="partial_missing_pages",
        missing_pages=[2],
    )
    _write_projection(source, raw, article_id="garbled", body="乱码" + "ᆴཟಆ౯ӑᄡუ" * 30)
    store = Store(tmp_path / "data")
    update_settings(store, {"channels": {"caixin_enabled": True}})
    result = sync_caixin(store, root=source, raw_root=raw)
    assert result["ready"] == 0 and result["blocked"] == 4
    assert all(item["status"] == "needs_text" for item in store.list("evidence"))
    warnings = {
        item["metadata"]["article_id"]: " ".join(item["metadata"]["warnings"])
        for item in store.list("evidence")
    }
    assert "哈希校验失败" in warnings["hash"]
    assert "路径越界" in warnings["path"]
    assert "缺少分页" in warnings["pages"]
    assert "疑似乱码" in warnings["garbled"]
    assert {gap["code"] for gap in result["gaps"]} >= {
        "body_hash_mismatch",
        "raw_path_outside_root",
        "missing_pages",
        "garbled_body",
    }


def test_sync_request_id_rejects_changed_roots(tmp_path):
    first_source, first_raw = tmp_path / "first-source", tmp_path / "first-raw"
    second_source, second_raw = tmp_path / "second-source", tmp_path / "second-raw"
    _write_projection(first_source, first_raw)
    _write_projection(second_source, second_raw)
    store = Store(tmp_path / "data")
    update_settings(store, {"channels": {"caixin_enabled": True}})
    sync_caixin(store, request_id="same", root=first_source, raw_root=first_raw)
    with pytest.raises(ConflictError):
        sync_caixin(store, request_id="same", root=second_source, raw_root=second_raw)
