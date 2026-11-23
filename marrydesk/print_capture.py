"""打印机离线采录：作业日志、受控盘扫码、空白件领用、作废见证与登记提交。

打印机是无法参加 SQLite 事务的外部设备：驱动报告"作业接受"与纸张真实落入
受控出纸盘不是同一步，重启还会丢失确认。因此本模块把两侧事实分开采录——

- ``print_jobs`` / ``capture_attempts``：发往打印机的命令（含被抑制的重试）；
- ``tray_scans``：受控出纸盘的独立扫码证据（可重传，按 scan_id 去重）；
- ``stock_moves``：每张预编号纸张的领用、绑定、领回、作废去向；
- ``print_queue``：本地任务队列进度（确认丢失后按双账对账收尾）；
- ``submissions``：电子登记提交与确认记录。

每条记录保留作业号、实体 serial、案卷、设备启动代次、事件时刻、收到时刻和
原始字节。作业身份 job_id 一次性绑定一张预编号纸：重发同一作业不会消耗第
二张纸；是否恢复打印只依据双账对账（reconcile），绝不靠对作业的盲目重试。
"""
import json

SCHEMA = """
CREATE TABLE IF NOT EXISTS print_jobs(
  job_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  serial TEXT NOT NULL,
  boot_epoch INTEGER NOT NULL,
  effective_at INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  ref TEXT NOT NULL,
  job_id TEXT,
  case_id TEXT,
  serial TEXT,
  boot_epoch INTEGER,
  outcome TEXT NOT NULL,
  reason TEXT,
  effective_at INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tray_scans(
  scan_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  serial TEXT NOT NULL,
  boot_epoch INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  effective_at INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_moves(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial TEXT NOT NULL,
  kind TEXT NOT NULL,
  job_id TEXT,
  case_id TEXT,
  detail TEXT,
  effective_at INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS print_queue(
  job_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  serial TEXT NOT NULL,
  state TEXT NOT NULL,
  enqueued_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions(
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  serial TEXT NOT NULL,
  job_id TEXT,
  kind TEXT NOT NULL,
  effective_at INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  raw TEXT NOT NULL
);
"""

TERMINAL_SHEET_STATES = ('reclaimed', 'voided')


def ensure(con):
    con.executescript(SCHEMA)


def _move(con, serial, kind, job_id=None, case_id=None, detail=None,
          effective_at=0, received_at=0, raw=''):
    con.execute('INSERT INTO stock_moves(serial,kind,job_id,case_id,detail,effective_at,received_at,raw)'
                ' VALUES(?,?,?,?,?,?,?,?)',
                (serial, kind, job_id, case_id,
                 json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                 int(effective_at), int(received_at), raw or ''))


def _attempt(con, kind, ref, outcome, reason=None, job_id=None, case_id=None, serial=None,
             boot_epoch=None, effective_at=0, received_at=0, raw=''):
    con.execute('INSERT INTO capture_attempts(kind,ref,job_id,case_id,serial,boot_epoch,outcome,reason,'
                'effective_at,received_at,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (kind, ref, job_id, case_id, serial, boot_epoch, outcome, reason,
                 int(effective_at), int(received_at), raw or ''))


def _sheet_kinds(con, serial):
    return {r['kind'] for r in con.execute('SELECT kind FROM stock_moves WHERE serial=?', (serial,))}


def _assignment(con, job_id):
    return con.execute("SELECT * FROM stock_moves WHERE job_id=? AND kind='assign'"
                       ' ORDER BY id DESC LIMIT 1', (job_id,)).fetchone()


# ---- 空白件领用 ----

def intake(con, serials, effective_at=0, received_at=0, raw=''):
    """预编号空白件进入受控库存；同一 serial 重复领用只记一次。"""
    ensure(con)
    taken, skipped = [], []
    with con:
        for serial in serials:
            if 'intake' in _sheet_kinds(con, serial):
                skipped.append(serial)
                continue
            _move(con, serial, 'intake', effective_at=effective_at,
                  received_at=received_at, raw=raw)
            taken.append(serial)
    return {'outcome': 'intaken', 'intaken': taken, 'skipped': skipped}


