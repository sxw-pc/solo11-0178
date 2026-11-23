"""电子签发登记：案卷唯一有效编号与历史撤销。

登记重试幂等：同一案卷重复签发返回原编号，绝不产生第二个 serial；serial
全局唯一（跨窗口）：一张预编号纸永远只属于一个案卷，撤销后也不再可用。
是否可签发由 certificate 的纸张物理状态裁定——SQLite 承诺与外部打印结果
之间没有天然原子提交，登记只消费已落库的采录证据。
"""
import sqlite3

from . import certificate
from . import print_capture

SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_entries(
  case_id TEXT NOT NULL,
  serial TEXT NOT NULL,
  state TEXT NOT NULL,
  job_id TEXT,
  window TEXT,
  reason TEXT,
  effective_at INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  PRIMARY KEY(case_id, serial)
);
CREATE UNIQUE INDEX IF NOT EXISTS registry_serial_unique ON registry_entries(serial);
CREATE UNIQUE INDEX IF NOT EXISTS registry_one_active_per_case
  ON registry_entries(case_id) WHERE state='issued';
"""


def ensure(con):
    print_capture.ensure(con)
    con.executescript(SCHEMA)


def entry(record):
    return {'case_id': record['case_id'], 'serial': record['serial'],
            'state': 'issued' if certificate.usable(record) else 'pending'}


def active(con, case_id):
    """案卷当前唯一有效编号；无则 None。"""
    ensure(con)
    row = con.execute("SELECT * FROM registry_entries WHERE case_id=? AND state='issued'",
                      (case_id,)).fetchone()
    return dict(row) if row else None


def history(con, case_id):
    """案卷全部登记历史（含已撤销）。"""
    ensure(con)
    return [dict(r) for r in con.execute(
        'SELECT * FROM registry_entries WHERE case_id=? ORDER BY effective_at, serial',
        (case_id,))]


def sheet_registration(con, serial):
    """该实体 serial 的全部登记记录（跨窗口全局查重）。"""
    ensure(con)
    return [dict(r) for r in con.execute(
        'SELECT * FROM registry_entries WHERE serial=?', (serial,))]


def issue(con, case_id, serial, job_id=None, window=None,
          effective_at=0, received_at=0, raw='', submission_id=None):
    """签发（幂等）：同一案卷重试返回原编号，绝不产生第二个 serial。"""
    ensure(con)
    cur = active(con, case_id)
    if cur:
        if cur['serial'] == serial:
            return {'outcome': 'idempotent', 'reason': 'already-issued',
                    'case_id': case_id, 'serial': serial, 'state': 'issued'}
        return {'outcome': 'blocked', 'reason': 'case-has-active-serial',
                'case_id': case_id, 'active_serial': cur['serial'],
                'attempted_serial': serial}
    prior = sheet_registration(con, serial)
    if prior:
        if any(p['case_id'] != case_id for p in prior):
            return {'outcome': 'blocked', 'reason': 'serial-registered-elsewhere',
                    'serial': serial}
        return {'outcome': 'blocked', 'reason': 'serial-revoked', 'serial': serial}
    ok, why = certificate.issuable(con, serial, case_id)
    if not ok:
        return {'outcome': 'blocked', 'reason': why, 'case_id': case_id, 'serial': serial}
    try:
        with con:
            con.execute('INSERT INTO registry_entries VALUES(?,?,?,?,?,?,?,?)',
                        (case_id, serial, 'issued', job_id, window, None,
                         int(effective_at), int(received_at)))
            print_capture.record_submission(
                con, submission_id or f'{case_id}:{serial}:submit',
                case_id, serial, job_id, 'submit', effective_at, received_at, raw)
    except sqlite3.IntegrityError:
        # 并发兜底：部分唯一索引保证一案卷至多一条有效签发
        return {'outcome': 'blocked', 'reason': 'case-has-active-serial',
                'case_id': case_id, 'serial': serial}
    return {'outcome': 'issued', 'case_id': case_id, 'serial': serial,
            'state': 'issued', 'job_id': job_id}


def revoke(con, case_id, reason='', effective_at=0, received_at=0):
    """撤销当前有效编号：历史留痕，案卷回到无有效证书状态。"""
    ensure(con)
    cur = active(con, case_id)
    if not cur:
        return {'outcome': 'blocked', 'reason': 'no-active-entry', 'case_id': case_id}
    with con:
        con.execute("UPDATE registry_entries SET state='revoked', reason=?"
                    ' WHERE case_id=? AND serial=?', (reason, case_id, cur['serial']))
    return {'outcome': 'revoked', 'case_id': case_id,
            'serial': cur['serial'], 'reason': reason}
