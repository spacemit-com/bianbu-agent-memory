import asyncio

from bianbu_agent_sdk.memory.projection import (
    AsyncProjectionAPI,
    ProjectionAPI,
    ProjectionMapping,
)


def test_projection_mapping_serialization_is_protocol_stable() -> None:
    value = ProjectionMapping(
        provider_memory_id="mem0-1",
        memory_id="canonical-1",
        canonical_revision=3,
        last_score=0.91,
    ).to_dict()
    assert value == {
        "provider_memory_id": "mem0-1",
        "memory_id": "canonical-1",
        "canonical_revision": 3,
        "state": "active",
        "last_score": 0.91,
    }


class _SyncClient:
    def __init__(self) -> None:
        self.calls = []

    def _call(self, method, params, idempotency_key=None):
        self.calls.append((method, params, idempotency_key))
        return {"method": method}


def test_projection_dead_job_management_is_protocol_scoped() -> None:
    client = _SyncClient()
    api = ProjectionAPI(client)

    api.list_dead(provider="mem0-oss-v2", limit=25)
    api.requeue_dead(
        "job-1", provider="mem0-oss-v2", idempotency_key="requeue-job-1"
    )

    assert client.calls == [
        (
            "projection.list_dead",
            {"provider": "mem0-oss-v2", "limit": 25},
            None,
        ),
        (
            "projection.requeue_dead",
            {"provider": "mem0-oss-v2", "job_id": "job-1"},
            "requeue-job-1",
        ),
    ]


def test_async_projection_dead_job_management_matches_sync_contract() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        async def _call(self, method, params, idempotency_key=None):
            self.calls.append((method, params, idempotency_key))
            return {"method": method}

    async def exercise() -> list[tuple[object, ...]]:
        client = Client()
        api = AsyncProjectionAPI(client)
        await api.list_dead(provider="mem0-oss-v2", limit=10)
        await api.requeue_dead("job-2", provider="mem0-oss-v2")
        return client.calls

    assert asyncio.run(exercise()) == [
        (
            "projection.list_dead",
            {"provider": "mem0-oss-v2", "limit": 10},
            None,
        ),
        (
            "projection.requeue_dead",
            {"provider": "mem0-oss-v2", "job_id": "job-2"},
            None,
        ),
    ]
