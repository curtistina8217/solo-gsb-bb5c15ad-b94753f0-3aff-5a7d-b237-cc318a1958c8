"""异步审计作业接口 POST/GET/DELETE /api/v1/audit-jobs 的端到端测试。"""

import threading
import time

import pytest

from app import database as db
from app.jobs import JobRunner

GOOD = {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]}
BAD_RANGE = {"dependencies": [{"name": "x", "range": "not-valid"}]}
MISSING_ROOT = {"plugin_name": "ghost", "plugin_version": "9.9.9", "dependencies": []}


def _wait_terminal(client, job_id, timeout=5.0):
    """轮询作业直到进入终结状态（succeeded/failed）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/audit-jobs/{job_id}").json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"作业 {job_id} 在 {timeout}s 内未终结: {body}")


# ---------- 创建与执行 ----------


def test_job_single_manifest_dict_accepted(client):
    """单条清单（裸对象）也能提交，202 + job_id，后台求解落库。"""
    resp = client.post("/api/v1/audit-jobs", json=GOOD)
    assert resp.status_code == 202, resp.text
    created = resp.json()
    assert created["status"] == "queued"
    assert created["total"] == 1
    assert len(created["items"]) == 1
    assert created["items"][0]["status"] == "queued"
    assert created["items"][0]["audit_id"] is None

    job = _wait_terminal(client, created["id"])
    assert job["status"] == "succeeded"
    assert job["completed"] == 1
    assert job["succeeded"] == 1
    assert job["failed"] == 0
    assert job["cancelled"] == 0
    assert job["queued"] == 0
    assert job["running"] == 0
    item = job["items"][0]
    assert item["status"] == "succeeded"
    assert item["audit_id"]
    assert item["audit"]["id"] == item["audit_id"]
    assert item["audit"]["status"] == "feasible"
    assert item["error"] is None

    # 成功项同步进入审计记录表，可经既有详情接口取回
    detail = client.get(f"/api/v1/audits/{item['audit_id']}")
    assert detail.status_code == 200
    assert detail.json()["resolution"]["core-lib"].startswith("1.")


def test_job_batch_mixed_results_item_isolation(client):
    """批量清单：好/坏/缺根插件混合，单项失败不阻断其他项，作业整体 failed。"""
    resp = client.post(
        "/api/v1/audit-jobs",
        json={"manifests": [GOOD, BAD_RANGE, MISSING_ROOT, GOOD]},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["id"]

    job = _wait_terminal(client, job_id)
    assert job["status"] == "failed"  # 存在失败项 -> failed
    assert job["total"] == 4
    assert job["completed"] == 4
    assert job["succeeded"] == 2
    assert job["failed"] == 2
    assert [item["index"] for item in job["items"]] == [0, 1, 2, 3]

    ok0, bad1, bad2, ok3 = job["items"]
    assert ok0["status"] == "succeeded" and ok0["audit_id"]
    assert bad1["status"] == "failed"
    assert bad1["audit_id"] is None and bad1["audit"] is None
    assert bad1["error"]["code"] == "invalid_manifest"
    assert bad1["error"]["details"], "非法 SemVer 必须携带结构化校验细节"
    assert bad2["status"] == "failed"
    assert bad2["error"]["code"] == "invalid_manifest"
    assert "ghost" in bad2["error"]["message"]
    assert ok3["status"] == "succeeded" and ok3["audit_id"]

    # 失败项不产生审计记录，成功项各有独立记录
    assert client.get(f"/api/v1/audits/{ok0['audit_id']}").status_code == 200
    assert client.get(f"/api/v1/audits/{ok3['audit_id']}").status_code == 200
    assert client.get("/api/v1/audits?limit=100").json()["total"] == 2


def test_job_bare_list_body_accepted(client):
    resp = client.post("/api/v1/audit-jobs", json=[GOOD, GOOD])
    assert resp.status_code == 202, resp.text
    assert resp.json()["total"] == 2
    job = _wait_terminal(client, resp.json()["id"])
    assert job["succeeded"] == 2


def test_job_items_solved_in_submission_order(client, monkeypatch):
    """后台按作业内下标顺序逐项求解：用屏障记录求解顺序。"""
    from app import solver as solver_module

    seen_order = []
    order_lock = threading.Lock()
    real_solve = solver_module.Solver.solve

    def ordered_solve(self, root_dependencies, root_name=None, root_version=None):
        with order_lock:
            seen_order.append(root_dependencies[0][0])
        return real_solve(self, root_dependencies, root_name, root_version)

    monkeypatch.setattr(solver_module.Solver, "solve", ordered_solve)

    manifests = [
        {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
        {"dependencies": [{"name": "authkit", "range": "^1.0.0"}]},
        {"dependencies": [{"name": "httpkit", "range": "^2.0.0"}]},
    ]
    resp = client.post("/api/v1/audit-jobs", json={"manifests": manifests})
    _wait_terminal(client, resp.json()["id"])
    assert seen_order == ["core-lib", "authkit", "httpkit"]


# ---------- 入参校验 ----------


@pytest.mark.parametrize(
    "payload",
    [
        {"manifests": []},
        {"manifests": None},
        [],
        [["not", "an", "object"]],
        {"manifests": [["nope"]]},
        "not-an-object",
        123,
    ],
)
def test_job_invalid_envelope_422(client, payload):
    resp = client.post("/api/v1/audit-jobs", json=payload)
    assert resp.status_code == 422, resp.text


def test_job_too_many_items_422(client):
    resp = client.post("/api/v1/audit-jobs", json={"manifests": [GOOD] * 21})
    assert resp.status_code == 422
    # 超限整体拒绝，不留作业痕迹
    assert client.get("/api/v1/audits").json()["total"] == 0


def test_job_boundary_20_items_accepted(client):
    resp = client.post("/api/v1/audit-jobs", json={"manifests": [GOOD] * 20})
    assert resp.status_code == 202, resp.text
    job = _wait_terminal(client, resp.json()["id"])
    assert job["total"] == 20
    assert job["succeeded"] == 20


# ---------- 取消 ----------


class _Gate:
    """让求解器在第一项上阻塞，直到测试发出放行信号。"""

    def __init__(self):
        self.enter = threading.Event()
        self.release = threading.Event()


def _post_gated_job(client, monkeypatch, gate):
    from app import solver as solver_module

    real_solve = solver_module.Solver.solve
    call = {"n": 0}

    def gated_solve(self, root_dependencies, root_name=None, root_version=None):
        call["n"] += 1
        if call["n"] == 1:
            gate.enter.set()
            assert gate.release.wait(timeout=5)
        return real_solve(self, root_dependencies, root_name, root_version)

    monkeypatch.setattr(solver_module.Solver, "solve", gated_solve)
    resp = client.post(
        "/api/v1/audit-jobs",
        json={"manifests": [GOOD, GOOD, GOOD, GOOD]},
    )
    assert resp.status_code == 202
    assert gate.enter.wait(timeout=5)
    return resp.json()["id"]


def test_delete_job_cancels_only_queued_items(client, monkeypatch):
    gate = _Gate()
    job_id = _post_gated_job(client, monkeypatch, gate)

    # 第 0 项 running，其余 queued：整单取消只能取消后 3 项
    resp = client.delete(f"/api/v1/audit-jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cancelled"] == 3
    assert body["completed"] == 3
    statuses = {item["index"]: item["status"] for item in body["items"]}
    assert statuses == {0: "running", 1: "cancelled", 2: "cancelled", 3: "cancelled"}

    gate.release.set()
    job = _wait_terminal(client, job_id)
    # running 项正常成功；有成功项 -> 作业 succeeded
    assert job["status"] == "succeeded"
    assert job["succeeded"] == 1
    assert job["cancelled"] == 3
    assert job["failed"] == 0


def test_delete_job_all_cancelled_terminal_succeeded(client, monkeypatch):
    """作业尚未开始时整单取消：全部项 cancelled，作业以 succeeded 终结。"""
    # 用一个“始终排队”的 runner：提交后暂停工作线程
    runner = client.app.state.job_runner
    runner.stop()
    try:
        resp = client.post("/api/v1/audit-jobs", json={"manifests": [GOOD, GOOD]})
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["id"]
        cancel = client.delete(f"/api/v1/audit-jobs/{job_id}")
        assert cancel.status_code == 200
        body = cancel.json()
        assert body["cancelled"] == 2
        assert body["status"] == "succeeded"
        assert all(item["status"] == "cancelled" for item in body["items"])
    finally:
        # 重启 runner，终结的作业不会再被处理
        runner.start()


def test_delete_item_cancels_single_queued_item(client, monkeypatch):
    gate = _Gate()
    job_id = _post_gated_job(client, monkeypatch, gate)

    # 取消第 2 项（queued）
    resp = client.delete(f"/api/v1/audit-jobs/{job_id}/items/2")
    assert resp.status_code == 200, resp.text
    assert resp.json()["cancelled"] == 1

    # 已开始的第 0 项不能取消 -> 409
    conflict = client.delete(f"/api/v1/audit-jobs/{job_id}/items/0")
    assert conflict.status_code == 409

    # 重复取消第 2 项 -> 409
    assert client.delete(f"/api/v1/audit-jobs/{job_id}/items/2").status_code == 409

    gate.release.set()
    job = _wait_terminal(client, job_id)
    statuses = {item["index"]: item["status"] for item in job["items"]}
    assert statuses[0] == "succeeded"
    assert statuses[1] == "succeeded"
    assert statuses[2] == "cancelled"
    assert statuses[3] == "succeeded"


def test_delete_unknown_job_404(client):
    assert client.delete("/api/v1/audit-jobs/nope").status_code == 404
    assert client.delete("/api/v1/audit-jobs/nope/items/0").status_code == 404


def test_delete_item_index_out_of_range_404(client):
    resp = client.post("/api/v1/audit-jobs", json={"manifests": [GOOD]})
    job_id = resp.json()["id"]
    _wait_terminal(client, job_id)
    assert client.delete(f"/api/v1/audit-jobs/{job_id}/items/7").status_code == 404


def test_delete_completed_job_no_queued_409(client):
    resp = client.post("/api/v1/audit-jobs", json={"manifests": [GOOD]})
    job_id = resp.json()["id"]
    _wait_terminal(client, job_id)
    assert client.delete(f"/api/v1/audit-jobs/{job_id}").status_code == 409


def test_get_unknown_job_404(client):
    assert client.get("/api/v1/audit-jobs/nope").status_code == 404


# ---------- 单项失败隔离 ----------


def test_job_solver_exception_is_item_failure(client, monkeypatch):
    """求解器异常：仅该项 failed(solver_error)，其他项照常成功。"""
    from app import solver as solver_module

    real_solve = solver_module.Solver.solve
    calls = {"n": 0}

    def boom(self, root_dependencies, root_name=None, root_version=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("solver exploded")
        return real_solve(self, root_dependencies, root_name, root_version)

    monkeypatch.setattr(solver_module.Solver, "solve", boom)

    resp = client.post("/api/v1/audit-jobs", json={"manifests": [GOOD, GOOD, GOOD]})
    job = _wait_terminal(client, resp.json()["id"])
    assert job["status"] == "failed"
    assert job["succeeded"] == 2
    assert job["failed"] == 1
    assert job["items"][1]["error"]["code"] == "solver_error"
    assert job["items"][0]["audit_id"] and job["items"][2]["audit_id"]
    assert client.get("/api/v1/audits?limit=100").json()["total"] == 2


# ---------- 重启恢复 ----------


def _wait_item_running(client, job_id, index, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/audit-jobs/{job_id}").json()
        if body["items"][index]["status"] == "running":
            return body
        time.sleep(0.01)
    raise AssertionError("第 %d 项未进入 running: %s" % (index, body))


def test_job_recovery_after_restart(client, tmp_path):
    """重启时 running 项标记 failed(interrupted)，queued 项由新 runner 续跑。"""
    from app import main as main_module

    # 停掉随应用启动的 runner，直接在 DB 层构造“崩溃现场”：
    # 一个作业中第 0 项 running（进程已死，没有任何在飞线程），其余 queued。
    client.app.state.job_runner.stop()
    conn = db.connect(main_module.DEFAULT_DB_PATH)
    try:
        db.create_job(conn, "job-restart", [GOOD, GOOD, GOOD])
        with db.transaction(conn):
            conn.execute(
                "UPDATE audit_job_items SET status = ?, started_at = ? "
                "WHERE job_id = ? AND item_index = 0",
                (db.ITEM_RUNNING, "2026-09-23T00:00:00+00:00", "job-restart"),
            )
            conn.execute(
                "UPDATE audit_jobs SET status = ? WHERE id = ?",
                (db.JOB_RUNNING, "job-restart"),
            )

        recovered = db.recover_interrupted_jobs(conn)
        assert recovered == 1

        # 新进程的 runner 启动后应继续消费剩余 queued 项直至作业终结
        new_runner = JobRunner(main_module.DEFAULT_DB_PATH)
        new_runner.start()
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                job_row = db.get_job(conn, "job-restart")
                if job_row["status"] in (db.JOB_SUCCEEDED, db.JOB_FAILED):
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("恢复后作业未终结")
            items = db.list_job_items(conn, "job-restart")
            statuses = [row["status"] for row in items]
            assert statuses[0] == "failed"
            assert statuses[1:] == ["succeeded", "succeeded"]
            assert items[0]["error_code"] == "interrupted"
            assert "重启" in items[0]["error_message"]
            job = db.get_job(conn, "job-restart")
            assert job["status"] == "failed"
            assert job["failed"] == 1
            assert job["succeeded"] == 2
            assert job["completed"] == 3
            assert job["cancelled"] == 0
        finally:
            new_runner.stop()
    finally:
        conn.close()


def test_bootstrap_recovers_and_is_idempotent(tmp_path, monkeypatch):
    """app.bootstrap 幂等：建表播种 + 恢复中断项，重复执行无副作用。"""
    from app import bootstrap

    db_path = tmp_path / "boot.db"
    seed_path = "/workspace/data/seed_registry.json"
    monkeypatch.setenv("AUDIT_DB_PATH", str(db_path))
    monkeypatch.setenv("AUDIT_SEED_PATH", seed_path)

    bootstrap.bootstrap(str(db_path), seed_path)
    bootstrap.bootstrap(str(db_path), seed_path)  # 第二次不应重复播种/报错

    conn = db.connect(str(db_path))
    try:
        assert db.package_count(conn) >= 6

        # 造一个 running 作业（模拟崩溃现场），再跑 bootstrap 应恢复
        db.create_job(
            conn,
            "job-boot",
            [GOOD, GOOD],
        )
        # 手动把第 0 项置 running
        with db.transaction(conn):
            conn.execute(
                "UPDATE audit_job_items SET status = ? WHERE job_id = ? AND item_index = 0",
                (db.ITEM_RUNNING, "job-boot"),
            )
            conn.execute(
                "UPDATE audit_jobs SET status = ? WHERE id = ?",
                (db.JOB_RUNNING, "job-boot"),
            )
    finally:
        conn.close()

    bootstrap.bootstrap(str(db_path), seed_path)

    conn = db.connect(str(db_path))
    try:
        items = db.list_job_items(conn, "job-boot")
        assert items[0]["status"] == "failed"
        assert items[0]["error_code"] == "interrupted"
        assert items[1]["status"] == "queued"  # 未开始的项保持排队，等待 runner 续跑
        job = db.get_job(conn, "job-boot")
        assert job["status"] == "running"  # 仍有 queued 项，作业保持运行态
    finally:
        conn.close()
