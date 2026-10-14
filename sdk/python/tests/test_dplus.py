from types import SimpleNamespace

from bianbu_agent_sdk.memory.dplus import DPlusProjector, preference_slot


def test_profile_name_wording_uses_one_stable_slot() -> None:
    assert preference_slot("我是小圆") == "profile.user_name"
    assert preference_slot('用户自称"小圆"（可能为昵称）') == "profile.user_name"
    assert preference_slot("用户名称为 Acceptance User 742。") == "profile.user_name"
    assert preference_slot("你还记得我是谁吗") is None
    assert preference_slot("我叫什么名字") is None


def test_product_preference_slots_merge_terminal_and_system_color_wording() -> None:
    assert preference_slot("终端配色偏好是浅色。") == (
        "appearance.terminal_color_scheme"
    )
    assert preference_slot("系统偏好颜色改为深色。") == (
        "appearance.terminal_color_scheme"
    )
    assert preference_slot("桌面壁纸偏好是浅色。") == (
        "appearance.wallpaper_color_scheme"
    )


def test_development_slots_classify_language_package_manager_and_indentation() -> None:
    assert preference_slot("我习惯使用 Rust 进行开发") == (
        "development.programming_language"
    )
    assert preference_slot("我使用 Cargo 进行依赖管理") == (
        "development.package_manager"
    )
    assert preference_slot("代码缩进使用 4 个空格") == "development.indentation"


def test_extended_development_slots_cover_versions_templates_tools_and_commands() -> None:
    assert preference_slot("Python 版本固定为 3.13") == "development.runtime_version.python"
    assert preference_slot("项目模板默认使用 FastAPI 脚手架") == "development.project_template"
    assert preference_slot("测试框架使用 pytest") == "development.test_framework"
    assert preference_slot("Rust 格式化工具使用 rustfmt") == "development.formatter"
    assert preference_slot("代码静态检查使用 clippy") == "development.linter"
    assert preference_slot("常用测试命令是 cargo test --workspace") == "development.command.test"
    assert preference_slot("编辑器默认使用 VSCode") == "development.editor"
    assert preference_slot("默认 shell 使用 PowerShell") == "development.shell"


def test_browser_wording_uses_one_stable_slot() -> None:
    assert preference_slot("常用浏览器为 Firefox。") == "application.default_browser"
    assert preference_slot("我常用 Chromium，不是 Firefox") == (
        "application.default_browser"
    )


def test_projection_converges_browser_result_onto_explicit_profile() -> None:
    from bianbu_agent_sdk.memory import MemoryKind

    record = SimpleNamespace(
        memory_id="explicit-browser",
        revision=1,
        sensitivity="sensitive",
        kind=MemoryKind.PROFILE,
        text="常用浏览器为 Firefox。",
        summary=None,
        tags=("personal-agent", "user", "slot:application.default_browser"),
        updated_at=1,
    )

    class LongTerm:
        def __init__(self):
            self.revisions = []

        def search_headers(self, query, top_k):
            del query, top_k
            return [SimpleNamespace(memory_id=record.memory_id)]

        def get_many(self, memory_ids):
            return [record] if record.memory_id in memory_ids else []

        def revise(self, memory_id, **arguments):
            self.revisions.append((memory_id, arguments))
            record.text = arguments["text"]
            record.revision += 1
            record.updated_at += 1
            return SimpleNamespace(memory_id=memory_id, revision=record.revision)

        def put(self, **arguments):
            raise AssertionError(f"unexpected duplicate put: {arguments}")

    class Projection:
        def resolve(self, **kwargs):
            del kwargs
            return []

        def upsert_mapping(self, **kwargs):
            del kwargs

    class Provider:
        config = SimpleNamespace(convergence_threshold=0.86)

        def search(self, identity, text, top_k):
            del identity, text, top_k
            return []

        def delete(self, provider_id):
            del provider_id

    long_term = LongTerm()
    client = SimpleNamespace(
        identity=SimpleNamespace(),
        long_term=long_term,
        projection=Projection(),
    )
    mapping = DPlusProjector(client, Provider())._upsert_canonical_for_result(
        job={"job_id": "job-1"},
        provider_id="provider-browser",
        text="我常用 Chromium，不是 Firefox",
        known=None,
    )

    assert mapping.memory_id == "explicit-browser"
    assert len(long_term.revisions) == 1
    assert record.text == "我常用 Chromium，不是 Firefox"


