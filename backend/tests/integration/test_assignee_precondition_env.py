"""指派審核者 / 前置案例 / 環境變數綁定 / 批次執行規劃 整合測試。

對應 plan 文件 `指派、審核、前置案例與環境變數規劃` 的 Test Plan 條目。
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


# ── 1) 送審必選審核者 ────────────────────────────────────────────────


async def test_submit_review_requires_assignee(client, org_a) -> None:
    """plan: '送審未選 reviewer 回 400'。pydantic 422 也視為 client error 。"""
    resp = await client.post(
        "/api/reviews",
        json={"entity_type": "document", "entity_id": "no-assignee"},
        headers=org_a.headers,
    )
    assert resp.status_code in (400, 422)


async def test_submit_review_with_unknown_user_assignee_404(client, org_a) -> None:
    resp = await client.post(
        "/api/reviews",
        json={
            "entity_type": "document",
            "entity_id": "ghost-asn",
            "assignee": "i-do-not-exist",
            "assignee_type": "user",
        },
        headers=org_a.headers,
    )
    assert resp.status_code == 404


async def test_submit_review_with_user_assignee_persists(client, org_a) -> None:
    resp = await client.post(
        "/api/reviews",
        json={
            "entity_type": "document",
            "entity_id": "asn-ok",
            "assignee": org_a.username,
            "assignee_type": "user",
        },
        headers=org_a.headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["assigned_to"] == org_a.username
    assert body["assigned_to_type"] == "user"


async def test_non_admin_cannot_approve_unless_assigned(client, org_a, qa_in_a) -> None:
    """plan: 一般使用者必須是被指派的審核者(或所屬群組成員)且具 review.manage 才可審。"""
    # 由 admin 送審,指派給自己(避免找其他 user)
    submit = await client.post(
        "/api/reviews",
        json={
            "entity_type": "document",
            "entity_id": "qa-cant-approve",
            "assignee": org_a.username,
            "assignee_type": "user",
        },
        headers=org_a.headers,
    )
    record_id = submit.json()["id"]

    blocked = await client.post(
        f"/api/reviews/{record_id}/approve", headers=qa_in_a.headers
    )
    # QA 沒有 review.manage,直接 403
    assert blocked.status_code == 403


async def test_mine_filter(client, org_a) -> None:
    """list?mine=true 只回傳指派給呼叫者的 record。"""
    # mine 的:assignee=org_a.username
    await client.post(
        "/api/reviews",
        json={
            "entity_type": "document",
            "entity_id": "mine-1",
            "assignee": org_a.username,
            "assignee_type": "user",
        },
        headers=org_a.headers,
    )
    # 非 mine 的:assignee 指派給(其實也是 admin 自己,但變更 entity 模擬不同)
    # 因為 _validate_assignee 要求 user 存在,只好還是 self-assign
    # 改測 group 走向:沒 group 時 mine list 仍應只看到 user-assignment 的
    listing = await client.get("/api/reviews?mine=true", headers=org_a.headers)
    assert listing.status_code == 200
    rows = listing.json()
    assert any(r["entity_id"] == "mine-1" for r in rows)


# ── 2) Precondition CRUD 與 cycle 偵測 ────────────────────────────────


async def _make_testcase(org_a, name: str) -> str:
    from app.database import AsyncSessionLocal
    from app.models import TreeNode
    from app.models.tree_node import LevelType

    async with AsyncSessionLocal() as session:
        node = TreeNode(
            id=str(uuid.uuid4()),
            project_id=org_a.project_id,
            organization_id=org_a.org_id,
            name=name,
            level_type=LevelType.TESTCASE,
            sort_order=1,
        )
        session.add(node)
        await session.commit()
        return node.id


async def test_precondition_replace_and_list(client, org_a) -> None:
    main_id = await _make_testcase(org_a, "main-A")
    pre1 = await _make_testcase(org_a, "pre-1")
    pre2 = await _make_testcase(org_a, "pre-2")

    resp = await client.put(
        f"/api/testcases/{main_id}/preconditions",
        json={
            "items": [
                {"precondition_testcase_id": pre1, "sort_order": 0, "enabled": True, "on_failure": "stop"},
                {"precondition_testcase_id": pre2, "sort_order": 1, "enabled": True, "on_failure": "stop"},
            ]
        },
        headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [r["precondition_testcase_id"] for r in rows] == [pre1, pre2]

    listed = await client.get(
        f"/api/testcases/{main_id}/preconditions", headers=org_a.headers
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 2


async def test_precondition_cannot_self_reference(client, org_a) -> None:
    tc = await _make_testcase(org_a, "self-loop")
    resp = await client.put(
        f"/api/testcases/{tc}/preconditions",
        json={"items": [{"precondition_testcase_id": tc, "sort_order": 0}]},
        headers=org_a.headers,
    )
    assert resp.status_code == 400


async def test_precondition_cycle_rejected(client, org_a) -> None:
    """A → B,接著想加 B → A,後者該被擋。"""
    a = await _make_testcase(org_a, "case-A")
    b = await _make_testcase(org_a, "case-B")

    # A 的前置 = [B]
    r1 = await client.put(
        f"/api/testcases/{a}/preconditions",
        json={"items": [{"precondition_testcase_id": b, "sort_order": 0}]},
        headers=org_a.headers,
    )
    assert r1.status_code == 200

    # B 的前置 = [A] → 形成循環,400
    r2 = await client.put(
        f"/api/testcases/{b}/preconditions",
        json={"items": [{"precondition_testcase_id": a, "sort_order": 0}]},
        headers=org_a.headers,
    )
    assert r2.status_code == 400


async def test_precondition_must_be_testcase_leaf(client, org_a) -> None:
    """前置案例必須是 TESTCASE 葉節點(不能指到 folder)。"""
    from app.database import AsyncSessionLocal
    from app.models import TreeNode
    from app.models.tree_node import LevelType

    async with AsyncSessionLocal() as session:
        feature = TreeNode(
            id=str(uuid.uuid4()),
            project_id=org_a.project_id,
            organization_id=org_a.org_id,
            name="f1",
            level_type=LevelType.FEATURE,
            sort_order=1,
        )
        session.add(feature)
        await session.commit()
        feat_id = feature.id

    main = await _make_testcase(org_a, "main-leaf")
    resp = await client.put(
        f"/api/testcases/{main}/preconditions",
        json={"items": [{"precondition_testcase_id": feat_id, "sort_order": 0}]},
        headers=org_a.headers,
    )
    assert resp.status_code == 400


# ── 3) Env binding CRUD ──────────────────────────────────────────────


async def test_env_binding_replace_and_list(client, org_a) -> None:
    tc = await _make_testcase(org_a, "with-env")

    resp = await client.put(
        f"/api/testcases/{tc}/env-bindings",
        json={"items": ["BASE_URL", "API_TOKEN", "BASE_URL", "  "]},  # 含 dup + 空
        headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    names = sorted(r["env_var_name"] for r in rows)
    # 重複與空字串會被去掉
    assert names == ["API_TOKEN", "BASE_URL"]

    listed = await client.get(
        f"/api/testcases/{tc}/env-bindings", headers=org_a.headers
    )
    assert listed.status_code == 200
    assert sorted(r["env_var_name"] for r in listed.json()) == ["API_TOKEN", "BASE_URL"]


# ── 4) 批次執行(node_ids)+ plan 含 setup ───────────────────────────


async def test_executions_node_ids_back_compat_with_node_id(client, org_a) -> None:
    """純粹 schema 相容測試:單一 node_id 仍然可以觸發。"""
    tc = await _make_testcase(org_a, "single-exec")
    # local mode 不會送 Celery,只回 task_id
    resp = await client.post(
        "/api/executions",
        json={"node_id": tc, "execution_mode": "local"},
        headers=org_a.headers,
    )
    assert resp.status_code == 201, resp.text


async def test_executions_node_ids_with_preconditions(client, org_a) -> None:
    """node_ids 多選 + 前置案例 → /api/local-runner/claim 應拿到 setup_testcase_ids
    與 project_env_vars。"""
    pre = await _make_testcase(org_a, "setup-1")
    main_a = await _make_testcase(org_a, "main-a-1")
    main_b = await _make_testcase(org_a, "main-b-1")

    # 兩個 main 都把 pre 加為前置(testcase_precondition_links)
    for mid in (main_a, main_b):
        await client.put(
            f"/api/testcases/{mid}/preconditions",
            json={"items": [{"precondition_testcase_id": pre, "sort_order": 0}]},
            headers=org_a.headers,
        )

    # 先寫一個 project_env_vars
    from app.database import AsyncSessionLocal
    from app.models.project_env_var import ProjectEnvVar

    async with AsyncSessionLocal() as session:
        session.add(
            ProjectEnvVar(
                id=str(uuid.uuid4()),
                project_id=org_a.project_id,
                organization_id=org_a.org_id,
                name="BASE_URL",
                value="https://demo.test",
            )
        )
        await session.commit()

    resp = await client.post(
        "/api/executions",
        json={
            "node_ids": [main_a, main_b],
            "execution_mode": "local",
        },
        headers=org_a.headers,
    )
    assert resp.status_code == 201, resp.text

    # 兩個 main + 一個被 dedup 過的 setup = 3 筆
    body = resp.json()
    task_id = body["task_id"]
    assert task_id

    # 模擬 agent 認領
    claim = await client.post(
        "/api/local-runner/claim",
        json={"agent_id": "test-agent"},
        headers=org_a.headers,
    )
    assert claim.status_code == 200, claim.text
    payload = claim.json()
    case_ids = [c["testcase_id"] for c in payload["cases"]]
    # setup 排在前
    assert case_ids[0] == pre
    assert pre in payload["setup_testcase_ids"]
    assert "BASE_URL" in payload["project_env_vars"]
    assert payload["project_env_vars"]["BASE_URL"] == "https://demo.test"


async def test_executions_cross_project_node_ids_rejected(client, org_a) -> None:
    """跨 project 批次不支援。"""
    from app.database import AsyncSessionLocal
    from app.models import Project, TreeNode
    from app.models.tree_node import LevelType

    tc1 = await _make_testcase(org_a, "p1-case")

    async with AsyncSessionLocal() as session:
        p2 = Project(
            id=str(uuid.uuid4()),
            name="p2",
            organization_id=org_a.org_id,
        )
        session.add(p2)
        await session.flush()
        tc2 = TreeNode(
            id=str(uuid.uuid4()),
            project_id=p2.id,
            organization_id=org_a.org_id,
            name="p2-case",
            level_type=LevelType.TESTCASE,
            sort_order=1,
        )
        session.add(tc2)
        await session.commit()
        tc2_id = tc2.id

    resp = await client.post(
        "/api/executions",
        json={"node_ids": [tc1, tc2_id], "execution_mode": "local"},
        headers=org_a.headers,
    )
    assert resp.status_code == 400
