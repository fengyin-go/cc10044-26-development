"""目录同步领域服务：同步不同Catalog或Metastore之间的元数据和表结构。

数据链路位置：元数据同步层，保持多个数据目录的结构一致性。

逐表目录校验结论：按「源变更 × 目标目录 × 表名 × 目录修订」核对源表必需字段
和字段类型，额外可空列单独列出；目录对账据此生成待处理的补同步清单，目录
同步入口按清单发起、跟进和拦截补单。新旧目录修订并发处理期间，校验放行只
认当前采用的结构版本，旧范围补单完成也不覆盖已采用的新修订。
"""

from __future__ import annotations

import copy
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 源端订单表权威结构：源变更 SC-2026-1123-PROMOTION 生效后的必需字段口径。
SOURCE_REQUIRED_COLUMNS: dict[str, dict[str, str]] = {
    "order_fact": {
        "order_id": "STRING",
        "order_date": "DATE",
        "customer_id": "STRING",
        "amount": "DECIMAL(18,2)",
        "promotion_code": "STRING",
    },
}

BASELINE_CHANGE = "SC-2026-0901-BASELINE"
PROMOTION_CHANGE = "SC-2026-1123-PROMOTION"
SOURCE_CATALOG = "order_source_catalog"
RUN_DATE = "2026-11-23"


def _table(required: dict[str, str], extra_nullable: dict[str, str] | None = None) -> dict[str, Any]:
    """组装目录修订中的表结构：源表必需列 + 目录侧额外可空列。"""
    columns = {name: {"type": ctype, "nullable": False} for name, ctype in required.items()}
    for name, ctype in (extra_nullable or {}).items():
        columns[name] = {"type": ctype, "nullable": True}
    return {"columns": columns}