def test_rebuild_does_not_copy_sensitive_canonical_memory_to_projection() -> None:
    public_record = SimpleNamespace(
        memory_id="public-1",
        revision=1,
        sensitivity="private",
        kind=SimpleNamespace(value="preference"),
        text="terminal theme: dark",
        tags=(),
        updated_at=1,
    )
    sensitive_record = SimpleNamespace(
        memory_id="sensitive-1",
        revision=1,
        sensitivity="sensitive",
        kind=SimpleNamespace(value="profile"),
        text="my name is Test User",
        tags=(),
        updated_at=2,
    )
    private_profile = SimpleNamespace(
        memory_id="profile-1",
        revision=1,
        sensitivity="private",
        kind=SimpleNamespace(value="profile"),
        text="profile nickname",
        tags=(),
        updated_at=3,
    )
    legacy_profile_slot = SimpleNamespace(
        memory_id="profile-slot-1",
        revision=1,
        sensitivity="private",
        kind=SimpleNamespace(value="fact"),
        text="legacy profile name",
        tags=("slot:profile.user_name",),
        updated_at=4,
    )

    class LongTerm:
        def search_headers(self, query, top_k):
            del query, top_k
            return [
                SimpleNamespace(memory_id="public-1"),
                SimpleNamespace(memory_id="sensitive-1"),
                SimpleNamespace(memory_id="profile-1"),
                SimpleNamespace(memory_id="profile-slot-1"),
            ]

        def get_many(self, memory_ids):
            del memory_ids
            return [
                public_record,
                sensitive_record,
                private_profile,
                legacy_profile_slot,
            ]

    class Projection:
        def upsert_mapping(self, **kwargs):
            del kwargs

    class Provider:
        def __init__(self):
            self.added = []

        def _call(self, *args, **kwargs):
            del args, kwargs
            return {"results": []}

        def add_canonical(self, identity, record):
            del identity
            self.added.append(record.memory_id)
            return "provider-" + record.memory_id

    client = SimpleNamespace(
        identity=SimpleNamespace(
            account_id="account", agent_id="agent", memory_space_id="space"
        ),
        long_term=LongTerm(),
        projection=Projection(),
    )
    provider = Provider()

    result = DPlusProjector(client, provider).rebuild()

    assert provider.added == ["public-1"]
    assert result["total"] == 1


def test_clear_scope_deletes_every_provider_record() -> None:
    class Provider:
        def delete_scope(self, identity):
            del identity
            return 4

    client = SimpleNamespace(identity=SimpleNamespace())

    assert DPlusProjector(client, Provider()).clear_scope() == {"deleted": 4}


def test_projection_rejects_question_shaped_provider_memory() -> None:
    class Projection:
        def resolve(self, **kwargs):
            del kwargs
            return []

    class Provider:
        def __init__(self):
            self.deleted = []

        def delete(self, provider_id):
            self.deleted.append(provider_id)

    provider = Provider()
    client = SimpleNamespace(projection=Projection())
    projector = DPlusProjector(client, provider)

    mappings = projector._reconcile(
        {"job_id": "job-question", "session_id": "session-question"},
        {
            "results": [
                {
                    "id": "provider-question",
                    "memory": "我叫什么名字",
                    "event": "ADD",
                }
            ]
        },
    )

    assert mappings == ()
    assert provider.deleted == ["provider-question"]
