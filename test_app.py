import base64
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, CustodyStore


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustodyStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.case = self.store.create_case("custodian1", "CASE-2026-001", "跨境资金调查")
        self.store.add_member("custodian1", self.case["id"], "custodian2", "custodian")
        self.store.add_member("custodian1", self.case["id"], "analyst1", "analyst")
        self.store.add_member("custodian1", self.case["id"], "auditor1", "auditor")
        self.retention = (date.today() + timedelta(days=3650)).isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def _ingest(self, label="E-001", custodian="custodian1", raw=b"bank statement original bytes"):
        return self.store.ingest_evidence(
            "custodian1", self.case["id"], label, "statement.csv",
            base64.b64encode(raw).decode(), self.retention, custodian,
        )

    def _start_transfer(self, item, frm="custodian1", to="custodian2", location="法院证物库"):
        return self.store.start_transfer(frm, item["id"], to, location, "封存后移交")

    def test_full_custody_analysis_two_step_transfer_release_and_report(self):
        item = self._ingest()
        opened = self.store.open_evidence("custodian1", item["id"], "A 区证物室", "两名人员在场开箱")
        self.assertEqual(opened["status"], "opened")
        # opened 状态可派生；先在开箱后做一次交接以便 custodian2 成为保管人
        handover = self._start_transfer(item)
        self.assertEqual(handover["status"], "in_transit")
        self.assertEqual(handover["current_custodian"], "custodian1")
        self.assertTrue(handover["handover_number"].startswith("HO-"))
        confirmed = self.store.confirm_transfer("custodian2", item["id"])
        self.assertEqual(confirmed["handover_status"], "confirmed")
        self.assertEqual(confirmed["status"], "opened")
        self.assertEqual(confirmed["current_custodian"], "custodian2")
        child = self.store.derive(
            "analyst1", item["id"], "CSV 提取交易记录", "E-001-D1", "transactions.json",
            base64.b64encode(b'[{"amount": 100}]').decode(),
        )
        self.store.release("custodian2", item["id"], "检察机关", "按调取令释放原件")
        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["overall_integrity_valid"])
        self.assertEqual(report["evidence_count"], 2)
        original = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertEqual(original["status"], "released")
        self.assertEqual(original["current_custodian"], "custodian2")
        self.assertTrue(original["chain_valid"])
        self.assertIsNone(original["active_handover"])
        self.assertEqual(original["handovers"][0]["status"], "confirmed")
        self.assertEqual(child["parent_id"], item["id"])

    def test_permissions_and_legal_hold_block_release(self):
        item = self._ingest("E-002", raw=b"evidence")
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_evidence("outsider", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("analyst1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.status, 403)
        self.store.set_hold("auditor1", item["id"], True, "诉讼保全要求")
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.code, "legal_hold_active")

    def test_handover_waits_for_confirmation_and_blocks_actions(self):
        item = self._ingest("E-003", raw=b"sealed evidence")
        handover = self._start_transfer(item)
        # 保管人在确认前不变，证据进入交接中
        detail = self.store.get_evidence("custodian1", item["id"])
        self.assertEqual(detail["current_custodian"], "custodian1")
        self.assertEqual(detail["status"], "in_transit")
        self.assertEqual(detail["active_handover"]["to_person"], "custodian2")
        self.assertEqual(detail["active_handover"]["status"], "pending")
        # 报告显示原保管人和待接收人
        report = self.store.report("auditor1", self.case["id"])
        ev = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertEqual(ev["current_custodian"], "custodian1")
        self.assertEqual(ev["pending_recipient"], "custodian2")
        # 开箱、派生、释放、再次移交均暂停
        with self.assertRaises(BusinessError) as ctx:
            self.store.open_evidence("custodian1", item["id"], "A 区证物室")
        self.assertEqual(ctx.exception.code, "handover_active")
        with self.assertRaises(BusinessError) as ctx:
            self.store.start_transfer("custodian1", item["id"], "custodian2", "证物库")
        self.assertEqual(ctx.exception.code, "handover_active")
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.code, "handover_active")
        # custodian1 先撤回以便开箱，再重新发起（交接中派生也被拦截的验证）
        self.store.withdraw_transfer("custodian1", item["id"])
        self.store.open_evidence("custodian1", item["id"], "A 区证物室")
        self._start_transfer(item)
        with self.assertRaises(BusinessError) as ctx:
            self.store.derive("analyst1", item["id"], "提取哈希", "D1", "x.json",
                              base64.b64encode(b"{}").decode())
        self.assertEqual(ctx.exception.code, "handover_active")
        # 非接收人不能确认
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_transfer("custodian1", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        # 接错人可以被接收人拒绝：撤回后重新发给正确的人
        self.store.withdraw_transfer("custodian1", item["id"])
        handover2 = self._start_transfer(item, to="custodian2")
        self.assertEqual(handover2["handover_status"], "pending")
        confirmed = self.store.confirm_transfer("custodian2", item["id"])
        self.assertEqual(confirmed["current_custodian"], "custodian2")
        transfers = self.store.get_transfers("auditor1", item["id"])
        self.assertIsNone(transfers["active_handover"])
        self.assertEqual([h["status"] for h in transfers["handovers"]],
                         ["withdrawn", "withdrawn", "confirmed"])

    def test_legal_hold_during_handover_blocks_confirm_until_resubmit(self):
        item = self._ingest("E-004", raw=b"held during transit")
        self._start_transfer(item)
        # 交接途中设置法律保留：交接被冻结为 blocked
        result = self.store.set_hold("auditor1", item["id"], True, "检察机关补充扣押令")
        self.assertEqual(result["handover_status"], "blocked")
        detail = self.store.get_evidence("custodian2", item["id"])
        self.assertEqual(detail["active_handover"]["status"], "blocked")
        self.assertIn("补充扣押令", detail["active_handover"]["hold_reason"])
        # 接收人确认被拒绝，返回原因且保留待处理交接
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_transfer("custodian2", item["id"])
        self.assertEqual(ctx.exception.code, "handover_blocked")
        self.assertIn("补充扣押令", ctx.exception.message)
        detail = self.store.get_evidence("custodian2", item["id"])
        self.assertEqual(detail["active_handover"]["status"], "blocked")
        self.assertIn("补充扣押令", detail["active_handover"]["reject_reason"])
        # 保留解除后确认仍然不行，必须由原发起人重新提交
        self.store.set_hold("auditor1", item["id"], False, "扣押令到期解除保留")
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_transfer("custodian2", item["id"])
        self.assertEqual(ctx.exception.code, "handover_blocked")
        # 只有原发起人可以重新提交
        with self.assertRaises(BusinessError) as ctx:
            self.store.resubmit_transfer("custodian2", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        resubmitted = self.store.resubmit_transfer("custodian1", item["id"])
        self.assertEqual(resubmitted["handover_status"], "pending")
        detail = self.store.get_evidence("custodian2", item["id"])
        self.assertEqual(detail["active_handover"]["status"], "pending")
        self.assertEqual(detail["active_handover"]["reject_reason"], "")
        self.assertEqual(detail["active_handover"]["resubmitted_by"], "custodian1")
        confirmed = self.store.confirm_transfer("custodian2", item["id"])
        self.assertEqual(confirmed["handover_status"], "confirmed")
        self.assertEqual(confirmed["current_custodian"], "custodian2")
        report = self.store.report("auditor1", self.case["id"])
        ev = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertTrue(ev["chain_valid"])
        self.assertEqual(ev["handovers"][0]["status"], "confirmed")

    def test_withdraw_restores_status_and_only_initiator(self):
        item = self._ingest("E-005", raw=b"withdraw me")
        self._start_transfer(item)
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_transfer("custodian2", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        withdrawn = self.store.withdraw_transfer("custodian1", item["id"])
        self.assertEqual(withdrawn["status"], "custody")
        detail = self.store.get_evidence("custodian1", item["id"])
        self.assertEqual(detail["status"], "custody")
        self.assertIsNone(detail["active_handover"])
        self.assertEqual(len(detail["handovers"]), 1)
        self.assertEqual(detail["handovers"][0]["status"], "withdrawn")

    def test_cannot_start_transfer_under_hold_or_to_non_custodian(self):
        item = self._ingest("E-006", raw=b"rules")
        self.store.set_hold("auditor1", item["id"], True, "立案初期保全措施")
        with self.assertRaises(BusinessError) as ctx:
            self.store.start_transfer("custodian1", item["id"], "custodian2", "证物库")
        self.assertEqual(ctx.exception.code, "legal_hold_active")
        self.store.set_hold("auditor1", item["id"], False, "保全措施解除完毕")
        with self.assertRaises(BusinessError) as ctx:
            self.store.start_transfer("custodian1", item["id"], "analyst1", "证物库")
        self.assertEqual(ctx.exception.code, "invalid_recipient")
        with self.assertRaises(BusinessError) as ctx:
            self.store.start_transfer("custodian1", item["id"], "outsider", "证物库")
        self.assertEqual(ctx.exception.code, "invalid_recipient")
        with self.assertRaises(BusinessError) as ctx:
            self.store.start_transfer("custodian1", item["id"], "ghost-user", "证物库")
        self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(BusinessError) as ctx:
            self.store.start_transfer("custodian1", item["id"], "custodian1", "证物库")
        self.assertEqual(ctx.exception.code, "invalid_transfer")


if __name__ == "__main__":
    unittest.main()
