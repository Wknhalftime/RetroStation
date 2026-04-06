"""MCP server for LEANN codebase search over RetroStation.

Exposes a `leann_search` tool that Claude Code can call natively.
Uses the LeannSearcher API directly (no CLI dependency).

Run via: python scripts/leann_mcp_server.py
"""

import json
import sys
from pathlib import Path

INDEX_PATH = "D:/PythonStuff/leann/retrostation-index/code_index.leann"

# Lazy-loaded searcher (heavy import)
_searcher = None


def get_searcher():
    global _searcher
    if _searcher is None:
        from leann import LeannSearcher
        _searcher = LeannSearcher(INDEX_PATH)
    return _searcher


def handle_request(request):
    method = request.get("method")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "capabilities": {"tools": {}},
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "leann-retrostation", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "leann_search",
                        "description": (
                            "Semantic search over the RetroStation codebase "
                            "(Python/FastAPI backend + React/TypeScript frontend). "
                            "Returns relevant code chunks ranked by similarity. "
                            "Use for finding implementations, patterns, and code references."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "Natural language search query (e.g., 'how does the matching pipeline work', 'broadcast_days queries', 'API endpoints')",
                                },
                                "top_k": {
                                    "type": "integer",
                                    "default": 5,
                                    "minimum": 1,
                                    "maximum": 20,
                                    "description": "Number of results to return",
                                },
                            },
                            "required": ["query"],
                        },
                    },
                ]
            },
        }

    if method == "tools/call":
        tool_name = request["params"]["name"]
        args = request["params"].get("arguments", {})

        if tool_name == "leann_search":
            query = args.get("query", "")
            top_k = args.get("top_k", 5)

            if not query:
                return _error_result(req_id, "query is required")

            if not Path(INDEX_PATH).exists():
                return _error_result(req_id, f"Index not found at {INDEX_PATH}. Run the initial build first.")

            try:
                searcher = get_searcher()
                results = searcher.search(query, top_k=top_k)

                if not results:
                    text = f"No results found for: {query}"
                else:
                    lines = []
                    for i, r in enumerate(results, 1):
                        meta = getattr(r, "metadata", {}) or {}
                        file_path = meta.get("file_path", "unknown")
                        lines.append(f"## Result {i} (score: {r.score:.4f})")
                        if file_path != "unknown":
                            lines.append(f"**File:** {file_path}")
                        lines.append(f"```\n{r.text}\n```")
                        lines.append("")
                    text = "\n".join(lines)

                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": text}]},
                }
            except Exception as e:
                return _error_result(req_id, str(e))

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def _error_result(req_id, message):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": f"Error: {message}"}]},
    }


def main():
    # Suppress noisy logs from LEANN/torch to stderr only
    import logging
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(request)
            if response:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            pass
        except Exception as e:
            error_response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -1, "message": str(e)},
            }
            sys.stdout.write(json.dumps(error_response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
