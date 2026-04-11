"""Incremental LEANN reindex for RetroStation codebase.

Called by the PostToolUse hook after git commits.
- If no index exists: full build (non-compact, for future incremental updates)
- If index exists and is non-compact: incremental update of changed files only
- If index exists but is compact: full rebuild (non-compact)

Uses all-MiniLM-L6-v2 for fast CPU embeddings.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# ── Single-instance guard + debounce (Windows-safe) ──────────
LOCK_FILE = Path("D:/PythonStuff/leann/retrostation-index/.reindex.lock")
TIMESTAMP_FILE = Path("D:/PythonStuff/leann/retrostation-index/.reindex.last")
DEBOUNCE_SECONDS = 300  # 5 minutes


def _acquire_lock() -> None:
    """Kill any previous reindex process and write our PID to the lock file."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    # If a previous instance left a lock, try to kill it
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            if old_pid != os.getpid():
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(old_pid)],
                    capture_output=True,
                    timeout=5,
                )
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass  # stale lock or process already gone

    LOCK_FILE.write_text(str(os.getpid()))


def _release_lock() -> None:
    """Remove the lock file if it still belongs to us."""
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass


def _should_skip_debounce() -> bool:
    """Return True if last successful reindex was within DEBOUNCE_SECONDS."""
    if not TIMESTAMP_FILE.exists():
        return False
    try:
        last_run = float(TIMESTAMP_FILE.read_text().strip())
        import time
        elapsed = time.time() - last_run
        if elapsed < DEBOUNCE_SECONDS:
            print(f"[leann] Skipping reindex — last run {int(elapsed)}s ago (debounce {DEBOUNCE_SECONDS}s)")
            return True
    except (ValueError, OSError):
        pass
    return False


def _record_timestamp() -> None:
    """Record the current time as last successful reindex."""
    import time
    try:
        TIMESTAMP_FILE.write_text(str(time.time()))
    except OSError:
        pass

# ── Configuration ──────────────────────────────────────────────
LEANN_ROOT = Path("D:/PythonStuff/leann")
REPO_ROOT = Path("D:/PythonStuff/RetroStation")
INDEX_DIR = LEANN_ROOT / "retrostation-index"
INDEX_PATH = INDEX_DIR / "code_index.leann"
META_PATH = INDEX_DIR / "code_index.leann.meta.json"

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_MODE = "sentence-transformers"
BACKEND = "hnsw"

INCLUDE_EXTENSIONS = {".py", ".ts", ".tsx", ".sql", ".md"}
EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "dist", ".next", ".claude"}
MAX_FILE_SIZE = 1_000_000  # 1MB

# Add LEANN to path
sys.path.insert(0, str(LEANN_ROOT))
sys.path.insert(0, str(LEANN_ROOT / "apps"))

os.environ["LEANN_EMBEDDING_DEVICE"] = "cpu"


