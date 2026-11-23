"""每张预编号纸张的物理状态与可签发性。

usable() 保留原有布尔口径（只看 printed/voided 两字段）；sheet_state() 与
issuable() 改从采录账本（print_capture）推导，断电重启后仍可恢复，不依赖
任何内存态。
"""
from . import print_capture

STATES = ('unknown', 'stock', 'assigned', 'printed', 'reclaimed', 'voided')


def usable(record):
    return bool(record.get('printed')) and not bool(record.get('voided'))


def sheet_state(con, serial):
    """单张纸的物理状态：unknown / stock / assigned / printed / reclaimed / voided。

    只依据已落库的采录证据：库存移动（领用、绑定、领回、作废）与受控盘
    扫码；驱动应答不作为出纸证据。
    """
    print_capture.ensure(con)
    moves = con.execute('SELECT * FROM stock_moves WHERE serial=? ORDER BY id',
                        (serial,)).fetchall()
    kinds = {m['kind'] for m in moves}
    job_id = case_id = None
    for m in moves:
        if m['kind'] == 'assign':
            job_id, case_id = m['job_id'], m['case_id']
    scan = None
    if job_id:
        scan = con.execute("SELECT 1 FROM tray_scans WHERE job_id=? AND outcome='trayed'",
                           (job_id,)).fetchone()
    if 'void' in kinds:
        state = 'voided'
    elif 'reclaim' in kinds:
        state = 'reclaimed'
    elif scan:
        state = 'printed'
    elif job_id:
        state = 'assigned'
    elif 'intake' in kinds:
        state = 'stock'
    else:
        state = 'unknown'
    return {'serial': serial, 'state': state, 'job_id': job_id, 'case_id': case_id,
            'moves': [m['kind'] for m in moves]}


def issuable(con, serial, case_id=None):
    """该纸现在能否对 case_id 电子签发 → (bool, reason)。

    两种调用顺序都合法：先打印（printed/reclaimed 已有出纸证据）或先占号
    （assigned 已绑定本案作业、纸张待发）。作废、未知、未领用、绑定他案
    一律不可签发。
    """
    sheet = sheet_state(con, serial)
    state = sheet['state']
    if state == 'unknown':
        return False, 'unknown-sheet'
    if state == 'voided':
        return False, 'sheet-voided'
    if state == 'stock':
        return False, 'sheet-not-assigned'
    if case_id is not None and sheet['case_id'] is not None and sheet['case_id'] != case_id:
        return False, 'sheet-bound-to-other-case'
    return True, 'ok'
