"""法律证据保管与流转后台。"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "custody.db"
MEMBER_ROLES = {"custodian", "analyst", "auditor"}


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CustodyStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS cases(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, case_number TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_members(
                    case_id INTEGER NOT NULL REFERENCES cases(id), user_id TEXT NOT NULL REFERENCES users(id),
                    role TEXT NOT NULL CHECK(role IN ('custodian','analyst','auditor')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                    granted_by TEXT NOT NULL REFERENCES users(id), granted_at TEXT NOT NULL,
                    PRIMARY KEY(case_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id), label TEXT NOT NULL,
                    filename TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                    content BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'custody'
                        CHECK(status IN ('custody','opened','released','derivative')),
                    current_custodian TEXT NOT NULL, legal_hold INTEGER NOT NULL DEFAULT 0 CHECK(legal_hold IN (0,1)),
                    retention_until TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, UNIQUE(case_id,label)
                );
                CREATE TABLE IF NOT EXISTS custody_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id), sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type IN ('INGEST','TRANSFER','OPEN','ANALYZE','RELEASE','HOLD_SET','HOLD_CLEARED')),
                    actor_id TEXT NOT NULL REFERENCES users(id), from_person TEXT,
                    to_person TEXT, location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                    previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(evidence_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS derivatives(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_evidence_id INTEGER NOT NULL REFERENCES evidence(id),
                    child_evidence_id INTEGER NOT NULL UNIQUE REFERENCES evidence(id),
                    method TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, UNIQUE(parent_evidence_id,child_evidence_id)
                );
                CREATE TABLE IF NOT EXISTS transfers(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transfer_no TEXT UNIQUE,
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
                    from_person TEXT NOT NULL, to_person TEXT NOT NULL,
                    location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','withdrawn')),
                    initiated_by TEXT NOT NULL REFERENCES users(id),
                    last_reject_reason TEXT,
                    decided_by TEXT, decided_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_transfers_pending ON transfers(evidence_id,status);
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id), actor_id TEXT NOT NULL REFERENCES users(id),
                    action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name) VALUES(?,?)",
                [
                    ("custodian1", "证据保管员甲"), ("custodian2", "证据保管员乙"),
                    ("analyst1", "电子数据分析员"), ("auditor1", "案件审计员"), ("outsider", "外部人员"),
                ],
            )

    def _user(self, conn, user_id):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在或已停用", 401, "unknown_user")
        return user

    def _case(self, conn, case_id):
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise BusinessError("案件不存在", 404, "not_found")
        return row

    def _member(self, conn, case_id, user_id, roles=None):
        user = self._user(conn, user_id)
        self._case(conn, case_id)
        row = conn.execute(
            "SELECT * FROM case_members WHERE case_id=? AND user_id=? AND active=1", (case_id, user_id)
        ).fetchone()
        if not row:
            raise BusinessError("不是案件有效成员", 403, "forbidden")
        if roles and row["role"] not in roles:
            raise BusinessError("当前案件角色无权执行此操作", 403, "forbidden")
        return user, row

    def _audit(self, conn, case_id, actor_id, action, detail):
        conn.execute(
            "INSERT INTO audit_log(case_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (case_id, actor_id, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def create_case(self, user_id, case_number, title):
        if not case_number.strip() or len(title.strip()) < 2:
            raise BusinessError("案件编号和标题不能为空", 422, "invalid_case")
        with self.connect() as conn:
            self._user(conn, user_id)
            try:
                cur = conn.execute(
                    "INSERT INTO cases(case_number,title,created_by,created_at) VALUES(?,?,?,?)",
                    (case_number.strip(), title.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("案件编号已存在", 409, "case_exists")
            case_id = cur.lastrowid
            conn.execute(
                "INSERT INTO case_members(case_id,user_id,role,granted_by,granted_at) VALUES(?,?,?,?,?)",
                (case_id, user_id, "custodian", user_id, now()),
            )
            self._audit(conn, case_id, user_id, "case.create", {"case_number": case_number.strip()})
            return {"id": case_id, "case_number": case_number.strip(), "title": title.strip()}

    def add_member(self, user_id, case_id, member_id, role):
        if role not in MEMBER_ROLES:
            raise BusinessError("案件角色必须是 custodian、analyst 或 auditor", 422, "invalid_role")
        with self.connect() as conn:
            case = self._case(conn, case_id)
            if case["created_by"] != user_id:
                raise BusinessError("只有案件创建人可以授权成员", 403, "forbidden")
            self._user(conn, member_id)
            conn.execute(
                """INSERT INTO case_members(case_id,user_id,role,active,granted_by,granted_at) VALUES(?,?,?,1,?,?)
                   ON CONFLICT(case_id,user_id) DO UPDATE SET role=excluded.role,active=1,granted_by=excluded.granted_by,granted_at=excluded.granted_at""",
                (case_id, member_id, role, user_id, now()),
            )
            self._audit(conn, case_id, user_id, "member.grant", {"member_id": member_id, "role": role})
            return {"case_id": case_id, "member_id": member_id, "role": role}

    @staticmethod
    def _event_hash(event):
        canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()

    def _append_event(self, conn, evidence_id, event_type, actor, from_person="", to_person="", location="", note=""):
        previous = conn.execute(
            "SELECT event_hash,sequence FROM custody_events WHERE evidence_id=? ORDER BY sequence DESC LIMIT 1", (evidence_id,)
        ).fetchone()
        sequence = (previous["sequence"] + 1) if previous else 1
        previous_hash = previous["event_hash"] if previous else "GENESIS"
        payload = {
            "evidence_id": evidence_id, "sequence": sequence, "event_type": event_type,
            "actor_id": actor, "from_person": from_person or None, "to_person": to_person or None,
            "location": location, "note": note, "previous_hash": previous_hash, "created_at": now(),
        }
        digest = self._event_hash(payload)
        cur = conn.execute(
            """INSERT INTO custody_events(evidence_id,sequence,event_type,actor_id,from_person,to_person,location,note,previous_hash,event_hash,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (evidence_id, sequence, event_type, actor, from_person or None, to_person or None, location, note, previous_hash, digest, payload["created_at"]),
        )
        return cur.lastrowid, digest

    def ingest_evidence(self, user_id, case_id, label, filename, content_b64, retention_until, custodian=None):
        label, filename = label.strip(), filename.strip()
        if not label or not filename:
            raise BusinessError("证据标签和文件名不能为空", 422, "invalid_evidence")
        try:
            content = base64.b64decode(content_b64, validate=True)
            deadline = date.fromisoformat(retention_until)
        except (binascii.Error, ValueError, TypeError):
            raise BusinessError("证据内容 Base64 或保留期限格式错误", 422, "invalid_evidence")
        if deadline < date.today():
            raise BusinessError("保留期限不能早于今天", 422, "invalid_retention")
        digest = hashlib.sha256(content).hexdigest()
        custodian = (custodian or user_id).strip()
        with self.connect() as conn:
            _, member = self._member(conn, case_id, user_id, {"custodian"})
            if not custodian:
                raise BusinessError("保管人不能为空", 422, "invalid_custodian")
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """INSERT INTO evidence(case_id,label,filename,sha256,size,content,current_custodian,retention_until,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (case_id, label, filename, digest, len(content), content, custodian, retention_until, user_id, now()),
                )
                evidence_id = cur.lastrowid
                self._append_event(conn, evidence_id, "INGEST", user_id, to_person=custodian, note=f"入册 SHA-256 {digest}")
                self._audit(conn, case_id, user_id, "evidence.ingest", {"evidence_id": evidence_id, "sha256": digest, "label": label})
                return {"id": evidence_id, "label": label, "sha256": digest, "size": len(content), "status": "custody", "current_custodian": custodian}
            except sqlite3.IntegrityError:
                conn.rollback()
                raise BusinessError("该案件中的证据标签已存在", 409, "label_exists")
            except Exception:
                conn.rollback()
                raise

    def _evidence(self, conn, evidence_id):
        row = conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if not row:
            raise BusinessError("证据不存在", 404, "not_found")
        return row

    def get_evidence(self, user_id, evidence_id, include_content=False):
        with self.connect() as conn:
            row = self._evidence(conn, evidence_id)
            self._member(conn, row["case_id"], user_id)
            result = {k: row[k] for k in row.keys() if k != "content"}
            result["legal_hold"] = bool(row["legal_hold"])
            result["integrity_valid"] = hashlib.sha256(row["content"]).hexdigest() == row["sha256"]
            result["events"] = [dict(x) for x in conn.execute("SELECT * FROM custody_events WHERE evidence_id=? ORDER BY sequence", (evidence_id,)).fetchall()]
            result["derived_children"] = [dict(x) for x in conn.execute("SELECT * FROM derivatives WHERE parent_evidence_id=? ORDER BY id", (evidence_id,)).fetchall()]
            result["pending_transfer"] = self._transfer_dict(self._pending_transfer(conn, evidence_id))
            result["transfers"] = [dict(x) for x in conn.execute("SELECT * FROM transfers WHERE evidence_id=? ORDER BY id DESC", (evidence_id,)).fetchall()]
            if include_content:
                result["content_b64"] = base64.b64encode(row["content"]).decode()
            return result

    def _pending_transfer(self, conn, evidence_id):
        return conn.execute(
            "SELECT * FROM transfers WHERE evidence_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (evidence_id,)
        ).fetchone()

    def _require_no_pending_transfer(self, conn, evidence_id, action):
        if self._pending_transfer(conn, evidence_id):
            raise BusinessError(f"证据处于交接中，{action}已暂停", 409, "transfer_pending")

    def _latest_hold_reason(self, conn, evidence_id):
        row = conn.execute(
            "SELECT note FROM custody_events WHERE evidence_id=? AND event_type='HOLD_SET' ORDER BY sequence DESC LIMIT 1",
            (evidence_id,),
        ).fetchone()
        return row["note"] if row else "未记录原因"

    @staticmethod
    def _transfer_dict(row):
        return dict(row) if row else None

    def transfer(self, user_id, evidence_id, to_person, location, note=""):
        to_person, location, note = (to_person or "").strip(), (location or "").strip(), (note or "").strip()
        if not to_person or not location:
            raise BusinessError("接收人和保管位置不能为空", 422, "invalid_transfer")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                self._member(conn, row["case_id"], user_id, {"custodian"})
                if row["status"] == "released":
                    raise BusinessError("已释放证据不能再移交", 409, "evidence_released")
                self._require_no_pending_transfer(conn, evidence_id, "再次移交")
                recipient = conn.execute("SELECT id FROM users WHERE id=? AND active=1", (to_person,)).fetchone()
                if not recipient:
                    raise BusinessError("接收人不存在或已停用", 422, "unknown_recipient")
                member = conn.execute(
                    "SELECT 1 FROM case_members WHERE case_id=? AND user_id=? AND active=1", (row["case_id"], to_person)
                ).fetchone()
                if not member:
                    raise BusinessError("接收人不是本案有效成员", 422, "invalid_recipient")
                ts = now()
                cur = conn.execute(
                    """INSERT INTO transfers(evidence_id,from_person,to_person,location,note,initiated_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (evidence_id, row["current_custodian"], to_person, location, note, user_id, ts, ts),
                )
                transfer_id = cur.lastrowid
                transfer_no = f"TR-{datetime.now(timezone.utc):%Y%m%d}-{transfer_id:04d}"
                conn.execute("UPDATE transfers SET transfer_no=? WHERE id=?", (transfer_no, transfer_id))
                self._audit(conn, row["case_id"], user_id, "custody.transfer.initiate",
                            {"evidence_id": evidence_id, "transfer_no": transfer_no,
                             "from": row["current_custodian"], "to": to_person, "location": location})
                return {"id": evidence_id, "transfer_no": transfer_no, "status": "pending",
                        "from_person": row["current_custodian"], "to_person": to_person,
                        "current_custodian": row["current_custodian"], "location": location}
            except Exception:
                conn.rollback()
                raise

    def confirm_transfer(self, user_id, evidence_id):
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                self._user(conn, user_id)
                transfer = self._pending_transfer(conn, evidence_id)
                if not transfer:
                    raise BusinessError("该证据没有待处理的交接", 409, "no_pending_transfer")
                if transfer["to_person"] != user_id:
                    raise BusinessError("只有待接收人可以确认交接", 403, "forbidden")
                if transfer["last_reject_reason"]:
                    raise BusinessError("交接因法律保留被拒绝，需原发起人重新提交", 409, "transfer_rejected")
                if row["legal_hold"]:
                    reason = self._latest_hold_reason(conn, evidence_id)
                    message = f"存在法律保留，无法确认交接：{reason}"
                    conn.execute("UPDATE transfers SET last_reject_reason=?, updated_at=? WHERE id=?",
                                 (message, now(), transfer["id"]))
                    self._audit(conn, row["case_id"], user_id, "custody.transfer.confirm_rejected",
                                {"evidence_id": evidence_id, "transfer_no": transfer["transfer_no"], "reason": reason})
                    conn.commit()
                    raise BusinessError(message, 409, "legal_hold_active")
                ts = now()
                conn.execute("UPDATE evidence SET current_custodian=? WHERE id=?", (transfer["to_person"], evidence_id))
                conn.execute("UPDATE transfers SET status='confirmed', decided_by=?, decided_at=?, updated_at=? WHERE id=?",
                             (user_id, ts, ts, transfer["id"]))
                note = f"交接单 {transfer['transfer_no']} 经接收人确认"
                if transfer["note"]:
                    note += f"；{transfer['note']}"
                self._append_event(conn, evidence_id, "TRANSFER", user_id,
                                   from_person=transfer["from_person"], to_person=transfer["to_person"],
                                   location=transfer["location"], note=note)
                self._audit(conn, row["case_id"], user_id, "custody.transfer.confirm",
                            {"evidence_id": evidence_id, "transfer_no": transfer["transfer_no"],
                             "from": transfer["from_person"], "to": transfer["to_person"]})
                return {"id": evidence_id, "transfer_no": transfer["transfer_no"], "status": "confirmed",
                        "current_custodian": transfer["to_person"]}
            except Exception:
                conn.rollback()
                raise

    def withdraw_transfer(self, user_id, evidence_id):
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                self._user(conn, user_id)
                transfer = self._pending_transfer(conn, evidence_id)
                if not transfer:
                    raise BusinessError("该证据没有待处理的交接", 409, "no_pending_transfer")
                if transfer["initiated_by"] != user_id:
                    raise BusinessError("只有交接发起人可以撤回", 403, "forbidden")
                ts = now()
                conn.execute("UPDATE transfers SET status='withdrawn', decided_by=?, decided_at=?, updated_at=? WHERE id=?",
                             (user_id, ts, ts, transfer["id"]))
                self._audit(conn, row["case_id"], user_id, "custody.transfer.withdraw",
                            {"evidence_id": evidence_id, "transfer_no": transfer["transfer_no"]})
                return {"id": evidence_id, "transfer_no": transfer["transfer_no"], "status": "withdrawn",
                        "current_custodian": row["current_custodian"]}
            except Exception:
                conn.rollback()
                raise

    def resubmit_transfer(self, user_id, evidence_id):
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                self._user(conn, user_id)
                transfer = self._pending_transfer(conn, evidence_id)
                if not transfer:
                    raise BusinessError("该证据没有待处理的交接", 409, "no_pending_transfer")
                if transfer["initiated_by"] != user_id:
                    raise BusinessError("只有原发起人可以重新提交交接", 403, "forbidden")
                if row["legal_hold"]:
                    raise BusinessError("法律保留仍生效，无法重新提交交接", 409, "legal_hold_active")
                conn.execute("UPDATE transfers SET last_reject_reason=NULL, updated_at=? WHERE id=?",
                             (now(), transfer["id"]))
                self._audit(conn, row["case_id"], user_id, "custody.transfer.resubmit",
                            {"evidence_id": evidence_id, "transfer_no": transfer["transfer_no"]})
                return {"id": evidence_id, "transfer_no": transfer["transfer_no"], "status": "pending",
                        "to_person": transfer["to_person"]}
            except Exception:
                conn.rollback()
                raise

    def open_evidence(self, user_id, evidence_id, location, note=""):
        if not location.strip():
            raise BusinessError("开箱地点不能为空", 422, "location_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                _, member = self._member(conn, row["case_id"], user_id, {"custodian"})
                self._require_no_pending_transfer(conn, evidence_id, "开箱")
                if row["status"] != "custody":
                    raise BusinessError("只有处于封存保管状态的证据可以开箱", 409, "invalid_status")
                self._append_event(conn, evidence_id, "OPEN", user_id, from_person=row["current_custodian"], location=location.strip(), note=note.strip())
                conn.execute("UPDATE evidence SET status='opened' WHERE id=?", (evidence_id,))
                self._audit(conn, row["case_id"], user_id, "evidence.open", {"evidence_id": evidence_id, "location": location.strip()})
                return {"id": evidence_id, "status": "opened", "location": location.strip()}
            except Exception:
                conn.rollback()
                raise

    def derive(self, user_id, evidence_id, method, label, filename, content_b64):
        if len(method.strip()) < 3 or not label.strip() or not filename.strip():
            raise BusinessError("分析方法、子证据标签和文件名不能为空", 422, "invalid_derivative")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                parent = self._evidence(conn, evidence_id)
                _, member = self._member(conn, parent["case_id"], user_id, {"analyst"})
                self._require_no_pending_transfer(conn, evidence_id, "派生分析")
                if parent["status"] != "opened":
                    raise BusinessError("原始证据必须先开箱才能分析", 409, "evidence_not_opened")
                cur = conn.execute(
                    """INSERT INTO evidence(case_id,label,filename,sha256,size,content,status,current_custodian,legal_hold,retention_until,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (parent["case_id"], label.strip(), filename.strip(), digest, len(content), content, "derivative", user_id, 0, parent["retention_until"], user_id, now()),
                )
                child_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO derivatives(parent_evidence_id,child_evidence_id,method,actor_id,created_at) VALUES(?,?,?,?,?)",
                    (evidence_id, child_id, method.strip(), user_id, now()),
                )
                self._append_event(conn, evidence_id, "ANALYZE", user_id, from_person=parent["current_custodian"], note=f"生成衍生证据 #{child_id}: {method.strip()}")
                self._append_event(conn, child_id, "INGEST", user_id, from_person=parent["current_custodian"], to_person=user_id, note=f"由证据 #{evidence_id} 派生，SHA-256 {digest}")
                self._audit(conn, parent["case_id"], user_id, "evidence.derive", {"parent_id": evidence_id, "child_id": child_id, "method": method.strip(), "sha256": digest})
                return {"id": child_id, "parent_id": evidence_id, "label": label.strip(), "sha256": digest, "status": "derivative"}
            except sqlite3.IntegrityError:
                conn.rollback()
                raise BusinessError("衍生证据标签已存在", 409, "label_exists")
            except Exception:
                conn.rollback()
                raise

    def set_hold(self, user_id, evidence_id, hold, reason):
        if len(reason.strip()) < 5:
            raise BusinessError("法律保留原因至少 5 字", 422, "reason_required")
        with self.connect() as conn:
            row = self._evidence(conn, evidence_id)
            case = self._case(conn, row["case_id"])
            if user_id != case["created_by"]:
                self._member(conn, row["case_id"], user_id, {"auditor"})
            conn.execute("UPDATE evidence SET legal_hold=? WHERE id=?", (int(bool(hold)), evidence_id))
            event = "HOLD_SET" if hold else "HOLD_CLEARED"
            self._append_event(conn, evidence_id, event, user_id, note=reason.strip())
            self._audit(conn, row["case_id"], user_id, "evidence.hold", {"evidence_id": evidence_id, "hold": bool(hold), "reason": reason.strip()})
            return {"id": evidence_id, "legal_hold": bool(hold)}

    def release(self, user_id, evidence_id, recipient, note=""):
        if not recipient.strip():
            raise BusinessError("接收方不能为空", 422, "recipient_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                _, member = self._member(conn, row["case_id"], user_id, {"custodian"})
                self._require_no_pending_transfer(conn, evidence_id, "释放")
                if row["legal_hold"]:
                    raise BusinessError("存在法律保留，禁止释放证据", 409, "legal_hold_active")
                if row["status"] == "released":
                    raise BusinessError("证据已经释放", 409, "already_released")
                self._append_event(conn, evidence_id, "RELEASE", user_id, from_person=row["current_custodian"], to_person=recipient.strip(), note=note.strip())
                conn.execute("UPDATE evidence SET status='released' WHERE id=?", (evidence_id,))
                self._audit(conn, row["case_id"], user_id, "evidence.release", {"evidence_id": evidence_id, "recipient": recipient.strip()})
                return {"id": evidence_id, "status": "released", "recipient": recipient.strip()}
            except Exception:
                conn.rollback()
                raise

    def report(self, user_id, case_id):
        with self.connect() as conn:
            self._member(conn, case_id, user_id)
            case = self._case(conn, case_id)
            items, all_valid = [], True
            for row in conn.execute("SELECT * FROM evidence WHERE case_id=? ORDER BY id", (case_id,)).fetchall():
                hash_valid = hashlib.sha256(row["content"]).hexdigest() == row["sha256"]
                events = conn.execute("SELECT * FROM custody_events WHERE evidence_id=? ORDER BY sequence", (row["id"],)).fetchall()
                expected_prev, chain_valid = "GENESIS", True
                for e in events:
                    payload = {
                        "evidence_id": e["evidence_id"], "sequence": e["sequence"], "event_type": e["event_type"],
                        "actor_id": e["actor_id"], "from_person": e["from_person"], "to_person": e["to_person"],
                        "location": e["location"], "note": e["note"], "previous_hash": e["previous_hash"], "created_at": e["created_at"],
                    }
                    if e["previous_hash"] != expected_prev or self._event_hash(payload) != e["event_hash"]:
                        chain_valid = False
                    expected_prev = e["event_hash"]
                all_valid = all_valid and hash_valid and chain_valid
                items.append({
                    "id": row["id"], "label": row["label"], "filename": row["filename"], "sha256": row["sha256"],
                    "size": row["size"], "status": row["status"], "current_custodian": row["current_custodian"],
                    "legal_hold": bool(row["legal_hold"]), "retention_until": row["retention_until"],
                    "pending_transfer": self._transfer_dict(self._pending_transfer(conn, row["id"])),
                    "hash_valid": hash_valid, "chain_valid": chain_valid,
                    "events": [dict(e) for e in events],
                    "derivatives": [dict(x) for x in conn.execute("SELECT * FROM derivatives WHERE parent_evidence_id=? ORDER BY id", (row["id"],)).fetchall()],
                })
            audit = conn.execute("SELECT * FROM audit_log WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
            return {
                "case": dict(case), "generated_at": now(), "overall_integrity_valid": all_valid,
                "evidence_count": len(items), "evidence": items,
                "audit": [dict(a) | {"detail": json.loads(a["detail"])} for a in audit],
            }


class Handler(BaseHTTPRequestHandler):
    server_version = "EvidenceCustody/1.0"
    def _store(self): return self.server.store  # type: ignore[attr-defined]
    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try: data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError): raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict): raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data
    def _send(self, status, payload):
        body=json.dumps(payload,ensure_ascii=False).encode(); self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def _dispatch(self, method):
        path=urlparse(self.path).path.rstrip("/") or "/"; parts=[p for p in path.split("/") if p]
        user=self.headers.get("X-User-Id",""); store=self._store()
        if method=="GET" and path=="/":
            body=(BASE_DIR/"web"/"index.html").read_bytes(); self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if method=="GET" and path=="/health": return self._send(200,{"ok":True})
        if parts==["api","cases"] and method=="POST":
            d=self._body(); return self._send(201,store.create_case(user,d.get("case_number",""),d.get("title","")))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="members" and method=="POST":
            d=self._body(); return self._send(201,store.add_member(user,int(parts[2]),d.get("user_id",""),d.get("role","")))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="evidence" and method=="POST":
            d=self._body(); return self._send(201,store.ingest_evidence(user,int(parts[2]),d.get("label",""),d.get("filename",""),d.get("content_b64",""),d.get("retention_until",""),d.get("custodian")))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="report" and method=="GET":
            return self._send(200,store.report(user,int(parts[2])))
        if len(parts)>=3 and parts[:2]==["api","evidence"]:
            evidence_id=int(parts[2])
            if len(parts)==3 and method=="GET": return self._send(200,store.get_evidence(user,evidence_id,bool(urlparse(self.path).query)))
            if len(parts)==4 and method=="POST":
                d=self._body()
                if parts[3]=="transfer": return self._send(201,store.transfer(user,evidence_id,d.get("to_person",""),d.get("location",""),d.get("note","")))
                if parts[3]=="open": return self._send(200,store.open_evidence(user,evidence_id,d.get("location",""),d.get("note","")))
                if parts[3]=="derive": return self._send(201,store.derive(user,evidence_id,d.get("method",""),d.get("label",""),d.get("filename",""),d.get("content_b64","")))
                if parts[3]=="release": return self._send(200,store.release(user,evidence_id,d.get("recipient",""),d.get("note","")))
                if parts[3]=="hold": return self._send(200,store.set_hold(user,evidence_id,bool(d.get("hold")),d.get("reason","")))
            if len(parts)==5 and parts[3]=="transfer" and method=="POST":
                self._body()
                if parts[4]=="confirm": return self._send(200,store.confirm_transfer(user,evidence_id))
                if parts[4]=="withdraw": return self._send(200,store.withdraw_transfer(user,evidence_id))
                if parts[4]=="resubmit": return self._send(200,store.resubmit_transfer(user,evidence_id))
        raise BusinessError("接口不存在",404,"not_found")
    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc: self._send(exc.status,{"error":{"code":exc.code,"message":exc.message}})
        except (ValueError,TypeError): self._send(400,{"error":{"code":"invalid_path","message":"路径参数格式错误"}})
        except Exception as exc: self._send(500,{"error":{"code":"internal_error","message":str(exc)}})
    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_DELETE(self): self._send(405,{"error":{"code":"immutable_audit","message":"证据和保管记录不提供删除接口"}})
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class CustodyServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,store): self.store=store; super().__init__(address,Handler)


def main():
    parser=argparse.ArgumentParser(description="法律证据保管与流转后台")
    parser.add_argument("--db",default=str(DEFAULT_DB)); parser.add_argument("--port",type=int,default=8105)
    parser.add_argument("--init",action="store_true"); parser.add_argument("--seed",action="store_true")
    args=parser.parse_args(); store=CustodyStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server=CustodyServer(("127.0.0.1",args.port),store); print(f"证据保管系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=="__main__": main()