def get_changed_files() -> list[Path]:
    """Get files changed in the most recent commit via git diff."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", "HEAD~1", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []

        changed = []
        for line in result.stdout.strip().splitlines():
            p = REPO_ROOT / line
            if not p.exists():
                continue
            if p.suffix not in INCLUDE_EXTENSIONS:
                continue
            if any(part in EXCLUDE_DIRS for part in p.parts):
                continue
            if p.stat().st_size > MAX_FILE_SIZE:
                continue
            changed.append(p)
        return changed
    except Exception:
        return []


def read_file_content(path: Path) -> str:
    """Read file content safely."""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def index_is_non_compact() -> bool:
    """Check if existing index supports incremental updates (non-compact, no-recompute)."""
    if not META_PATH.exists():
        return False
    try:
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        is_compact = meta.get("is_compact", True)
        bk = meta.get("backend_kwargs", {})
        is_recompute = bk.get("is_recompute", True)
        return not is_compact and not is_recompute
    except Exception:
        return False


def index_uses_correct_model() -> bool:
    """Check if existing index uses our target embedding model."""
    if not META_PATH.exists():
        return False
    try:
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        return meta.get("embedding_model", "") == EMBEDDING_MODEL
    except Exception:
        return False


def do_incremental_update(changed_files: list[Path]) -> None:
    """Add only changed files to existing index."""
    from leann.api import LeannBuilder
    from leann.chunking_utils import create_text_chunks

    print(f"[leann] Incremental update: {len(changed_files)} changed files")

    # Create fake document objects matching llama_index Document interface
    class FakeDoc:
        def __init__(self, text: str, metadata: dict):
            self.text = text
            self.metadata = metadata

    docs = []
    for f in changed_files:
        content = read_file_content(f)
        if content.strip():
            docs.append(FakeDoc(text=content, metadata={"file_path": str(f)}))

    if not docs:
        print("[leann] No content to update")
        return

    chunks = create_text_chunks(
        docs,
        chunk_size=256,
        chunk_overlap=64,
        use_ast_chunking=True,
        ast_chunk_size=300,
        ast_chunk_overlap=64,
        code_file_extensions=list(INCLUDE_EXTENSIONS),
        ast_fallback_traditional=True,
    )

    if not chunks:
        print("[leann] No chunks generated from changed files")
        return

    builder = LeannBuilder(
        backend_name=BACKEND,
        embedding_model=EMBEDDING_MODEL,
        embedding_mode=EMBEDDING_MODE,
        is_compact=False,
        is_recompute=False,
    )

    for chunk in chunks:
        if isinstance(chunk, dict):
            builder.add_text(chunk["text"], metadata=chunk.get("metadata", {}))
        elif isinstance(chunk, str):
            builder.add_text(chunk)

    try:
        builder.update_index(str(INDEX_PATH))
        print(f"[leann] Incremental update complete: {len(chunks)} chunks added")
    except ValueError as e:
        if "already exists" in str(e):
            print(f"[leann] Duplicate chunk IDs detected, skipping: {e}")
        else:
            raise


def do_full_rebuild() -> None:
    """Full rebuild with non-compact index for future incremental updates."""
    from leann.api import LeannBuilder
    from leann.chunking_utils import create_text_chunks
    from llama_index.core import SimpleDirectoryReader

    print("[leann] Full rebuild (non-compact for incremental updates)")

    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    documents = SimpleDirectoryReader(
        str(REPO_ROOT),
        recursive=True,
        encoding="utf-8",
        required_exts=list(INCLUDE_EXTENSIONS),
        exclude_hidden=True,
    ).load_data(show_progress=False)

    # Filter
    filtered = []
    for doc in documents:
        fp = doc.metadata.get("file_path", "")
        p = Path(fp)
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        try:
            if p.stat().st_size > MAX_FILE_SIZE:
                continue
        except Exception:
            continue
        filtered.append(doc)

    print(f"[leann] Loaded {len(filtered)} files")

    chunks = create_text_chunks(
        filtered,
        chunk_size=256,
        chunk_overlap=64,
        use_ast_chunking=True,
        ast_chunk_size=300,
        ast_chunk_overlap=64,
        code_file_extensions=list(INCLUDE_EXTENSIONS),
        ast_fallback_traditional=True,
    )

    print(f"[leann] Generated {len(chunks)} chunks")

    builder = LeannBuilder(
        backend_name=BACKEND,
        embedding_model=EMBEDDING_MODEL,
        embedding_mode=EMBEDDING_MODE,
        is_compact=False,
        is_recompute=False,
    )

    for chunk in chunks:
        if isinstance(chunk, dict):
            builder.add_text(chunk["text"], metadata=chunk.get("metadata", {}))
        elif isinstance(chunk, str):
            builder.add_text(chunk)

    builder.build_index(str(INDEX_PATH))
    print(f"[leann] Full rebuild complete: {len(chunks)} chunks indexed")


def main() -> None:
    if _should_skip_debounce():
        return

    _acquire_lock()
    try:
        can_incremental = (
            INDEX_PATH.exists()
            and index_is_non_compact()
            and index_uses_correct_model()
        )

        if can_incremental:
            changed = get_changed_files()
            if changed:
                do_incremental_update(changed)
                _record_timestamp()
            else:
                print("[leann] No relevant files changed, skipping")
        else:
            reason = "no index" if not INDEX_PATH.exists() else "compact index or model mismatch"
            print(f"[leann] Full rebuild needed ({reason})")
            do_full_rebuild()
            _record_timestamp()
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
