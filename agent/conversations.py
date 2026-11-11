"""Version-bound conversations, using Pydantic AI's native message serializer."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
from uuid import UUID, uuid4

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from agent.evidence import CheckedAnswer
from config import CHROMA_DIR
from indexer.snapshots import normalize_repo_url


class ConversationConflict(ValueError):
    """Another completed run has already advanced this conversation."""


@dataclass(frozen=True)
class ConversationTurn:
    question: str
    messages: tuple[ModelMessage, ...]
    answer: CheckedAnswer


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    repo_url: str
    commit_sha: str
    collection_name: str
    revision: int
    reference_count: int
    turns: tuple[ConversationTurn, ...]

    @property
    def messages(self):
        return [message for turn in self.turns for message in turn.messages]


def _scope(repo_url, commit_sha, collection_name):
    url = normalize_repo_url(repo_url)
    if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ValueError("对话需要完整的代码版本。")
    if not isinstance(collection_name, str) or not collection_name.strip():
        raise ValueError("对话需要已选定的索引集合。")
    return url, commit_sha, collection_name


def _session_id(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("对话编号无效。") from None
    return value


@contextmanager
def _connect():
    path = Path(CHROMA_DIR) / "conversations.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, repo_url TEXT NOT NULL,
                commit_sha TEXT NOT NULL, collection_name TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                reference_count INTEGER NOT NULL DEFAULT 0,
                format_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS conversation_turns (
                conversation_id TEXT NOT NULL, revision INTEGER NOT NULL,
                question TEXT NOT NULL, messages_json TEXT NOT NULL,
                answer_json TEXT NOT NULL,
                PRIMARY KEY (conversation_id, revision)
            );
        """)
        with db:
            yield db
    finally:
        db.close()


def create_conversation(repo_url: str, commit_sha: str, collection_name: str) -> str:
    scope = _scope(repo_url, commit_sha, collection_name)
    conversation_id = str(uuid4())
    with _connect() as db:
        db.execute("INSERT INTO conversations (id, repo_url, commit_sha, collection_name) VALUES (?, ?, ?, ?)",
                   (conversation_id, *scope))
    return conversation_id


def load_conversation(conversation_id: str, repo_url: str, commit_sha: str, collection_name: str) -> Conversation:
    scope = _scope(repo_url, commit_sha, collection_name)
    with _connect() as db:
        # One read transaction keeps metadata and turns at the same revision.
        db.execute("BEGIN")
        row = db.execute("SELECT repo_url, commit_sha, collection_name, revision, reference_count, format_version "
                         "FROM conversations WHERE id=?", (_session_id(conversation_id),)).fetchone()
        if row is None:
            raise ValueError("对话不存在。")
        if row[:3] != scope:
            raise ValueError("对话与当前仓库、代码版本或索引不一致。请新建对话。")
        if row[5] != 1:
            raise ValueError("对话记录格式不受支持。")
        records = db.execute("SELECT revision, question, messages_json, answer_json FROM conversation_turns "
                             "WHERE conversation_id=? ORDER BY revision", (conversation_id,)).fetchall()
    if [record[0] for record in records] != list(range(1, row[3] + 1)):
        raise ValueError("对话记录不完整。")
    turns = tuple(ConversationTurn(record[1], tuple(ModelMessagesTypeAdapter.validate_json(record[2])),
                                   CheckedAnswer.model_validate_json(record[3])) for record in records)
    return Conversation(conversation_id, *scope, row[3], row[4], turns)


def append_turn(conversation: Conversation, question: str, messages: list[ModelMessage],
                answer: CheckedAnswer, reference_count: int) -> None:
    """Commit one complete turn, or leave the previous conversation unchanged."""
    if not isinstance(question, str) or not question.strip() or not messages:
        raise ValueError("不能保存空的对话轮次。")
    if type(reference_count) is not int or reference_count < conversation.reference_count:
        raise ValueError("源码引用序号无效。")
    messages_json = ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")
    # Validate before opening the write transaction; never use pickle or eval.
    ModelMessagesTypeAdapter.validate_json(messages_json)
    answer_json = answer.model_dump_json()
    with _connect() as db:
        changed = db.execute("UPDATE conversations SET revision=revision+1, reference_count=? "
                             "WHERE id=? AND repo_url=? AND commit_sha=? AND collection_name=? AND revision=?",
                             (reference_count, conversation.conversation_id, conversation.repo_url,
                              conversation.commit_sha, conversation.collection_name, conversation.revision)).rowcount
        if changed != 1:
            raise ConversationConflict("对话已被其他请求更新，请重新提问。")
        db.execute("INSERT INTO conversation_turns VALUES (?, ?, ?, ?, ?)",
                   (conversation.conversation_id, conversation.revision + 1, question, messages_json, answer_json))
