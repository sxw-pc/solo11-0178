"""崩溃恢复双账测试：实体纸张账（print_capture）× 电子登记账（registry）。

fixtures/cert-crash.json 是场景真源：crash 步真实关闭并重开 SQLite 连接
（模拟进程重启），其后所有结论只能来自落库证据。
"""
import json
import tempfile
import unittest
from pathlib import Path

from marrydesk import certificate, print_capture, registry, storage

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'cert-crash.json'
NORMAL = Path(__file__).resolve().parents[1] / 'fixtures' / 'certificate_normal.json'


def load_phases():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))['phases']


class Runner:
    """按 fixture 相位驱动双账；crash 步关闭并重开连接（进程重启，启动代次递增）。"""

    def __init__(self, tmp):
        self.db = Path(tmp) / 'cert.db'
        self.con = storage.connect(self.db)
        self.boot = 1

    def close(self):
        self.con.close()

    def crash(self, boot):
        self.con.close()
        self.con = storage.connect(self.db)
        self.boot = boot

    def run(self, phases, names):
        for name in names:
            for i, step in enumerate(phases[name]):
                self.step(step, f'{name}[{i}]')

    def step(self, s, where):
        op = s['op']
        at = s.get('at', 0)
        rx = s.get('rx', at)
        raw = s.get('raw', '')
        if op == 'crash':
            self.crash(s['boot'])
            return None
        if op == 'intake':
            r = print_capture.intake(self.con, s['serials'], at, rx, raw)
        elif op == 'assign':
            r = print_capture.assign(self.con, s['job'], s['case'], s['serial'],
                                     s.get('boot', self.boot), at, rx, raw)
        elif op == 'enqueue':
            r = print_capture.enqueue(self.con, s['job'], s['case'], s['serial'], at)
        elif op == 'dispatch':
            r = print_capture.dispatch(self.con, s['job'], s.get('boot', self.boot), at, rx, raw)
        elif op == 'scan':
            r = print_capture.tray_scan(self.con, s['scan'], s['job'], s['serial'],
                                        s.get('boot', self.boot), at, rx, raw)
        elif op == 'reclaim':
            r = print_capture.reclaim(self.con, s['job'], s['clerk'], at, rx, raw)
        elif op == 'void':
            r = print_capture.void(self.con, s['serial'], s['witnesses'],
                                   s.get('reason', ''), at, rx, raw)
        elif op == 'issue':
            r = registry.issue(self.con, s['case'], s['serial'], job_id=s.get('job'),
                               window=s.get('window'), effective_at=at, received_at=rx, raw=raw)
        elif op == 'revoke':
            r = registry.revoke(self.con, s['case'], s.get('reason', ''), at, rx)
        elif op == 'confirm':
            r = print_capture.confirm_task(self.con, s['job'], at, rx, raw)
        elif op == 'reconcile':
            plan = print_capture.reconcile(self.con, s['case'])
            assert plan['state'] == s['expect_state'], \
                f"{where}: reconcile {s['case']} -> {plan['state']!r}, want {s['expect_state']!r}"
            if 'expect_actions' in s:
                assert plan['actions'] == s['expect_actions'], \
                    f"{where}: actions {plan['actions']} != {s['expect_actions']}"
            return plan
        elif op == 'assert_sheet':
            st = certificate.sheet_state(self.con, s['serial'])
            assert st['state'] == s['expect'], \
                f"{where}: sheet {s['serial']} -> {st['state']!r}, want {s['expect']!r}"
            return st
        elif op == 'assert_registry':
            act = registry.active(self.con, s['case'])
            got = act['serial'] if act else None
            assert got == s['expect'], \
                f"{where}: registry {s['case']} -> {got!r}, want {s['expect']!r}"
            return act
        elif op == 'assert_prints':
            n = print_capture.physical_commands(self.con, s['job'])
            assert n == s['expect'], f"{where}: prints {s['job']} -> {n}, want {s['expect']}"
            return n
        else:
            raise AssertionError(f'{where}: unknown op {op}')
        if 'expect' in s:
            assert r['outcome'] == s['expect'], \
                f"{where}: {op} -> {r['outcome']!r}, want {s['expect']!r} ({r})"
        if 'expect_reason' in s:
            assert r.get('reason') == s['expect_reason'], \
                f"{where}: {op} reason -> {r.get('reason')!r}, want {s['expect_reason']!r}"
        return r


class CrashRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.phases = load_phases()
        self.runner = Runner(self.tmp.name)

    def tearDown(self):
        self.runner.close()
        self.tmp.cleanup()

    def run_phases(self, *names):
        self.runner.run(self.phases, list(names))

    def test_lost_ack_leaves_only_sent_record(self):
        # 断电切点 A：SQLite 只保留 "J7 已发送"，没有 issued 行，也没有扫码
        self.run_phases('stock', 'crash_a_setup')
        con = self.runner.con
        self.assertEqual(print_capture.job_state(con, 'J7')['state'], 'sent')
        self.assertIsNone(registry.active(con, 'X'))
        self.assertEqual(con.execute('SELECT COUNT(*) n FROM tray_scans').fetchone()['n'], 0)
        self.assertEqual(con.execute('SELECT COUNT(*) n FROM registry_entries').fetchone()['n'], 0)

    def test_crash_a_recover_reclaim(self):
        # 领回路：领回 C100 → 补登记签发 X=C100，最终只有一张有效证书
        self.run_phases('stock', 'crash_a_setup', 'crash_a_restart', 'recover_reclaim')
        con = self.runner.con
        self.assertEqual(registry.active(con, 'X')['serial'], 'C100')
        self.assertEqual(len(registry.history(con, 'X')), 1)
        # 打印重试未产生第二次真实发印，也未消耗第二张纸
        self.assertEqual(print_capture.physical_commands(con, 'J7'), 1)
        self.assertEqual(certificate.sheet_state(con, 'C101')['state'], 'stock')
        self.assertEqual([m['kind'] for m in print_capture.sheet_moves(con, 'C100')],
                         ['intake', 'assign', 'reclaim'])
        # 登记提交与确认记录已采录
        subs = con.execute('SELECT id,kind FROM submissions ORDER BY id').fetchall()
        self.assertEqual([(s['id'], s['kind']) for s in subs],
                         [('J7:confirm', 'confirm'), ('X:C100:submit', 'submit')])
        self.assertEqual(print_capture.pending_recovery(con), [])

    def test_crash_a_recover_void(self):
        # 作废路：两人见证作废 C100 → 新作业 J8 打印 C101 → 签发 X=C101
        self.run_phases('stock', 'crash_a_setup', 'crash_a_restart', 'recover_void')
        con = self.runner.con
        self.assertEqual(registry.active(con, 'X')['serial'], 'C101')
        self.assertEqual(certificate.sheet_state(con, 'C100')['state'], 'voided')
        # 旧 J7 只消耗一次物理 C100；J8 各自只发印一次
        self.assertEqual(print_capture.physical_commands(con, 'J7'), 1)
        self.assertEqual(print_capture.physical_commands(con, 'J8'), 1)
        moves = print_capture.sheet_moves(con, 'C100')
        void_move = [m for m in moves if m['kind'] == 'void'][0]
        self.assertEqual(json.loads(void_move['detail'])['witnesses'],
                         ['witness-1', 'witness-2'])
        # J7 的队列任务已随作废取消，恢复清单为空
        self.assertEqual(con.execute("SELECT state FROM print_queue WHERE job_id='J7'")
                         .fetchone()['state'], 'cancelled')
        self.assertEqual(print_capture.pending_recovery(con), [])

    def test_crash_b_registered_unconfirmed_no_second_print(self):
        # 断电切点 B：登记已写 issued、队列未确认，恢复不得再发物理打印
        self.run_phases('stock', 'crash_b_setup', 'crash_b_restart')
        con = self.runner.con
        self.assertEqual(print_capture.physical_commands(con, 'J9'), 1)
        scans = con.execute("SELECT COUNT(*) n FROM tray_scans"
                            " WHERE job_id='J9' AND outcome='trayed'").fetchone()['n']
        self.assertEqual(scans, 1)
        self.assertEqual(registry.active(con, 'Y')['serial'], 'C200')
        self.assertEqual(print_capture.pending_recovery(con), [])

    def test_register_first_order_recoverable(self):
        # 先占号后打印：登记先于出纸，断电后按同一作业同一纸张恢复
        self.run_phases('stock', 'register_first')
        con = self.runner.con
        self.assertEqual(registry.active(con, 'Z')['serial'], 'C300')
        self.assertEqual(print_capture.physical_commands(con, 'J10'), 1)
        self.assertEqual(certificate.sheet_state(con, 'C300')['state'], 'printed')

    def test_scan_retransmission_dedup_and_raw_bytes_kept(self):
        self.run_phases('stock', 'crash_a_setup', 'crash_a_restart')
        con = self.runner.con
        rows = con.execute("SELECT * FROM tray_scans WHERE scan_id='S1'").fetchall()
        self.assertEqual(len(rows), 1)  # 重传去重：只有一条扫码证据
        self.assertEqual(rows[0]['raw'], 'TRAY|SCAN|S1|C100|J7')
        self.assertEqual(rows[0]['effective_at'], 13)   # 事件时刻保留原扫码时间
        self.assertEqual(rows[0]['received_at'], 16)    # 收到时刻是重传到达时间
        attempts = con.execute("SELECT outcome,received_at FROM capture_attempts"
                               " WHERE kind='tray_scan' AND ref='S1' ORDER BY id").fetchall()
        self.assertEqual([a['outcome'] for a in attempts], ['trayed', 'duplicate-scan'])
        self.assertEqual(attempts[1]['received_at'], 17)
        # 作业日志保留启动代次与原始字节；被抑制的重发同样留痕
        job = con.execute("SELECT * FROM print_jobs WHERE job_id='J7'").fetchone()
        self.assertEqual((job['boot_epoch'], job['raw']), (1, 'PRN|PRINT|J7|C100|X|boot=1'))
        retry = con.execute("SELECT outcome,boot_epoch,raw FROM capture_attempts"
                            " WHERE kind='dispatch' AND job_id='J7' ORDER BY id").fetchall()
        self.assertEqual([r['outcome'] for r in retry],
                         ['sent', 'duplicate-dispatch-suppressed'])
        self.assertEqual((retry[1]['boot_epoch'], retry[1]['raw']),
                         (2, 'PRN|PRINT|J7|C100|X|boot=2'))

    def test_registry_retry_never_issues_second_serial(self):
        self.run_phases('stock', 'crash_a_setup', 'crash_a_restart', 'recover_reclaim')
        con = self.runner.con
        again = registry.issue(con, 'X', 'C100', job_id='J7', effective_at=30, received_at=30)
        self.assertEqual(again['outcome'], 'idempotent')
        other = registry.issue(con, 'X', 'C101', effective_at=31, received_at=31)
        self.assertEqual((other['outcome'], other['reason']),
                         ('blocked', 'case-has-active-serial'))
        self.assertEqual(len(registry.history(con, 'X')), 1)

    def test_cross_window_serial_uniqueness(self):
        self.run_phases('stock', 'crash_a_setup', 'crash_a_restart', 'recover_reclaim')
        con = self.runner.con
        # 另一窗口 W2 为另一案卷尝试同一实体 serial：全局唯一，拒绝
        print_capture.assign(con, 'J20', 'Y2', 'C101', boot_epoch=2,
                             effective_at=40, received_at=40)
        r = registry.issue(con, 'Y2', 'C100', job_id='J20', window='W2',
                           effective_at=41, received_at=41)
        self.assertEqual((r['outcome'], r['reason']), ('blocked', 'serial-registered-elsewhere'))
        # 案卷已有有效编号时也不能再占第二张纸
        r2 = registry.issue(con, 'X', 'C101', effective_at=42, received_at=42)
        self.assertEqual((r2['outcome'], r2['reason']), ('blocked', 'case-has-active-serial'))
        # 绑定他案作业的纸不可横夺
        r3 = registry.issue(con, 'Y3', 'C101', effective_at=43, received_at=43)
        self.assertEqual((r3['outcome'], r3['reason']), ('blocked', 'sheet-bound-to-other-case'))

    def test_revocation_history(self):
        # 签发后纸面被污染：双人见证作废 → 登记撤销留痕 → 新作业新纸签发
        self.run_phases('stock', 'crash_a_setup', 'crash_a_restart', 'recover_reclaim')
        con = self.runner.con
        v = print_capture.void(con, 'C100', ['w1', 'w2'], 'contaminated-after-issue',
                               effective_at=50, received_at=50)
        self.assertEqual(v['outcome'], 'voided')
        plan = print_capture.reconcile(con, 'X')
        self.assertEqual(plan['state'], 'registered-sheet-voided')
        self.assertEqual(plan['actions'], ['revoke'])
        # 已撤销的 serial 永不可再签发
        self.assertEqual(registry.revoke(con, 'X', 'sheet-voided',
                                         effective_at=51, received_at=51)['outcome'], 'revoked')
        self.assertIsNone(registry.active(con, 'X'))
        self.assertEqual(registry.issue(con, 'X', 'C100',
                                        effective_at=51, received_at=51)['reason'],
                         'serial-revoked')
        # 新作业 J8 用 C101 重新签发，历史保留撤销记录
        print_capture.assign(con, 'J8', 'X', 'C101', boot_epoch=2,
                             effective_at=52, received_at=52)
        print_capture.enqueue(con, 'J8', 'X', 'C101', effective_at=53)
        self.assertEqual(print_capture.dispatch(con, 'J8', 2, effective_at=54, received_at=54,
                                                raw='PRN|PRINT|J8|C101|X|boot=2')['outcome'],
                         'sent')
        self.assertEqual(print_capture.tray_scan(con, 'S9', 'J8', 'C101', 2,
                                                 effective_at=55, received_at=55,
                                                 raw='TRAY|SCAN|S9|C101|J8')['outcome'], 'trayed')
        self.assertEqual(registry.issue(con, 'X', 'C101', job_id='J8',
                                        effective_at=56, received_at=56)['outcome'], 'issued')
        self.assertEqual(registry.active(con, 'X')['serial'], 'C101')
        self.assertEqual([(h['serial'], h['state']) for h in registry.history(con, 'X')],
                         [('C100', 'revoked'), ('C101', 'issued')])

    def test_sent_no_evidence_needs_operator_not_blind_retry(self):
        # 断电后无扫码证据：盲目重发被抑制；操作员现场确认空盘后才允许同源
        # 重发——两次命令仍只绑定同一张 C100，不消耗第二张纸
        self.run_phases('stock', 'crash_a_setup')
        self.runner.crash(2)
        con = self.runner.con
        r = print_capture.dispatch(con, 'J7', 2, effective_at=20, received_at=20,
                                   raw='PRN|PRINT|J7|C100|X|boot=2')
        self.assertEqual((r['outcome'], r['reason']),
                         ('duplicate-dispatch-suppressed', 'unverified-retry'))
        plan = print_capture.reconcile(con, 'X')
        self.assertEqual(plan['state'], 'sent-no-evidence')
        self.assertEqual(plan['actions'], ['await-scan', 'operator-inspect'])
        r2 = print_capture.dispatch(con, 'J7', 2, effective_at=21, received_at=21,
                                    raw='PRN|PRINT|J7|C100|X|boot=2|resend',
                                    force=True, operator='clerk-A')
        self.assertEqual(r2['outcome'], 'resent-after-physical-check')
        self.assertEqual(print_capture.physical_commands(con, 'J7'), 2)
        self.assertEqual(certificate.sheet_state(con, 'C100')['state'], 'assigned')
        self.assertEqual(certificate.sheet_state(con, 'C101')['state'], 'stock')

    def test_witness_quorum_required(self):
        self.run_phases('stock', 'crash_a_setup', 'crash_a_restart')
        con = self.runner.con
        self.assertEqual(print_capture.void(con, 'C100', ['w1'], 'x',
                                            effective_at=30, received_at=30)['reason'],
                         'witness-quorum-missing')
        self.assertEqual(print_capture.void(con, 'C100', ['w1', 'w1'], 'x',
                                            effective_at=30, received_at=30)['reason'],
                         'witness-quorum-missing')
        self.assertEqual(certificate.sheet_state(con, 'C100')['state'], 'printed')

    def test_pending_recovery_lists_intermediate_jobs(self):
        self.run_phases('stock', 'crash_a_setup', 'crash_a_restart')
        con = self.runner.con
        pending = print_capture.pending_recovery(con)
        self.assertEqual([(p['case_id'], p['state']) for p in pending],
                         [('X', 'printed-unregistered')])

    def test_seed_single_sample_still_runs(self):
        # 原单张样例：usable/entry 口径不变
        record = json.loads(NORMAL.read_text(encoding='utf-8'))
        self.assertTrue(certificate.usable(record))
        self.assertEqual(registry.entry(record),
                         {'case_id': 'COUPLE-1', 'serial': 'CERT-100', 'state': 'issued'})


if __name__ == '__main__':
    unittest.main()
