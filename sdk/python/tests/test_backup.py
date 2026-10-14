import asyncio

from bianbu_agent_sdk.memory.backup import AsyncBackupAPI, BackupAPI


class Client:
    def __init__(self) -> None:
        self.calls = []

    def _call(self, method, params, **kwargs):
        self.calls.append((method, params, kwargs))
        return [] if method == "backup.list" else {"method": method}


class AsyncClient(Client):
    async def _call(self, method, params, **kwargs):
        return super()._call(method, params, **kwargs)


def test_backup_api_forwards_scope_filters_and_idempotency() -> None:
    client = Client()
    api = BackupAPI(client)
    api.create(scopes=["preferences"], idempotency_key="create-1")
    api.run_scheduled()
    api.list(limit=10, device_id="k3-a")
    api.restore("backup-1", confirm_token="confirmed", idempotency_key="restore-1")
    assert client.calls == [
        ("backup.create", {"scopes": ["preferences"]}, {"idempotency_key": "create-1"}),
        ("backup.run_scheduled", {"force": False}, {}),
        ("backup.list", {"limit": 10, "device_id": "k3-a"}, {}),
        (
            "backup.restore",
            {"backup_id": "backup-1", "confirm_token": "confirmed"},
            {"idempotency_key": "restore-1"},
        ),
    ]


def test_async_backup_api_matches_sync_contract() -> None:
    async def scenario() -> None:
        client = AsyncClient()
        api = AsyncBackupAPI(client)
        await api.create(scopes=("summaries",), idempotency_key="create-async")
        await api.run_scheduled(force=True)
        assert client.calls[0] == (
            "backup.create",
            {"scopes": ["summaries"]},
            {"idempotency_key": "create-async"},
        )
        assert client.calls[1] == ("backup.run_scheduled", {"force": True}, {})

    asyncio.run(scenario())
