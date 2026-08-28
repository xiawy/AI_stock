"""News vector store for quant (设计文档 §9.3).

- ChromaDB 可用时: 嵌入式向量库 ``quant_news`` collection, 语义检索
  利空/利好; 查询强制带时间窗口过滤, 减少历史噪声.
- 未安装时: 降级为 SQLite ``quant_news_vectors`` 元数据表 + 关键词
  LIKE 检索 (接口一致, 保底可用).
- 新闻入库去重 (news_hash); 超过 30 天自动清理 (双端同步).
- 定期导出元数据 JSON 备份, 防止 Chroma 文件损坏.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import db_ops
from .config import NEWS_RETENTION_DAYS, NEWS_SEARCH_DEFAULT_DAYS

logger = logging.getLogger(__name__)

_chroma_warned = False


def news_hash(title: str) -> str:
    return hashlib.md5(title.encode("utf-8")).hexdigest()[:16]


class NewsVectorStore:
    """ChromaDB 优先 / SQLite 关键词降级 的新闻检索."""

    def __init__(self, persist_dir: Optional[Path] = None):
        self._client = None
        self._collection = None
        if persist_dir is None:
            root = Path(__file__).resolve().parent.parent.parent
            persist_dir = root / "backend" / "data" / "quant_chroma"
        try:
            import chromadb

            persist_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(persist_dir))
            self._collection = self._client.get_or_create_collection(
                name="quant_news",
                metadata={"hnsw:space": "cosine"},
            )
            logger.info("NewsVectorStore: chromadb ready (%d docs)", self._collection.count())
        except ImportError:
            global _chroma_warned
            if not _chroma_warned:
                _chroma_warned = True
                logger.info(
                    "chromadb 未安装 — 新闻检索降级为 SQLite 关键词匹配; "
                    "pip install 'chromadb>=1.0.0' 启用语义检索",
                )
        except Exception as exc:
            logger.warning("chromadb init failed (%s); 使用 SQLite 降级检索", exc)

    @property
    def semantic(self) -> bool:
        return self._collection is not None

    # ------------------------------------------------------------------
    # 写入 (去重)
    # ------------------------------------------------------------------

    def add_news(self, items: list[dict], symbol: str = "") -> int:
        """新闻入库 (标题哈希去重). 返回新增条数."""
        added = 0
        batch_ids, batch_docs, batch_meta = [], [], []
        for item in items:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            h = news_hash(title)
            # 元数据表先写 (source of truth; chroma 损坏可重建)
            if db_ops.news_meta_add({
                "news_hash": h,
                "title": title,
                "content": item.get("content", "")[:1000],
                "source": item.get("source", ""),
                "pub_time": item.get("time", "") or item.get("pub_time", ""),
                "symbol": symbol,
            }):
                added += 1
                if self._collection is not None:
                    batch_ids.append(h)
                    batch_docs.append(
                        title + "\n" + (item.get("content", "") or "")[:800],
                    )
                    batch_meta.append({
                        "news_hash": h,
                        "symbol": symbol or item.get("symbol", ""),
                        "pub_time": item.get("time", "") or item.get("pub_time", ""),
                        "added": datetime.now(timezone.utc).isoformat(),
                    })
        if batch_ids and self._collection is not None:
            try:
                self._collection.add(
                    ids=batch_ids, documents=batch_docs, metadatas=batch_meta,
                )
            except Exception as exc:
                logger.warning("chroma add failed (%s); meta 已入库, 可重建", exc)
        return added

    # ------------------------------------------------------------------
    # 检索 (强制时间窗口)
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        days: int = NEWS_SEARCH_DEFAULT_DAYS,
        n_results: int = 5,
        symbol: str = "",
    ) -> list[dict]:
        """检索相关新闻 (语义优先; 强制 days 天时间窗口过滤)."""
        if self._collection is not None and self._collection.count() > 0:
            try:
                semantic = self._search_chroma(query, days, n_results, symbol)
                if semantic:
                    return semantic
            except Exception as exc:
                logger.warning("chroma query failed (%s); 降级关键词检索", exc)
        return self._search_sqlite(query, days, n_results)

    def _search_chroma(self, query, days, n_results, symbol) -> list[dict]:
        # 时间窗口在应用层过滤 (chroma where 对 ISO 时间字符串比较不可靠)
        results = self._collection.query(
            query_texts=[query], n_results=min(n_results * 4, 30),
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        out = []
        if results and results.get("ids") and results["ids"][0]:
            metas = (results.get("metadatas") or [[]])[0]
            docs = (results.get("documents") or [[]])[0]
            for i, doc_id in enumerate(results["ids"][0]):
                meta = metas[i] if i < len(metas) else {}
                added = str(meta.get("added", ""))
                try:
                    added_dt = datetime.fromisoformat(added)
                except ValueError:
                    added_dt = None
                if added_dt is not None and added_dt < cutoff:
                    continue
                if symbol and meta.get("symbol") not in ("", symbol):
                    continue
                out.append({
                    "news_hash": doc_id,
                    "title": (docs[i] if i < len(docs) else "").splitlines()[0][:200],
                    "content": (docs[i] if i < len(docs) else "")[:500],
                    "source": "chroma",
                    "pub_time": meta.get("pub_time", ""),
                })
                if len(out) >= n_results:
                    break
        return out

    def _search_sqlite(self, query, days, n_results) -> list[dict]:
        keywords = [
            kw.strip() for kw in query.replace("，", " ").replace(",", " ").split()
            if len(kw.strip()) >= 2
        ][:6]
        if not keywords:
            keywords = [query[:12]] if query else []
        return db_ops.news_meta_search(keywords, days=days, limit=n_results)

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    def cleanup(self, days: int = NEWS_RETENTION_DAYS) -> int:
        """超期清理 (SQLite 元数据 + chroma 双端同步)."""
        removed = db_ops.news_meta_cleanup(days)
        if self._collection is not None and removed > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            try:
                all_meta = self._collection.get()
                stale_ids = []
                for i, doc_id in enumerate(all_meta.get("ids", [])):
                    metas = all_meta.get("metadatas") or []
                    meta = metas[i] if i < len(metas) else {}
                    try:
                        added_dt = datetime.fromisoformat(str(meta.get("added", "")))
                    except ValueError:
                        continue
                    if added_dt < cutoff:
                        stale_ids.append(doc_id)
                if stale_ids:
                    self._collection.delete(ids=stale_ids)
            except Exception as exc:
                logger.warning("chroma cleanup failed: %s", exc)
        if removed:
            logger.info("NewsVectorStore cleaned %d stale news", removed)
        return removed

    def backup_metadata(self, out_dir: Optional[Path] = None) -> Optional[str]:
        """导出元数据 JSON 备份 (防 chroma 文件损坏, §9.3)."""
        if out_dir is None:
            root = Path(__file__).resolve().parent.parent.parent
            out_dir = root / "backend" / "data" / "quant_backups"
        out_dir.mkdir(parents=True, exist_ok=True)
        from .db import session_scope
        from .db_models import NewsVectorMeta

        try:
            with session_scope() as s:
                rows = s.query(NewsVectorMeta).order_by(
                    NewsVectorMeta.added_at.desc(),
                ).limit(5000).all()
                payload = [
                    {
                        "news_hash": r.news_hash,
                        "title": r.title,
                        "content": r.content[:500],
                        "source": r.source,
                        "pub_time": r.pub_time,
                        "symbol": r.symbol,
                    }
                    for r in rows
                ]
            path = out_dir / f"news_meta_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8",
            )
            logger.info("News metadata backup: %s (%d items)", path, len(payload))
            return str(path)
        except Exception as exc:
            logger.warning("news metadata backup failed: %s", exc)
            return None


_store: Optional[NewsVectorStore] = None


def get_news_store() -> NewsVectorStore:
    global _store
    if _store is None:
        _store = NewsVectorStore()
    return _store