def assign(con, job_id, case_id, serial, boot_epoch=0, effective_at=0, received_at=0, raw=''):
    """作业身份一次性绑定一张预编号纸——"重试不消耗第二张纸"的根部约束。"""
    ensure(con)
    prior_job = _assignment(con, job_id)
    if prior_job:
        if prior_job['serial'] == serial and prior_job['case_id'] == case_id:
            return {'outcome': 'idempotent', 'reason': 'already-assigned',
                    'job_id': job_id, 'serial': serial}
        return {'outcome': 'blocked', 'reason': 'job-already-assigned',
                'job_id': job_id, 'serial': prior_job['serial']}
    prior_serial = con.execute("SELECT * FROM stock_moves WHERE serial=? AND kind='assign'",
                               (serial,)).fetchone()
    if prior_serial:
        return {'outcome': 'blocked', 'reason': 'serial-already-assigned',
                'serial': serial, 'job_id': prior_serial['job_id']}
    kinds = _sheet_kinds(con, serial)
    if 'intake' not in kinds:
        return {'outcome': 'blocked', 'reason': 'unknown-sheet', 'serial': serial}
    if 'void' in kinds:
        return {'outcome': 'blocked', 'reason': 'sheet-voided', 'serial': serial}
    if 'reclaim' in kinds:
        return {'outcome': 'blocked', 'reason': 'sheet-reclaimed', 'serial': serial}
    with con:
        _move(con, serial, 'assign', job_id, case_id, {'boot_epoch': int(boot_epoch)},
              effective_at, received_at, raw)
    return {'outcome': 'assigned', 'job_id': job_id, 'case_id': case_id, 'serial': serial}


# ---- 本地任务队列 ----

def enqueue(con, job_id, case_id, serial, effective_at=0):
    ensure(con)
    old = con.execute('SELECT * FROM print_queue WHERE job_id=?', (job_id,)).fetchone()
    if old:
        if old['case_id'] == case_id and old['serial'] == serial:
            return {'outcome': 'idempotent', 'job_id': job_id, 'state': old['state']}
        return {'outcome': 'blocked', 'reason': 'task-id-conflict', 'job_id': job_id}
    if not _assignment(con, job_id):
        return {'outcome': 'blocked', 'reason': 'job-not-assigned', 'job_id': job_id}
    with con:
        con.execute('INSERT INTO print_queue VALUES(?,?,?,?,?,?)',
                    (job_id, case_id, serial, 'pending', int(effective_at), int(effective_at)))
    return {'outcome': 'enqueued', 'job_id': job_id, 'state': 'pending'}


# ---- 打印机离线作业日志 ----

def dispatch(con, job_id, boot_epoch, effective_at=0, received_at=0, raw='',
             force=False, operator=None):
    """记录一次真实发印命令。

    同一作业已有发印记录时默认抑制重发：受控盘扫码或领回记录证明纸已出，
    重发就是第二次真实出纸；无任何证据时盲目重试同样被抑制——只有操作员
    现场确认受控盘无此纸（force + operator），才允许对同一作业、同一张纸
    的确认重发，绝不换纸。
    """
    ensure(con)
    assign = _assignment(con, job_id)
    if not assign:
        return {'outcome': 'blocked', 'reason': 'job-not-assigned', 'job_id': job_id}
    serial, case_id = assign['serial'], assign['case_id']
    kinds = _sheet_kinds(con, serial)
    existing = con.execute('SELECT * FROM print_jobs WHERE job_id=?', (job_id,)).fetchone()
    if existing:
        scan = con.execute("SELECT 1 FROM tray_scans WHERE job_id=? AND outcome='trayed'",
                           (job_id,)).fetchone()
        if 'void' in kinds:
            outcome, reason = 'duplicate-dispatch-suppressed', 'sheet-voided'
        elif scan or 'reclaim' in kinds:
            outcome, reason = 'duplicate-dispatch-suppressed', 'paper-already-out'
        elif force and operator:
            outcome, reason = 'resent-after-physical-check', 'operator-attested-tray-empty'
        else:
            outcome, reason = 'duplicate-dispatch-suppressed', 'unverified-retry'
        with con:
            _attempt(con, 'dispatch', job_id, outcome, reason, job_id=job_id, case_id=case_id,
                     serial=serial, boot_epoch=boot_epoch,
                     effective_at=effective_at, received_at=received_at, raw=raw)
        return {'outcome': outcome, 'reason': reason,
                'job_id': job_id, 'serial': serial, 'case_id': case_id}
    if 'void' in kinds:
        return {'outcome': 'blocked', 'reason': 'sheet-voided', 'job_id': job_id, 'serial': serial}
    with con:
        con.execute('INSERT INTO print_jobs VALUES(?,?,?,?,?,?,?)',
                    (job_id, case_id, serial, int(boot_epoch),
                     int(effective_at), int(received_at), raw or ''))
        con.execute("UPDATE print_queue SET state='dispatched', updated_at=?"
                    " WHERE job_id=? AND state='pending'", (int(effective_at), job_id))
        _attempt(con, 'dispatch', job_id, 'sent', None, job_id=job_id, case_id=case_id,
                 serial=serial, boot_epoch=boot_epoch,
                 effective_at=effective_at, received_at=received_at, raw=raw)
    return {'outcome': 'sent', 'job_id': job_id, 'serial': serial,
            'case_id': case_id, 'boot_epoch': int(boot_epoch)}


