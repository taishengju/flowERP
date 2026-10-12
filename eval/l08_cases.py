"""L08 新增业务检查：整单原子预占（多行成功、第二行缺货、写入中途失败）。

由课程侧工具 l08_practice.py 复制进隔离候选的 eval/ 目录，并登记到 EVALS。
本文件属课程侧改编件，非课程原版。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from flowerp import ERPStore, InventoryService, MasterDataService, SalesService
from flowerp.identity import IdentityService, SYSTEM_PRINCIPAL
from flowerp.models import InsufficientStock

LOC = "LOC-MAIN-STOCK"


def _fixture(tmp, stock_a: int, stock_b: int):
    store = ERPStore(Path(tmp.name) / "l08.db")
    IdentityService(store).ensure_local_defaults()
    master = MasterDataService(store)
    inventory = InventoryService(store)
    sales = SalesService(store)
    a = master.create_product(SYSTEM_PRINCIPAL, "L08-A", "L08 验收商品A", 1000, 500)
    b = master.create_product(SYSTEM_PRINCIPAL, "L08-B", "L08 验收商品B", 1000, 500)
    customer = master.create_customer(SYSTEM_PRINCIPAL, "L08-CUS", "L08 验收客户", credit_limit_cents=1000000)
    inventory.receive(SYSTEM_PRINCIPAL, a["id"], LOC, stock_a, "l08-opening-a")
    inventory.receive(SYSTEM_PRINCIPAL, b["id"], LOC, stock_b, "l08-opening-b")
    order = sales.create_order(SYSTEM_PRINCIPAL, customer["id"], [
        {"product_id": a["id"], "quantity": 2},
        {"product_id": b["id"], "quantity": 3},
    ])
    sales.confirm(SYSTEM_PRINCIPAL, order["id"])
    return store, inventory, sales, order, {"A": a["id"], "B": b["id"]}


def _tables(store, inventory, products, order_id):
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT ordered_quantity, reserved_quantity FROM sales_document_lines WHERE document_id=? ORDER BY line_number",
            (order_id,),
        ).fetchall()
        reservations = conn.execute(
            "SELECT COUNT(*) FROM stock_reservations WHERE reference_id=?", (order_id,)
        ).fetchone()[0]
        status = conn.execute("SELECT status FROM sales_documents WHERE id=?", (order_id,)).fetchone()["status"]
    stocks = {key: inventory.balance(SYSTEM_PRINCIPAL, pid, LOC) for key, pid in products.items()}
    return {
        "available": {key: value["available"] for key, value in stocks.items()},
        "reserved": {key: value["reserved"] for key, value in stocks.items()},
        "reservations": reservations,
        "line_reserved": [row["reserved_quantity"] for row in rows],
        "status": status,
    }


def _assert_unchanged(tables, stock_a: int, stock_b: int, label: str) -> None:
    assert tables["available"] == {"A": stock_a, "B": stock_b}, f"{label}：可用量应不变，实际 {tables['available']}"
    assert tables["reserved"] == {"A": 0, "B": 0}, f"{label}：不应有预占量，实际 {tables['reserved']}"
    assert tables["reservations"] == 0, f"{label}：预占记录应为 0 条，实际 {tables['reservations']}"
    assert tables["line_reserved"] == [0, 0], f"{label}：订单明细已预占应为 [0, 0]，实际 {tables['line_reserved']}"
    assert tables["status"] == "confirmed", f"{label}：订单状态应保持 confirmed，实际 {tables['status']}"


def l08_multi_line_reservation_succeeds() -> str:
    tmp = tempfile.TemporaryDirectory(prefix="l08-eval-multi-")
    try:
        store, inventory, sales, order, products = _fixture(tmp, 5, 5)
        sales.reserve(SYSTEM_PRINCIPAL, order["id"])
        tables = _tables(store, inventory, products, order["id"])
        assert tables["available"] == {"A": 3, "B": 2}, f"可用量应 A3/B2，实际 {tables['available']}"
        assert tables["reserved"] == {"A": 2, "B": 3}, f"预占量应 A2/B3，实际 {tables['reserved']}"
        assert tables["reservations"] == 2, f"预占记录应为 2 条，实际 {tables['reservations']}"
        assert tables["line_reserved"] == [2, 3], f"订单明细已预占应 [2, 3]，实际 {tables['line_reserved']}"
        assert tables["status"] == "reserved", f"订单状态应为 reserved，实际 {tables['status']}"
        return "多行订单整单预占成功：可用 A3/B2、预占 A2/B3、预占记录 2 条、订单 reserved"
    finally:
        tmp.cleanup()


def l08_second_line_shortage_rolls_back() -> str:
    tmp = tempfile.TemporaryDirectory(prefix="l08-eval-short-")
    try:
        store, inventory, sales, order, products = _fixture(tmp, 5, 2)
        raised = None
        try:
            sales.reserve(SYSTEM_PRINCIPAL, order["id"])
        except InsufficientStock:
            raised = "InsufficientStock"
        if raised is None:
            raise AssertionError("第二行缺货未被阻断：整单预占不应成功")
        tables = _tables(store, inventory, products, order["id"])
        _assert_unchanged(tables, 5, 2, "第二行缺货")
        return "第二行缺货整单拒绝：四表完全不变（A5/B2、预占 0、明细 0、订单 confirmed）"
    finally:
        tmp.cleanup()


def l08_midwrite_failure_rolls_back() -> str:
    tmp = tempfile.TemporaryDirectory(prefix="l08-eval-mid-")
    try:
        store, inventory, sales, order, products = _fixture(tmp, 5, 5)
        with store.connect() as conn:
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS l08_block_second BEFORE INSERT ON stock_reservations "
                "WHEN (SELECT COUNT(*) FROM stock_reservations) >= 1 "
                "BEGIN SELECT RAISE(ABORT, 'L08 injected failure on second reservation'); END"
            )
        failure = None
        try:
            sales.reserve(SYSTEM_PRINCIPAL, order["id"])
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
        if failure is None:
            raise AssertionError("注入的第二条预占写入失败没有发生")
        tables = _tables(store, inventory, products, order["id"])
        _assert_unchanged(tables, 5, 5, "写入中途失败")
        return f"写入中途失败整单回滚：四表回到原样（注入触发 {failure}）"
    finally:
        tmp.cleanup()