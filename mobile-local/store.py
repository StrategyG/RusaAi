"""SQLite-backed, device-local memory and hybrid lexical/sparse-vector RAG."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

TOKEN_RE = re.compile(r"[\w]{2,}", re.UNICODE)
MAX_DOCUMENT_CHARS = 2_000_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(text: str) -> list[str]:
    return [token.casefold() for token in TOKEN_RE.findall(text)][:100_000]


def _chunks(text: str, size: int = 1100, overlap: int = 160) -> list[str]:
    normalized = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n").replace("\r", "\n"))
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if not normalized:
        return []
    result: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + size, len(normalized))
        if end < len(normalized):
            boundary = max(
                normalized.rfind("\n\n", start + size // 2, end),
                normalized.rfind("\n", start + size // 2, end),
                normalized.rfind(". ", start + size // 2, end),
                normalized.rfind(" ", start + size // 2, end),
            )
            if boundary > start:
                end = boundary + (2 if normalized[boundary:boundary + 2] == ". " else 1)
        piece = normalized[start:end].strip()
        if piece:
            result.append(piece)
        if end >= len(normalized):
            break
        start = max(start + 1, end - overlap)
    return result


class LocalStore:
    """Small local store. All user documents and chats remain in the app data folder."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.fts_available = True
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._db() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    collection_name TEXT NOT NULL DEFAULT 'default',
                    content_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_documents_collection
                    ON documents(collection_name, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_documents_hash
                    ON documents(collection_name, content_hash);
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_document
                    ON chunks(document_id, chunk_index);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, id);
                """
            )
            try:
                db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                    "USING fts5(chunk_id UNINDEXED, content, tokenize='unicode61')"
                )
            except sqlite3.OperationalError:
                self.fts_available = False

    def ingest_text(
        self,
        *,
        title: str,
        source_uri: str,
        content: str,
        collection: str = "default",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Document content is empty.")
        if len(content) > MAX_DOCUMENT_CHARS:
            raise ValueError("Document exceeds the 2 million character limit.")
        if not isinstance(title, str) or not title.strip() or len(title) > 256:
            raise ValueError("Title must contain 1 to 256 characters.")
        if not isinstance(source_uri, str) or not source_uri.strip() or len(source_uri) > 2048:
            raise ValueError("Source URI is invalid.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,63}", collection):
            raise ValueError("Collection name contains unsupported characters.")
        if re.search(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*\S+", content):
            raise ValueError("Possible credential material detected; remove secrets before ingestion.")
        parts = _chunks(content)
        if not parts:
            raise ValueError("Document did not contain searchable text.")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        document_id = str(uuid.uuid4())
        safe_metadata = metadata if isinstance(metadata, dict) else {}
        with self._db() as db:
            existing = db.execute(
                "SELECT id FROM documents WHERE collection_name=? AND content_hash=? LIMIT 1",
                (collection, digest),
            ).fetchone()
            if existing:
                count = db.execute(
                    "SELECT COUNT(*) AS n FROM chunks WHERE document_id=?", (existing["id"],)
                ).fetchone()["n"]
                return {
                    "document_id": existing["id"],
                    "chunk_count": count,
                    "duplicate": True,
                    "content_hash": digest,
                }
            db.execute(
                """INSERT INTO documents
                   (id,title,source_uri,collection_name,content_hash,metadata_json,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    document_id, title.strip(), source_uri, collection, digest,
                    json.dumps(safe_metadata, ensure_ascii=False)[:12000], _now(),
                ),
            )
            for index, part in enumerate(parts):
                chunk_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO chunks(id,document_id,chunk_index,content) VALUES (?,?,?,?)",
                    (chunk_id, document_id, index, part),
                )
                if self.fts_available:
                    db.execute(
                        "INSERT INTO chunks_fts(chunk_id,content) VALUES (?,?)",
                        (chunk_id, part),
                    )
        return {
            "document_id": document_id,
            "chunk_count": len(parts),
            "duplicate": False,
            "content_hash": digest,
        }

    def search(self, query: str, *, collection: str = "default", limit: int = 5) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("Search query must contain 1 to 2000 characters.")
        limit = max(1, min(int(limit), 10))
        query_tokens = _tokens(query)[:24]
        if not query_tokens:
            return []
        with self._db() as db:
            rows = db.execute(
                """SELECT c.id AS chunk_id,c.chunk_index,c.content,d.id AS document_id,
                          d.title,d.source_uri,d.collection_name
                   FROM chunks c JOIN documents d ON d.id=c.document_id
                   WHERE d.collection_name=?""",
                (collection,),
            ).fetchall()
            if not rows:
                return []

            lexical_rank: dict[str, int] = {}
            if self.fts_available:
                match = " OR ".join('"' + token.replace('"', '""') + '"' for token in query_tokens)
                try:
                    hits = db.execute(
                        """SELECT c.id AS chunk_id
                           FROM chunks_fts
                           JOIN chunks c ON c.id=chunks_fts.chunk_id
                           JOIN documents d ON d.id=c.document_id
                           WHERE chunks_fts MATCH ? AND d.collection_name=?
                           ORDER BY bm25(chunks_fts) ASC LIMIT 100""",
                        (match, collection),
                    ).fetchall()
                    lexical_rank = {row["chunk_id"]: rank for rank, row in enumerate(hits, 1)}
                except sqlite3.OperationalError:
                    lexical_rank = {}

            if not lexical_rank:
                lexical_scores = []
                for row in rows:
                    haystack = row["content"].casefold()
                    score = sum(haystack.count(token) for token in set(query_tokens))
                    if score:
                        lexical_scores.append((row["chunk_id"], score))
                lexical_scores.sort(key=lambda item: item[1], reverse=True)
                lexical_rank = {chunk_id: rank for rank, (chunk_id, _) in enumerate(lexical_scores, 1)}

            counters = [Counter(_tokens(row["content"])) for row in rows]
            document_frequency: Counter[str] = Counter()
            for counter in counters:
                document_frequency.update(counter.keys())
            total = len(rows)
            idf = {
                token: math.log((total + 1) / (document_frequency[token] + 1)) + 1.0
                for token in set(query_tokens)
            }
            query_counts = Counter(query_tokens)
            query_vector = {
                token: (1.0 + math.log(count)) * idf[token]
                for token, count in query_counts.items()
            }
            query_norm = math.sqrt(sum(value * value for value in query_vector.values())) or 1.0
            vector_scores: list[tuple[str, float]] = []
            for row, counter in zip(rows, counters):
                weights = {
                    token: (1.0 + math.log(count)) * idf[token]
                    for token, count in counter.items()
                    if token in idf
                }
                dot = sum(value * query_vector.get(token, 0.0) for token, value in weights.items())
                norm = math.sqrt(sum(value * value for value in weights.values())) or 1.0
                score = dot / (norm * query_norm)
                if score > 0:
                    vector_scores.append((row["chunk_id"], score))
            vector_scores.sort(key=lambda item: item[1], reverse=True)
            vector_rank = {chunk_id: rank for rank, (chunk_id, _) in enumerate(vector_scores, 1)}

            by_id = {row["chunk_id"]: dict(row) for row in rows}
            candidates = set(lexical_rank) | set(vector_rank)
            fused = []
            for chunk_id in candidates:
                score = (1.0 / (60 + lexical_rank[chunk_id]) if chunk_id in lexical_rank else 0.0)
                score += (1.0 / (60 + vector_rank[chunk_id]) if chunk_id in vector_rank else 0.0)
                fused.append((chunk_id, score))
            fused.sort(key=lambda item: item[1], reverse=True)
            return [
                {
                    **by_id[chunk_id],
                    "score": round(score, 6),
                    "lexical_rank": lexical_rank.get(chunk_id),
                    "vector_rank": vector_rank.get(chunk_id),
                }
                for chunk_id, score in fused[:limit]
            ]

    def list_documents(self, collection: str | None = None) -> list[dict[str, Any]]:
        with self._db() as db:
            if collection:
                rows = db.execute(
                    """SELECT d.id,d.title,d.source_uri,d.collection_name,d.created_at,
                              COUNT(c.id) AS chunk_count
                       FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                       WHERE d.collection_name=?
                       GROUP BY d.id ORDER BY d.created_at DESC LIMIT 200""",
                    (collection,),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT d.id,d.title,d.source_uri,d.collection_name,d.created_at,
                              COUNT(c.id) AS chunk_count
                       FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                       GROUP BY d.id ORDER BY d.created_at DESC LIMIT 200"""
                ).fetchall()
            return [dict(row) for row in rows]

    def get_document(self, document_id: str, max_chars: int = 20000) -> dict[str, Any] | None:
        with self._db() as db:
            doc = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if not doc:
                return None
            chunks = db.execute(
                "SELECT content FROM chunks WHERE document_id=? ORDER BY chunk_index",
                (document_id,),
            ).fetchall()
            content = "\n\n".join(row["content"] for row in chunks)[:max(1, min(max_chars, 50000))]
            return {
                "id": doc["id"], "title": doc["title"], "source_uri": doc["source_uri"],
                "collection": doc["collection_name"], "metadata": json.loads(doc["metadata_json"]),
                "created_at": doc["created_at"], "content": content,
            }

    def delete_document(self, document_id: str) -> bool:
        with self._db() as db:
            if self.fts_available:
                db.execute(
                    "DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)",
                    (document_id,),
                )
            cursor = db.execute("DELETE FROM documents WHERE id=?", (document_id,))
            return cursor.rowcount > 0

    def create_conversation(self, title: str = "Percakapan baru") -> str:
        conversation_id = str(uuid.uuid4())
        now = _now()
        with self._db() as db:
            db.execute(
                "INSERT INTO conversations(id,title,created_at,updated_at) VALUES (?,?,?,?)",
                (conversation_id, title[:80], now, now),
            )
        return conversation_id

    def list_conversations(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                """SELECT c.id,c.title,c.created_at,c.updated_at,
                          (SELECT content FROM messages m WHERE m.conversation_id=c.id
                           ORDER BY m.id DESC LIMIT 1) AS last_message
                   FROM conversations c ORDER BY c.updated_at DESC LIMIT 100"""
            ).fetchall()
            return [dict(row) for row in rows]

    def append_message(self, conversation_id: str, role: str, content: str) -> None:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Unsupported message role.")
        if not isinstance(content, str) or len(content) > 30000:
            raise ValueError("Message must not exceed 30000 characters.")
        now = _now()
        with self._db() as db:
            exists = db.execute(
                "SELECT title FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if not exists:
                raise KeyError("Conversation not found.")
            db.execute(
                "INSERT INTO messages(conversation_id,role,content,created_at) VALUES (?,?,?,?)",
                (conversation_id, role, content, now),
            )
            if role == "user" and exists["title"] == "Percakapan baru":
                title = re.sub(r"\s+", " ", content).strip()[:72] or "Percakapan baru"
                db.execute(
                    "UPDATE conversations SET title=?,updated_at=? WHERE id=?",
                    (title, now, conversation_id),
                )
            else:
                db.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?",
                    (now, conversation_id),
                )

    def get_messages(self, conversation_id: str, limit: int = 30) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                """SELECT role,content,created_at FROM messages
                   WHERE conversation_id=? ORDER BY id DESC LIMIT ?""",
                (conversation_id, max(1, min(limit, 100))),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._db() as db:
            cursor = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            return cursor.rowcount > 0

    def stats(self) -> dict[str, int]:
        with self._db() as db:
            documents = db.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            chunks = db.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
            conversations = db.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
            return {"documents": documents, "chunks": chunks, "conversations": conversations}