# ---- 受控出纸盘扫码 ----

def tray_scan(con, scan_id, job_id, serial, boot_epoch, effective_at=0, received_at=0, raw=''):
    """采录受控盘扫码包。扫码包可重传：按 scan_id 去重，载荷不一致拒绝；
    扫码 serial 必须与作业绑定的纸张一致，否则作为异常证据留存。"""
    ensure(con)
    old = con.execute('SELECT * FROM tray_scans WHERE scan_id=?', (scan_id,)).fetchone()
    if old:
        same = old['job_id'] == job_id and old['serial'] == serial and old['raw'] == (raw or '')
        outcome = 'duplicate-scan' if same else 'scan-id-conflict'
        with con:
            _attempt(con, 'tray_scan', scan_id, outcome, None if same else 'payload-differs',
                     job_id=job_id, serial=serial, boot_epoch=boot_epoch,
                     effective_at=effective_at, received_at=received_at, raw=raw)
        return {'outcome': outcome, 'scan_id': scan_id, 'job_id': job_id, 'serial': serial}
    job = con.execute('SELECT * FROM print_jobs WHERE job_id=?', (job_id,)).fetchone()
    if not job:
        outcome, reason = 'unknown-job', 'no-dispatch-record'
    elif job['serial'] != serial:
        outcome, reason = 'serial-mismatch', f"job-bound-to-{job['serial']}"
    else:
        outcome, reason = 'trayed', None
    with con:
        con.execute('INSERT INTO tray_scans VALUES(?,?,?,?,?,?,?,?)',
                    (scan_id, job_id, serial, int(boot_epoch), outcome,
                     int(effective_at), int(received_at), raw or ''))
        _attempt(con, 'tray_scan', scan_id, outcome, reason, job_id=job_id, serial=serial,
                 boot_epoch=boot_epoch, effective_at=effective_at, received_at=received_at, raw=raw)
    return {'outcome': outcome, 'reason': reason,
            'scan_id': scan_id, 'job_id': job_id, 'serial': serial}


# ---- 领回与作废见证 ----

def reclaim(con, job_id, clerk, effective_at=0, received_at=0, raw=''):
    """办事人核验纸张完整后从受控盘领回：领回事实入账，供补登记签发。"""
    ensure(con)
    job = con.execute('SELECT * FROM print_jobs WHERE job_id=?', (job_id,)).fetchone()
    if not job:
        return {'outcome': 'blocked', 'reason': 'unknown-job', 'job_id': job_id}
    serial = job['serial']
    scan = con.execute("SELECT 1 FROM tray_scans WHERE job_id=? AND outcome='trayed'",
                       (job_id,)).fetchone()
    if not scan:
        return {'outcome': 'blocked', 'reason': 'no-tray-evidence', 'job_id': job_id}
    kinds = _sheet_kinds(con, serial)
    if 'void' in kinds:
        return {'outcome': 'blocked', 'reason': 'sheet-voided', 'serial': serial}
    if 'reclaim' in kinds:
        return {'outcome': 'idempotent', 'reason': 'already-reclaimed',
                'job_id': job_id, 'serial': serial}
    with con:
        _move(con, serial, 'reclaim', job_id, job['case_id'], {'clerk': clerk},
              effective_at, received_at, raw)
    return {'outcome': 'reclaimed', 'job_id': job_id, 'serial': serial, 'clerk': clerk}


