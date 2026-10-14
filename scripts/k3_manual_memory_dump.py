#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from bianbu_agent_sdk.memory import MemoryClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the K3 manual-test memory database")
    parser.add_argument("socket_path")
    parser.add_argument("--query", default="")
    parser.add_argument("--top-k", type=int, default=100)
    arguments = parser.parse_args()

    with MemoryClient.connect(arguments.socket_path, timeout=5.0) as client:
        headers = client.long_term.search_headers(
            arguments.query, top_k=max(1, min(arguments.top_k, 100))
        )
        memories = client.long_term.get_many([item.memory_id for item in headers])
        print(
            json.dumps(
                {
                    "identity": {
                        "account_id": client.identity.account_id,
                        "agent_id": client.identity.agent_id,
                        "memory_space_id": client.identity.memory_space_id,
                    },
                    "query": arguments.query,
                    "count": len(memories),
                    "memories": [
                        {
                            "memory_id": item.memory_id,
                            "kind": item.kind.value,
                            "text": item.text,
                            "revision": item.revision,
                            "tags": list(item.tags),
                            "created_at": item.created_at,
                            "updated_at": item.updated_at,
                        }
                        for item in memories
                    ],
                    "stats": client.stats(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
