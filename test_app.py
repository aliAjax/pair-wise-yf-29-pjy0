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

    def test_full_custody_analysis_release_and_integrity_report(self):
        raw = b"bank statement original bytes"
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-001", "statement.csv",
            base64.b64encode(raw).decode(), self.retention, "custodian1",
        )
        opened = self.store.open_evidence("custodian1", item["id"], "A 区证物室", "两名人员在场开箱")
        self.assertEqual(opened["status"], "opened")
        child = self.store.derive(
            "analyst1", item["id"], "CSV 提取交易记录", "E-001-D1", "transactions.json",
            base64.b64encode(b'[{"amount": 100}]').decode(),
        )
        pending = self.store.transfer("custodian2", item["id"], "custodian2", "法院证物库", "封存后移交")
        self.assertEqual(pending["status"], "pending")
        self.store.confirm_transfer("custodian2", item["id"])
        self.store.release("custodian2", item["id"], "检察机关", "按调取令释放原件")
        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["overall_integrity_valid"])
        self.assertEqual(report["evidence_count"], 2)
        original = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertEqual(original["status"], "released")
        self.assertTrue(original["chain_valid"])
        self.assertEqual(child["parent_id"], item["id"])

    def test_permissions_and_legal_hold_block_release(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-002", "raw.bin",
            base64.b64encode(b"evidence").decode(), self.retention,
        )
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

    def test_two_step_transfer_blocks_operations_until_confirmed(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-003", "disk.img",
            base64.b64encode(b"disk image").decode(), self.retention,
        )
        pending = self.store.transfer("custodian1", item["id"], "custodian2", "B 区证物室", "例行移交")
        self.assertEqual(pending["status"], "pending")
        self.assertTrue(pending["transfer_no"])
        self.assertEqual(pending["current_custodian"], "custodian1")
        # 交接中：开箱、派生、释放、再次移交全部暂停
        blocked = [
            lambda: self.store.open_evidence("custodian1", item["id"], "A 区证物室"),
            lambda: self.store.derive("analyst1", item["id"], "提取文件", "E-003-D1", "a.bin",
                                      base64.b64encode(b"x").decode()),
            lambda: self.store.release("custodian1", item["id"], "检察机关"),
            lambda: self.store.transfer("custodian1", item["id"], "analyst1", "C 区证物室"),
        ]
        for op in blocked:
            with self.assertRaises(BusinessError) as ctx:
                op()
            self.assertEqual(ctx.exception.code, "transfer_pending")
        # 报告仍显示原保管人和待接收人
        report = self.store.report("auditor1", self.case["id"])
        entry = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertEqual(entry["current_custodian"], "custodian1")
        self.assertEqual(entry["pending_transfer"]["to_person"], "custodian2")
        self.assertEqual(entry["pending_transfer"]["transfer_no"], pending["transfer_no"])
        # 非接收人不能确认
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_transfer("custodian1", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        # 接收人确认后保管人才更新
        done = self.store.confirm_transfer("custodian2", item["id"])
        self.assertEqual(done["status"], "confirmed")
        self.assertEqual(done["current_custodian"], "custodian2")
        detail = self.store.get_evidence("custodian1", item["id"])
        self.assertEqual(detail["current_custodian"], "custodian2")
        self.assertIsNone(detail["pending_transfer"])
        self.assertEqual(detail["events"][-1]["event_type"], "TRANSFER")
        self.assertEqual(detail["events"][-1]["to_person"], "custodian2")

    def test_transfer_rejects_unknown_or_non_member_recipient(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-004", "log.txt",
            base64.b64encode(b"log").decode(), self.retention,
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.transfer("custodian1", item["id"], "nobody", "B 区证物室")
        self.assertEqual(ctx.exception.code, "unknown_recipient")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transfer("custodian1", item["id"], "outsider", "B 区证物室")
        self.assertEqual(ctx.exception.code, "invalid_recipient")

    def test_legal_hold_during_handover_keeps_pending_and_requires_resubmit(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-005", "mail.pst",
            base64.b64encode(b"mail").decode(), self.retention,
        )
        self.store.transfer("custodian1", item["id"], "custodian2", "B 区证物室")
        # 交接途中出现法律保留
        self.store.set_hold("auditor1", item["id"], True, "法院签发诉前保全裁定")
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_transfer("custodian2", item["id"])
        self.assertEqual(ctx.exception.code, "legal_hold_active")
        self.assertIn("法院签发诉前保全裁定", ctx.exception.message)
        # 待处理交接保留，拒绝原因可见
        detail = self.store.get_evidence("custodian2", item["id"])
        self.assertEqual(detail["pending_transfer"]["status"], "pending")
        self.assertIn("法院签发诉前保全裁定", detail["pending_transfer"]["last_reject_reason"])
        self.assertEqual(detail["current_custodian"], "custodian1")
        # 被拒绝后接收人不能直接确认，须发起人重新提交
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_transfer("custodian2", item["id"])
        self.assertEqual(ctx.exception.code, "transfer_rejected")
        # 保留未解除时不能重新提交
        with self.assertRaises(BusinessError) as ctx:
            self.store.resubmit_transfer("custodian1", item["id"])
        self.assertEqual(ctx.exception.code, "legal_hold_active")
        # 解除后仅原发起人可重新提交
        self.store.set_hold("auditor1", item["id"], False, "保全裁定已解除")
        with self.assertRaises(BusinessError) as ctx:
            self.store.resubmit_transfer("custodian2", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        resubmitted = self.store.resubmit_transfer("custodian1", item["id"])
        self.assertEqual(resubmitted["status"], "pending")
        detail = self.store.get_evidence("custodian1", item["id"])
        self.assertIsNone(detail["pending_transfer"]["last_reject_reason"])
        done = self.store.confirm_transfer("custodian2", item["id"])
        self.assertEqual(done["current_custodian"], "custodian2")

    def test_withdraw_transfer_releases_evidence(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-006", "photo.raw",
            base64.b64encode(b"photo").decode(), self.retention,
        )
        self.store.transfer("custodian1", item["id"], "custodian2", "B 区证物室")
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_transfer("custodian2", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        withdrawn = self.store.withdraw_transfer("custodian1", item["id"])
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertEqual(withdrawn["current_custodian"], "custodian1")
        detail = self.store.get_evidence("custodian1", item["id"])
        self.assertIsNone(detail["pending_transfer"])
        self.assertEqual(detail["transfers"][0]["status"], "withdrawn")
        # 撤回后证据恢复正常操作
        opened = self.store.open_evidence("custodian1", item["id"], "A 区证物室")
        self.assertEqual(opened["status"], "opened")


if __name__ == "__main__":
    unittest.main()