def void(con, serial, witnesses, reason='', effective_at=0, received_at=0, raw=''):
    """两人见证作废实体纸：库存账留痕，绑定作业的未完成任务随之中止。"""
    ensure(con)
    witnesses = sorted({w for w in (witnesses or []) if w})
    if len(witnesses) != 2:
        return {'outcome': 'blocked', 'reason': 'witness-quorum-missing', 'serial': serial}
    kinds = _sheet_kinds(con, serial)
    if 'intake' not in kinds:
        return {'outcome': 'blocked', 'reason': 'unknown-sheet', 'serial': serial}
    if 'void' in kinds:
        return {'outcome': 'idempotent', 'reason': 'already-voided', 'serial': serial}
    assign = con.execute("SELECT * FROM stock_moves WHERE serial=? AND kind='assign'"
                         ' ORDER BY id DESC LIMIT 1', (serial,)).fetchone()
    job_id = assign['job_id'] if assign else None
    case_id = assign['case_id'] if assign else None
    with con:
        _move(con, serial, 'void', job_id, case_id,
              {'witnesses': witnesses, 'reason': reason}, effective_at, received_at, raw)
        if job_id:
            con.execute("UPDATE print_queue SET state='cancelled', updated_at=?"
                        " WHERE job_id=? AND state<>'confirmed'", (int(effective_at), job_id))
    return {'outcome': 'voided', 'serial': serial, 'job_id': job_id, 'witnesses': witnesses}


# ---- 电子登记提交记录 ----

def record_submission(con, submission_id, case_id, serial, job_id=None, kind='submit',
                      effective_at=0, received_at=0, raw=''):
    """登记提交/确认采录。不自带事务边界：与调用方同一 SQLite 事务提交，
    保证登记行与提交记录同生共死。"""
    old = con.execute('SELECT * FROM submissions WHERE id=?', (submission_id,)).fetchone()
    if old:
        if old['case_id'] == case_id and old['serial'] == serial and old['kind'] == kind:
            return {'outcome': 'idempotent', 'id': submission_id}
        return {'outcome': 'blocked', 'reason': 'submission-id-conflict', 'id': submission_id}
    con.execute('INSERT INTO submissions VALUES(?,?,?,?,?,?,?,?)',
                (submission_id, case_id, serial, job_id, kind,
                 int(effective_at), int(received_at), raw or ''))
    return {'outcome': 'recorded', 'id': submission_id, 'kind': kind}


def confirm_task(con, job_id, effective_at=0, received_at=0, raw=''):
    """关闭队列任务：仅当纸张物理去向与电子登记都已落账。断电后靠对账
    收尾（reconcile → confirm_task），而不是靠重发物理打印。"""
    ensure(con)
    from . import certificate, registry
    task = con.execute('SELECT * FROM print_queue WHERE job_id=?', (job_id,)).fetchone()
    if not task:
        return {'outcome': 'blocked', 'reason': 'no-task', 'job_id': job_id}
    if task['state'] == 'confirmed':
        return {'outcome': 'idempotent', 'reason': 'already-confirmed', 'job_id': job_id}
    if task['state'] == 'cancelled':
        return {'outcome': 'blocked', 'reason': 'task-cancelled', 'job_id': job_id}
    sheet = certificate.sheet_state(con, task['serial'])
    if sheet['state'] not in ('printed', 'reclaimed'):
        return {'outcome': 'blocked', 'reason': 'no-physical-evidence',
                'job_id': job_id, 'sheet': sheet['state']}
    reg = registry.active(con, task['case_id'])
    if not reg or reg['serial'] != task['serial']:
        return {'outcome': 'blocked', 'reason': 'not-registered', 'job_id': job_id}
    with con:
        con.execute("UPDATE print_queue SET state='confirmed', updated_at=? WHERE job_id=?",
                    (int(effective_at), job_id))
        record_submission(con, f'{job_id}:confirm', task['case_id'], task['serial'],
                          job_id, 'confirm', effective_at, received_at, raw)
    return {'outcome': 'confirmed', 'job_id': job_id, 'serial': task['serial']}


# ---- 查询与恢复对账 ----

def sheet_moves(con, serial):
    """一张预编号纸的全部去向记录。"""
    ensure(con)
    return [dict(m) for m in
            con.execute('SELECT * FROM stock_moves WHERE serial=? ORDER BY id', (serial,))]


def physical_commands(con, job_id):
    """真实送达打印机的命令次数（被抑制的重试不计）。"""
    ensure(con)
    row = con.execute("SELECT COUNT(*) n FROM capture_attempts WHERE job_id=?"
                      " AND kind='dispatch' AND outcome IN ('sent','resent-after-physical-check')",
                      (job_id,)).fetchone()
    return row['n']