class CatalogSyncService(BaseModuleService):
    MODULE = "catalog_sync"
    ENTITY = "同步任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务名称", "源Catalog", "目标Catalog", "同步范围", "同步状态"]
    REQUIRED_FIELDS = ["任务名称", "源Catalog", "目标Catalog"]
    STATUS_ORDER = ["待同步", "同步中", "已同步", "同步失败"]
    ACTION_RULES = {"启动同步": "待同步", "取消同步": "同步中"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self._seq = 0
        # 源变更登记：核验范围只含变更影响的表，历史表 order_fact_archive 不在范围。
        self._source_changes: dict[str, dict[str, Any]] = {
            BASELINE_CHANGE: {
                "change_id": BASELINE_CHANGE,
                "table": "order_fact",
                "change_type": "BASELINE",
                "column": "",
                "column_type": "",
                "required": False,
                "scope_tables": ["order_fact"],
                "summary": "订单表基线结构登记（V4 同步依据）",
            },
            PROMOTION_CHANGE: {
                "change_id": PROMOTION_CHANGE,
                "table": "order_fact",
                "change_type": "ADD_COLUMN",
                "column": "promotion_code",
                "column_type": "STRING",
                "required": True,
                "scope_tables": ["order_fact"],
                "summary": "源端订单表 order_fact 新增必需列 promotion_code STRING",
            },
        }
        baseline_cols = {
            name: ctype for name, ctype in SOURCE_REQUIRED_COLUMNS["order_fact"].items()
            if name != "promotion_code"
        }
        # 目录注册表：收到的修订和当前采用的版本分开记录。
        # 目录甲现用 V5（缺 promotion_code）；目录乙当前修订已含该列。
        self._catalogs: dict[str, dict[str, Any]] = {
            "catalog_alpha": {
                "received_revisions": ["V4", "V5"],
                "adopted_revision": "V5",
                "revisions": {
                    "V4": {"tables": {"order_fact": _table(baseline_cols)}},
                    "V5": {"tables": {
                        "order_fact": _table(baseline_cols, extra_nullable={"legacy_remark": "STRING"}),
                        "order_fact_archive": _table({"order_id": "STRING", "archived_at": "TIMESTAMP"}),
                    }},
                },
            },
            "catalog_beta": {
                "received_revisions": ["V4", "V5"],
                "adopted_revision": "V5",
                "revisions": {
                    "V4": {"tables": {"order_fact": _table(baseline_cols)}},
                    "V5": {"tables": {
                        "order_fact": _table(
                            SOURCE_REQUIRED_COLUMNS["order_fact"],
                            extra_nullable={"channel_tag": "STRING"},
                        ),
                    }},
                },
            },
        }
        # 逐表目录校验结论（含历史通过记录，全部保留可查）。
        self._conclusions: list[dict[str, Any]] = [{
            "seq": 0,
            "change_id": BASELINE_CHANGE,
            "catalog": "catalog_alpha",
            "table": "order_fact",
            "revision": "V4",
            "missing_required": [],
            "type_mismatches": [],
            "extra_nullable": [],
            "ready": True,
            "concluded_at": "2026-09-01",
            "note": "基线结构核验通过（历史通过记录）",
        }]
        self._reconciliations: list[dict[str, Any]] = []
        self._sync_orders: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 逐表目录校验结论
    # ------------------------------------------------------------------
    def validate_table_structure(
        self,
        change_id: str,
        catalog: str,
        table: str,
        revision: str | None = None,
    ) -> dict[str, Any]:
        """生成逐表目录校验结论。

        结论对应同一项源变更、目标目录、确切表名和目录修订：按源表核对
        必需字段和字段类型，目录侧的额外可空列单独列出。revision 缺省时
        使用目录当前采用的版本。
        """
        change = self._source_changes.get(change_id)
        if change is None:
            return {"ok": False, "message": f"源变更 {change_id} 不存在"}
        if catalog not in self._catalogs:
            return {"ok": False, "message": f"目录 {catalog} 不存在"}
        if table not in change["scope_tables"]:
            return {
                "ok": False,
                "in_scope": False,
                "message": f"表 {table} 不在源变更 {change_id} 的核验范围",
            }
        cat = self._catalogs[catalog]
        revision = revision or cat["adopted_revision"]
        if revision not in cat["revisions"]:
            return {"ok": False, "message": f"目录 {catalog} 未登记修订 {revision}"}
        conclusion = self._validate(change, catalog, table, revision)
        return {"ok": True, "in_scope": True, "conclusion": conclusion}

    def list_validation_conclusions(
        self,
        change_id: str | None = None,
        catalog: str | None = None,
        table: str | None = None,
        ready_only: bool = False,
    ) -> dict[str, Any]:
        """查询逐表目录校验结论，历史通过记录一并保留可查。"""
        rows = list(self._conclusions)
        if change_id:
            rows = [r for r in rows if r["change_id"] == change_id]
        if catalog:
            rows = [r for r in rows if r["catalog"] == catalog]
        if table:
            rows = [r for r in rows if r["table"] == table]
        if ready_only:
            rows = [r for r in rows if r["ready"]]
        return {"total": len(rows), "conclusions": rows}

    # ------------------------------------------------------------------
    # 目录对账与放行
    # ------------------------------------------------------------------
    def reconcile_catalogs(self, change_id: str) -> dict[str, Any]:
        """目录对账：按当前采用版本逐表校验，生成待处理的补同步清单。

        每个目录独立对账，一个目录的差异不影响另一个目录；每次对账记录
        各目录实际使用的结构版本和缺少字段依据，供事后回查核对。
        """
        change = self._source_changes.get(change_id)
        if change is None:
            return {"ok": False, "message": f"源变更 {change_id} 不存在"}

        results: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for catalog, cat in self._catalogs.items():
            for table in change["scope_tables"]:
                conclusion = self._validate(change, catalog, table, cat["adopted_revision"])
                results.append(conclusion)
                if not conclusion["ready"]:
                    running = self._running_order(change_id, catalog, table)
                    pending.append({
                        "change_id": change_id,
                        "catalog": catalog,
                        "table": table,
                        "revision": conclusion["revision"],
                        "missing_columns": conclusion["missing_required"],
                        "type_mismatches": conclusion["type_mismatches"],
                        "running_order_id": running["order_id"] if running else None,
                    })

        record = {
            "reconciliation_id": len(self._reconciliations) + 1,
            "change_id": change_id,
            "ran_at": RUN_DATE,
            "basis": [
                {
                    "catalog": c["catalog"],
                    "table": c["table"],
                    "revision": c["revision"],
                    "missing_required": c["missing_required"],
                    "ready": c["ready"],
                }
                for c in results
            ],
            "pending_syncs": pending,
        }
        self._reconciliations.append(record)
        return {"ok": True, **record}

    def list_reconciliations(self) -> dict[str, Any]:
        """目录对账记录：每次对账使用的结构版本和缺列依据都可回查。"""
        return {"total": len(self._reconciliations), "reconciliations": self._reconciliations}

    def check_release_readiness(self, change_id: str, catalog: str) -> dict[str, Any]:
        """放行条件检查：只按当前采用版本、同一项源变更的逐表结论判定。

        历史修订（如 V4）的通过记录和其他修订（如 V5）的校验结论都不能
        替当前采用版本放行；当前版本缺必需列时不满足放行条件。
        """
        change = self._source_changes.get(change_id)
        if change is None:
            return {"ok": False, "message": f"源变更 {change_id} 不存在"}
        cat = self._catalogs.get(catalog)
        if cat is None:
            return {"ok": False, "message": f"目录 {catalog} 不存在"}

        revision = cat["adopted_revision"]
        conclusions = [
            self._validate(change, catalog, table, revision)
            for table in change["scope_tables"]
        ]
        blocking = [c for c in conclusions if not c["ready"]]
        parts: list[str] = []
        for conclusion in blocking:
            if conclusion["missing_required"]:
                parts.append(
                    f"{conclusion['table']} 缺必需列 {','.join(conclusion['missing_required'])}")
            if conclusion["type_mismatches"]:
                mism = ",".join(m["column"] for m in conclusion["type_mismatches"])
                parts.append(f"{conclusion['table']} 字段类型不符 {mism}")
        detail = "；".join(parts)
        releasable = not blocking
        return {
            "ok": True,
            "change_id": change_id,
            "catalog": catalog,
            "adopted_revision": revision,
            "releasable": releasable,
            "conclusions": conclusions,
            "message": (
                f"当前采用 {revision} 结构就绪，满足放行条件"
                if releasable
                else f"当前采用 {revision} {detail}，不满足放行条件"
            ),
        }

    # ------------------------------------------------------------------
    # 目录同步入口：补单发起、跟进与完成
    # ------------------------------------------------------------------
    def initiate_catalog_sync(self, change_id: str, catalog: str) -> dict[str, Any]:
        """目录同步入口：按缺列目录的补同步范围发起补单，只针对该目录。

        先用当前采用版本做逐表校验，仍缺必需列时才起单；同表已有进行中
        的补单则直接沿用，不重复起单。其他目录不受影响。
        """
        change = self._source_changes.get(change_id)
        if change is None:
            return {"ok": False, "message": f"源变更 {change_id} 不存在"}
        if catalog not in self._catalogs:
            return {"ok": False, "message": f"目录 {catalog} 不存在"}

        readiness = self.check_release_readiness(change_id, catalog)
        if readiness["releasable"]:
            return {
                "ok": True,
                "action": "无需补单",
                "catalog": catalog,
                "adopted_revision": readiness["adopted_revision"],
                "orders": [],
                "message": "当前采用版本结构已就绪，无须发起补单",
            }

        orders: list[dict[str, Any]] = []
        created = False
        for conclusion in readiness["conclusions"]:
            if conclusion["ready"]:
                continue
            table = conclusion["table"]
            running = self._running_order(change_id, catalog, table)
            if running is not None:
                orders.append({
                    "order_id": running["order_id"], "table": table, "decision": "沿用已有补单",
                })
                continue
            order = self._open_sync_order(change, catalog, table, conclusion["missing_required"])
            created = True
            orders.append({"order_id": order["order_id"], "table": table, "decision": "新建补单"})

        return {
            "ok": True,
            "action": "已发起补单" if created else "沿用已有补单",
            "catalog": catalog,
            "adopted_revision": readiness["adopted_revision"],
            "orders": orders,
            "message": f"补单只针对目录 {catalog}，其他目录不受影响",
        }

    def check_sync_followup(self, change_id: str, catalog: str) -> dict[str, Any]:
        """目录同步入口的跟进核对：决定沿用、另起还是拦截补单。

        按当前采用版本重新校验：仍缺必需列时沿用进行中的补单（没有才另
        起同表补单）；已满足就绪规则时拦截旧范围的补同步。
        """
        change = self._source_changes.get(change_id)
        if change is None:
            return {"ok": False, "message": f"源变更 {change_id} 不存在"}
        if catalog not in self._catalogs:
            return {"ok": False, "message": f"目录 {catalog} 不存在"}

        readiness = self.check_release_readiness(change_id, catalog)
        adopted = readiness["adopted_revision"]
        decisions: list[dict[str, Any]] = []
        for conclusion in readiness["conclusions"]:
            table = conclusion["table"]
            running = self._running_order(change_id, catalog, table)
            if conclusion["ready"]:
                if running is not None:
                    running["status"] = "已拦截"
                    running["notes"].append(
                        f"跟进核对时目录已采用 {adopted} 且结构就绪，旧范围补同步被拦截")
                    self._update_task_row(
                        running["task_id"], "同步失败", f"已拦截-目录已采用{adopted}且结构就绪")
                    decisions.append({
                        "table": table, "decision": "拦截旧范围补同步",
                        "order_id": running["order_id"], "created": False,
                    })
                else:
                    decisions.append({
                        "table": table, "decision": "无需补单", "order_id": None, "created": False,
                    })
            elif running is not None:
                decisions.append({
                    "table": table, "decision": "沿用已有补单",
                    "order_id": running["order_id"], "created": False,
                })
            else:
                order = self._open_sync_order(change, catalog, table, conclusion["missing_required"])
                decisions.append({
                    "table": table, "decision": "另起同表补单",
                    "order_id": order["order_id"], "created": True,
                })

        return {
            "ok": True,
            "catalog": catalog,
            "adopted_revision": adopted,
            "releasable": readiness["releasable"],
            "decisions": decisions,
        }

    def complete_sync_order(self, order_id: int) -> dict[str, Any]:
        """受理补单完成请求。

        完成请求始终受理并记录；补单范围基于发起时的目录修订，若目录已
        采用更新的修订（如 V6），旧补单不得覆盖当前采用版本，只记录完成。
        """
        order = next((o for o in self._sync_orders if o["order_id"] == order_id), None)
        if order is None:
            return {"ok": False, "message": f"补单 {order_id} 不存在"}
        catalog = self._catalogs[order["catalog"]]
        adopted = catalog["adopted_revision"]

        if order["status"] == "已拦截":
            order["notes"].append("完成请求已受理：补单已被拦截，未改动目录结构")
            return {
                "ok": True,
                "applied": False,
                "order_status": "已拦截",
                "adopted_revision": adopted,
                "message": "完成请求已受理，旧范围补单已被拦截，未覆盖当前采用版本",
            }
        if order["status"] in ("已同步", "已过期"):
            return {
                "ok": True,
                "applied": order["applied"],
                "order_status": order["status"],
                "adopted_revision": adopted,
                "message": "补单已完结，完成请求已记录",
            }

        if order["base_revision"] == adopted:
            columns = catalog["revisions"][adopted]["tables"].setdefault(
                order["table"], {"columns": {}})["columns"]
            required = SOURCE_REQUIRED_COLUMNS.get(order["table"], {})
            for name in order["missing_columns"]:
                columns[name] = {"type": required.get(name, "STRING"), "nullable": False}
            order["status"] = "已同步"
            order["applied"] = True
            self._update_task_row(order["task_id"], "已同步", "已完成")
            return {
                "ok": True,
                "applied": True,
                "order_status": "已同步",
                "adopted_revision": adopted,
                "message": f"补单完成，{order['table']} 已补齐 {len(order['missing_columns'])} 列",
            }

        order["status"] = "已过期"
        order["applied"] = False
        order["notes"].append(
            f"完成时目录已采用 {adopted}，旧范围 {order['base_revision']} 未覆盖当前版本")
        self._update_task_row(order["task_id"], "同步失败", f"已过期-目录已采用{adopted}，未覆盖")
        return {
            "ok": True,
            "applied": False,
            "order_status": "已过期",
            "adopted_revision": adopted,
            "message": f"完成请求已受理，目录已采用 {adopted}，旧补单未覆盖当前采用版本",
        }

    # ------------------------------------------------------------------
    # 目录修订登记
    # ------------------------------------------------------------------
    def register_catalog_revision(
        self,
        catalog: str,
        revision: str,
        tables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """受理目录修订登记，分别记录收到的修订和当前采用的版本。

        补单运行中也可登记；登记只追加收到的修订，版本号更新时才切换当
        前采用版本。未显式给出结构时，按全部源变更在当前采用版本基础上
        补齐必需列，模拟「已补齐该列的结构更正」。
        """
        cat = self._catalogs.get(catalog)
        if cat is None:
            return {"ok": False, "message": f"目录 {catalog} 不存在"}
        if revision in cat["received_revisions"]:
            return {"ok": False, "message": f"修订 {revision} 已登记，不能重复登记"}

        if tables is None:
            tables = copy.deepcopy(cat["revisions"][cat["adopted_revision"]]["tables"])
            for change in self._source_changes.values():
                if change.get("change_type") != "ADD_COLUMN":
                    continue
                for table in change["scope_tables"]:
                    columns = tables.setdefault(table, {"columns": {}})["columns"]
                    columns.setdefault(change["column"], {
                        "type": change["column_type"],
                        "nullable": not change["required"],
                    })
        cat["revisions"][revision] = {"tables": tables}
        cat["received_revisions"].append(revision)

        adopted_before = cat["adopted_revision"]
        if self._revision_no(revision) > self._revision_no(adopted_before):
            cat["adopted_revision"] = revision
        adopted = cat["adopted_revision"]
        running = [
            o["order_id"] for o in self._sync_orders
            if o["catalog"] == catalog and o["status"] == "同步中"
        ]
        return {
            "ok": True,
            "catalog": catalog,
            "received_revision": revision,
            "received_revisions": list(cat["received_revisions"]),
            "adopted_revision": adopted,
            "adopted_changed": adopted != adopted_before,
            "running_orders": running,
            "message": (
                "修订已受理登记，并切换为当前采用版本"
                if adopted != adopted_before
                else "修订已受理登记，当前采用版本不变"
            ),
        }

    def catalog_registry_snapshot(self) -> dict[str, Any]:
        """目录注册表快照：收到的修订、当前采用版本和补单状态分开记录。"""
        return {
            "source_changes": list(self._source_changes.values()),
            "catalogs": [
                {
                    "catalog": name,
                    "received_revisions": list(cat["received_revisions"]),
                    "adopted_revision": cat["adopted_revision"],
                    "registered_revisions": sorted(cat["revisions"], key=self._revision_no),
                    "sync_orders": [
                        {
                            key: order[key]
                            for key in (
                                "order_id", "task_id", "table", "base_revision",
                                "missing_columns", "status", "applied",
                            )
                        }
                        for order in self._sync_orders
                        if order["catalog"] == name
                    ],
                }
                for name, cat in self._catalogs.items()
            ],
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _validate(
        self,
        change: dict[str, Any],
        catalog: str,
        table: str,
        revision: str,
    ) -> dict[str, Any]:
        """按源表必需字段和类型核对指定目录修订，生成逐表目录校验结论。"""
        self._seq += 1
        cat = self._catalogs[catalog]
        columns = cat["revisions"].get(revision, {}).get("tables", {}).get(table, {}).get("columns", {})
        required = SOURCE_REQUIRED_COLUMNS.get(table, {})
        missing = [name for name in required if name not in columns]
        type_mismatches = [
            {"column": name, "expected": ctype, "actual": columns[name]["type"]}
            for name, ctype in required.items()
            if name in columns and columns[name]["type"] != ctype
        ]
        extra_nullable = [
            {"column": name, "type": col["type"]}
            for name, col in columns.items()
            if name not in required
        ]
        conclusion = {
            "seq": self._seq,
            "change_id": change["change_id"],
            "catalog": catalog,
            "table": table,
            "revision": revision,
            "missing_required": missing,
            "type_mismatches": type_mismatches,
            "extra_nullable": extra_nullable,
            "ready": not missing and not type_mismatches,
            "concluded_at": RUN_DATE,
        }
        self._conclusions.append(conclusion)
        return conclusion

    def _running_order(self, change_id: str, catalog: str, table: str) -> dict[str, Any] | None:
        for order in self._sync_orders:
            if (
                order["change_id"] == change_id
                and order["catalog"] == catalog
                and order["table"] == table
                and order["status"] == "同步中"
            ):
                return order
        return None

    def _open_sync_order(
        self,
        change: dict[str, Any],
        catalog: str,
        table: str,
        missing_columns: list[str],
    ) -> dict[str, Any]:
        """开立补单：范围取发起时目录采用版本所缺的必需列，并登记同步任务行。"""
        revision = self._catalogs[catalog]["adopted_revision"]
        rows = store.rows(self.MODULE)
        task_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        rows.append({
            "id": task_id,
            "status": "同步中",
            "pending": True,
            "abnormal": False,
            "任务名称": f"补同步_{catalog}_{table}",
            "源Catalog": SOURCE_CATALOG,
            "目标Catalog": catalog,
            "同步范围": f"{table}@{revision}:+{','.join(missing_columns)}",
            "同步状态": f"补同步中-待补{len(missing_columns)}列",
        })
        order = {
            "order_id": len(self._sync_orders) + 1,
            "task_id": task_id,
            "change_id": change["change_id"],
            "catalog": catalog,
            "table": table,
            "base_revision": revision,
            "missing_columns": list(missing_columns),
            "status": "同步中",
            "applied": False,
            "created_at": RUN_DATE,
            "notes": [],
        }
        self._sync_orders.append(order)
        return order

    def _update_task_row(self, task_id: int, status: str, note: str) -> None:
        row = store.find(self.MODULE, task_id)
        if row is not None:
            row["status"] = status
            row["同步状态"] = note
            row["pending"] = status in ("待同步", "同步中")

    @staticmethod
    def _revision_no(revision: str) -> int:
        digits = "".join(ch for ch in str(revision) if ch.isdigit())
        return int(digits) if digits else 0

    # ------------------------------------------------------------------
    # 既有能力：同步执行与宏观完整性检查
    # ------------------------------------------------------------------
    def start_sync(self, entry_id: int, incremental: bool = True) -> dict[str, Any]:
        """启动目录同步任务。

        incremental=True 时只同步上次同步以来发生变更的表，
        incremental=False 时全量重新同步。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}
        if entry.get("status") != "待同步":
            return {"ok": False, "message": "只能启动待同步状态的同步任务"}

        source_catalog = entry.get("源Catalog", "")
        target_catalog = entry.get("目标Catalog", "")
        sync_scope = entry.get("同步范围", "")

        entry["status"] = "同步中"
        tables_to_sync = self._resolve_sync_tables(source_catalog, sync_scope)
        return {
            "ok": True,
            "task_id": entry_id,
            "source_catalog": source_catalog,
            "target_catalog": target_catalog,
            "incremental": incremental,
            "tables_to_sync": len(tables_to_sync),
            "estimated_duration_minutes": 12,
            "tables": tables_to_sync[:20],
        }

    def validate_sync_completeness(self, entry_id: int) -> dict[str, Any]:
        """验证同步完整性。

        对比源和目标Catalog中的表列表、Schema和分区定义，
        检测遗漏、不一致或版本差异。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}

        checks = [
            {"check": "table_list_match", "source_count": 320, "target_count": 320, "matched": True, "mismatched_tables": []},
            {"check": "schema_match", "tables_with_diff": 2, "diff_details": [
                {"table": "order_fact_v3", "source_columns": 48, "target_columns": 47,
                 "missing_in_target": ["promotion_code"]},
                {"table": "user_behavior_log", "source_columns": 22, "target_columns": 22,
                 "type_diff": ["event_timestamp: TIMESTAMP vs STRING"]},
            ], "matched": False},
            {"check": "partition_match", "total_tables": 320, "mismatched": 0, "matched": True},
            {"check": "table_properties_match", "mismatched": 1, "details": [
                {"table": "dashboard_cache", "prop": "format-version", "source": "2", "target": "1"}
            ], "matched": False},
        ]
        all_ok = all(c["matched"] for c in checks)
        if all_ok:
            entry["status"] = "已同步"
            entry["同步状态"] = "已完成"
            entry["pending"] = False
        else:
            entry["abnormal"] = True

        return {
            "ok": all_ok,
            "task_id": entry_id,
            "checks": checks,
            "verdict": "同步完整" if all_ok else "存在差异需要修复",
        }

    def detect_schema_conflicts(self, entry_id: int) -> dict[str, Any]:
        """检测目录同步中的Schema冲突。

        当源和目标Catalog对同一表的Schema定义不一致时，
        列出冲突详情并提供解决建议。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}

        return {
            "task_id": entry_id,
            "conflict_count": 3,
            "conflicts": [
                {
                    "table": "user_events",
                    "conflict_type": "column_type_mismatch",
                    "source": "event_data: MAP<STRING,STRING>",
                    "target": "event_data: STRING",
                    "resolution": "建议以源端类型为准，目标端已过期",
                },
                {
                    "table": "dim_customer",
                    "conflict_type": "missing_column",
                    "column": "loyalty_tier",
                    "resolution": "在目标端添加该列，默认值设为'standard'",
                },
                {
                    "table": "sales_summary",
                    "conflict_type": "partition_diff",
                    "source_partition": "dt",
                    "target_partition": "date_str",
                    "resolution": "按源端分区键重新创建目标表",
                },
            ],
            "auto_resolvable": 2,
            "manual_intervention_required": 1,
        }

    def sync_with_lineage(self, entry_id: int) -> dict[str, Any]:
        """同步完成后更新血缘关系。

        将同步范围内的表在目标Catalog的位置注册到血缘图谱，
        建立源→目标的跨Catalog血缘链路。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}

        lineage_rows = store.rows("lineage_graph")
        start_id = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
        new_lineages = []
        for i in range(3):
            lid = start_id + i
            lineage_rows.append({
                "id": lid,
                "status": "已建立",
                "目标表": f"{entry.get('目标Catalog')}_{entry.get('同步范围', 'table')}_{i}",
                "来源表列表": f"{entry.get('源Catalog')}_{entry.get('同步范围', 'table')}_{i}",
                "转换逻辑": f"catalog_sync_{entry_id}",
                "中间层级": "1",
                "血缘深度": "1",
                "pending": False,
                "abnormal": False,
            })
            new_lineages.append(lid)

        return {
            "ok": True,
            "task_id": entry_id,
            "new_lineage_ids": new_lineages,
            "message": f"已注册 {len(new_lineages)} 条血缘关系",
        }

    def _resolve_sync_tables(self, catalog: str, scope: str) -> list[str]:
        base_tables = [
            "order_fact", "user_dim", "product_dim", "sales_agg_daily",
            "inventory_snapshot", "marketing_campaign", "web_traffic_log",
            "customer_360", "supplier_master", "finance_ledger",
        ]
        if scope and scope != "全部":
            return [t for t in base_tables if scope in t]
        return base_tables
