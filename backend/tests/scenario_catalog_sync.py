"""场景核验：逐表目录校验结论、目录对账与补单并发处理。

运行：cd backend && .venv/bin/python -m tests.scenario_catalog_sync
覆盖：
  A. 初始对账 → 只针对目录甲发起补单；仍缺列时沿用已有补单
  B. 补单运行中登记 V6 → 再对账 → 拦截旧范围补单 → 旧补单完成不覆盖 V6
  C. 无并发新修订时补单正常落地
  D. 无进行中补单且仍缺列时另起同表补单
  E. 字段类型不符与额外可空列的逐表结论
"""
from __future__ import annotations

import copy
import sys

from app.services.catalog_sync import (
    BASELINE_CHANGE,
    PROMOTION_CHANGE as CHANGE,
    CatalogSyncService,
)
from app.store import store

ALPHA = "catalog_alpha"
BETA = "catalog_beta"
TABLE = "order_fact"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "✓" if condition else "✗"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def basis_of(recon: dict, catalog: str) -> dict:
    return next(b for b in recon["basis"] if b["catalog"] == catalog)


def main() -> int:
    svc = CatalogSyncService()

    print("场景A：初始目录对账与补单发起")
    # A1 对账#1：甲 V5 缺列进待处理清单，乙独立就绪
    r1 = svc.reconcile_catalogs(CHANGE)
    check("对账#1 待处理补同步清单只含目录甲",
          len(r1["pending_syncs"]) == 1
          and r1["pending_syncs"][0]["catalog"] == ALPHA
          and r1["pending_syncs"][0]["table"] == TABLE
          and r1["pending_syncs"][0]["revision"] == "V5"
          and r1["pending_syncs"][0]["missing_columns"] == ["promotion_code"])
    check("对账#1 目录乙不受甲差异影响、独立就绪",
          basis_of(r1, BETA)["ready"] and not basis_of(r1, BETA)["missing_required"])
    check("对账#1 依据记录甲使用 V5 且缺 promotion_code",
          basis_of(r1, ALPHA)["revision"] == "V5"
          and basis_of(r1, ALPHA)["missing_required"] == ["promotion_code"])

    # A2 历史通过记录可查：V4 基线结论与任务列表的 V4 已同步记录都在
    passed = svc.list_validation_conclusions(catalog=ALPHA, ready_only=True)
    check("历史通过记录可查（V4 基线结论）",
          any(c["revision"] == "V4" and c["change_id"] == BASELINE_CHANGE and c["ready"]
              for c in passed["conclusions"]))
    v4_row = store.find("catalog_sync", 4)
    check("任务列表仍有 V4 已同步的记录",
          v4_row is not None and v4_row["status"] == "已同步" and v4_row["同步范围"] == "order_fact@V4")

    # A3 当前版本缺列时不满足放行条件
    rel1 = svc.check_release_readiness(CHANGE, ALPHA)
    check("当前采用 V5 缺列，不满足放行条件",
          not rel1["releasable"] and rel1["adopted_revision"] == "V5"
          and "promotion_code" in rel1["message"])

    # A4 值班员从目录同步入口发起只针对甲的补单
    init = svc.initiate_catalog_sync(CHANGE, ALPHA)
    order_id = init["orders"][0]["order_id"]
    order = next(o for o in svc.catalog_registry_snapshot()["catalogs"][0]["sync_orders"]
                 if o["order_id"] == order_id)
    check("只针对甲发起补单，范围取 V5 缺列",
          init["action"] == "已发起补单"
          and order["base_revision"] == "V5"
          and order["missing_columns"] == ["promotion_code"])
    snap = svc.catalog_registry_snapshot()
    check("目录乙没有补单", snap["catalogs"][1]["sync_orders"] == [])
    task_row = store.find("catalog_sync", order["task_id"])
    check("补单已进入任务列表（同步中）",
          task_row is not None and task_row["status"] == "同步中"
          and task_row["目标Catalog"] == ALPHA)

    # A5 仍缺列时跟进：沿用已有补单，不重复起单
    f1 = svc.check_sync_followup(CHANGE, ALPHA)
    check("仍缺列时沿用已有补单，无须重复起单",
          f1["decisions"][0]["decision"] == "沿用已有补单"
          and f1["decisions"][0]["order_id"] == order_id
          and len(svc.catalog_registry_snapshot()["catalogs"][0]["sync_orders"]) == 1)

    print("场景B：V6 到达与新旧修订并发处理")
    # B1 补单运行中受理 V6 登记，收到的修订和采用版本分开记录
    reg = svc.register_catalog_revision(ALPHA, "V6")
    check("V6 登记受理，收到修订追加、采用版本切换到 V6",
          reg["ok"] and reg["received_revisions"] == ["V4", "V5", "V6"]
          and reg["adopted_revision"] == "V6" and reg["adopted_changed"])
    check("V6 登记时能看到运行中的旧补单", reg["running_orders"] == [order_id])
    v6_snapshot = copy.deepcopy(svc._catalogs[ALPHA]["revisions"]["V6"]["tables"])

    # B2 V6 到达后、原补单结束前再对账：用当前采用的 V6
    r2 = svc.reconcile_catalogs(CHANGE)
    check("对账#2 使用当前采用的 V6，甲已就绪",
          basis_of(r2, ALPHA)["revision"] == "V6" and basis_of(r2, ALPHA)["ready"])
    check("对账#2 待处理清单为空", r2["pending_syncs"] == [])

    # B3 已满足就绪规则：拦住旧范围的补同步
    f2 = svc.check_sync_followup(CHANGE, ALPHA)
    check("已满足就绪规则，拦截旧范围补同步",
          f2["decisions"][0]["decision"] == "拦截旧范围补同步"
          and f2["decisions"][0]["order_id"] == order_id)

    # B4 旧补单完成请求仍受理，但不能覆盖已采用的 V6
    done = svc.complete_sync_order(order_id)
    check("旧补单完成请求受理但不覆盖 V6",
          done["ok"] and not done["applied"] and done["adopted_revision"] == "V6")
    check("V6 结构未被旧补单改动",
          svc._catalogs[ALPHA]["revisions"]["V6"]["tables"] == v6_snapshot)

    # B5 V5 的校验不能替 V6 放行：放行只认当前采用版本
    v5_check = svc.validate_table_structure(CHANGE, ALPHA, TABLE, revision="V5")
    rel2 = svc.check_release_readiness(CHANGE, ALPHA)
    check("V5 结论仅作记录（仍未就绪），不能替 V6 放行",
          not v5_check["conclusion"]["ready"]
          and rel2["releasable"] and rel2["adopted_revision"] == "V6"
          and all(c["revision"] == "V6" for c in rel2["conclusions"]))

    # B6 两个请求处理完后，合格的 V6 可用于当前目录对账
    r3 = svc.reconcile_catalogs(CHANGE)
    check("对账#3 合格的 V6 可用于当前对账",
          basis_of(r3, ALPHA)["revision"] == "V6" and basis_of(r3, ALPHA)["ready"]
          and r3["pending_syncs"] == [])

    # B7 核对两次对账分别使用的结构版本和缺少字段依据
    check("两次对账依据可核对：#1 用 V5 缺列、#2 用 V6 不缺列",
          basis_of(r1, ALPHA)["revision"] == "V5"
          and basis_of(r1, ALPHA)["missing_required"] == ["promotion_code"]
          and basis_of(r2, ALPHA)["revision"] == "V6"
          and basis_of(r2, ALPHA)["missing_required"] == [])
    recons = svc.list_reconciliations()
    check("对账记录全部留痕可查", recons["total"] == 3)

    # B8 历史表 order_fact_archive 不在核验范围
    arch = svc.validate_table_structure(CHANGE, ALPHA, "order_fact_archive")
    check("order_fact_archive 不在核验范围", not arch["ok"] and not arch["in_scope"])

    print("场景C：无并发新修订时补单正常落地")
    svc2 = CatalogSyncService()
    init2 = svc2.initiate_catalog_sync(CHANGE, ALPHA)
    oid2 = init2["orders"][0]["order_id"]
    done2 = svc2.complete_sync_order(oid2)
    rel3 = svc2.check_release_readiness(CHANGE, ALPHA)
    check("补单完成后采用版本补齐缺列并放行",
          done2["applied"] and done2["order_status"] == "已同步" and rel3["releasable"])

    print("场景D：无进行中补单且仍缺列时另起同表补单")
    svc3 = CatalogSyncService()
    f3 = svc3.check_sync_followup(CHANGE, ALPHA)
    check("另起同表补单",
          f3["decisions"][0]["decision"] == "另起同表补单" and f3["decisions"][0]["created"])

    print("场景E：字段类型核对与额外可空列单列")
    svc4 = CatalogSyncService()
    bad_v6 = {
        TABLE: {"columns": {
            "order_id": {"type": "STRING", "nullable": False},
            "order_date": {"type": "DATE", "nullable": False},
            "customer_id": {"type": "STRING", "nullable": False},
            "amount": {"type": "DECIMAL(18,2)", "nullable": False},
            "promotion_code": {"type": "INT", "nullable": False},
        }},
    }
    svc4.register_catalog_revision(BETA, "V6", tables=bad_v6)
    bad = svc4.validate_table_structure(CHANGE, BETA, TABLE)
    check("必需列类型不符时不就绪",
          not bad["conclusion"]["ready"]
          and bad["conclusion"]["type_mismatches"] == [
              {"column": "promotion_code", "expected": "STRING", "actual": "INT"}])
    extra = svc4.validate_table_structure(CHANGE, BETA, TABLE, revision="V5")
    check("额外可空列单独列出",
          extra["conclusion"]["extra_nullable"] == [{"column": "channel_tag", "type": "STRING"}]
          and extra["conclusion"]["ready"])

    print()
    if FAILURES:
        print(f"未通过 {len(FAILURES)} 项：")
        for label in FAILURES:
            print(f"  - {label}")
        return 1
    print("全部场景核验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