def job_state(con, job_id):
    ensure(con)
    from . import certificate
    assign = _assignment(con, job_id)
    job = con.execute('SELECT * FROM print_jobs WHERE job_id=?', (job_id,)).fetchone()
    if not assign and not job:
        return {'job_id': job_id, 'state': 'unknown'}
    serial = (assign or job)['serial']
    case_id = (assign or job)['case_id']
    scan = con.execute("SELECT 1 FROM tray_scans WHERE job_id=? AND outcome='trayed'",
                       (job_id,)).fetchone()
    queue = con.execute('SELECT state FROM print_queue WHERE job_id=?', (job_id,)).fetchone()
    sheet = certificate.sheet_state(con, serial)['state']
    sent = job is not None
    if sheet in TERMINAL_SHEET_STATES:
        state = sheet
    elif scan:
        state = 'trayed'
    elif sent:
        state = 'sent'
    else:
        state = 'assigned'
    return {'job_id': job_id, 'case_id': case_id, 'serial': serial,
            'boot_epoch': job['boot_epoch'] if job else None,
            'assigned': True, 'sent': sent, 'trayed': bool(scan), 'sheet': sheet,
            'queue': queue['state'] if queue else 'none',
            'prints': physical_commands(con, job_id), 'state': state}


def _reconcile_state(job, reg):
    if job is None:
        return ('registered-no-job', ['inspect']) if reg else ('no-job', [])
    reg_match = bool(reg) and reg['serial'] == job['serial']
    sheet = job['sheet']
    if sheet == 'voided':
        if reg_match:
            return 'registered-sheet-voided', ['revoke']
        if reg:
            return 'conflict', ['inspect']
        return 'sheet-voided-awaiting-new-job', ['assign-new-job']
    if sheet == 'reclaimed':
        if reg_match:
            return ('complete', []) if job['queue'] == 'confirmed' \
                else ('registered-unconfirmed', ['confirm-queue'])
        if reg:
            return 'conflict', ['inspect']
        return 'reclaimed-unregistered', ['register-reclaimed-sheet']
    if sheet == 'printed':
        if reg_match:
            return ('complete', []) if job['queue'] == 'confirmed' \
                else ('registered-unconfirmed', ['confirm-queue'])
        if reg:
            return 'conflict', ['inspect']
        return 'printed-unregistered', ['reclaim-and-register', 'void-and-reprint']
    # assigned：纸张尚无出纸证据
    if reg_match:
        if job['sent']:
            return 'sent-no-evidence', ['await-scan', 'operator-inspect']
        return 'registered-not-printed', ['dispatch']
    if reg:
        return 'conflict', ['inspect']
    if job['sent']:
        return 'sent-no-evidence', ['await-scan', 'operator-inspect']
    return 'awaiting-dispatch', ['dispatch']


def reconcile(con, case_id):
    """双账对账：作业身份 × 受控盘状态 × 电子登记 → 中间状态与合法恢复动作。

    不假设 SQLite 提交与外部打印结果之间存在天然原子提交：所有结论只来自
    已落库的采录证据（作业日志、受控盘扫码、库存移动、登记行、队列进度）。
    """
    ensure(con)
    from . import registry
    assigns = con.execute("SELECT * FROM stock_moves WHERE case_id=? AND kind='assign'"
                          ' ORDER BY id', (case_id,)).fetchall()
    jobs = [job_state(con, a['job_id']) for a in assigns]
    reg = registry.active(con, case_id)
    current = jobs[-1] if jobs else None
    state, actions = _reconcile_state(current, reg)
    return {'case_id': case_id, 'jobs': jobs,
            'current_job': current['job_id'] if current else None,
            'registry': reg, 'state': state, 'actions': actions}


def pending_recovery(con):
    """崩溃重启后的恢复工作清单：所有未闭环的案卷及其对账状态。"""
    ensure(con)
    cases = {r['case_id'] for r in
             con.execute('SELECT DISTINCT case_id FROM stock_moves WHERE case_id IS NOT NULL')}
    cases |= {r['case_id'] for r in con.execute('SELECT DISTINCT case_id FROM registry_entries')}
    out = []
    for case_id in sorted(cases):
        plan = reconcile(con, case_id)
        if plan['state'] not in ('complete', 'no-job'):
            out.append(plan)
    return out
