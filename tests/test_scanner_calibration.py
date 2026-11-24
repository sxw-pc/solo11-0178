import copy, json, math
from pathlib import Path
from marrydesk import scanner, equipment, scanner_capture, storage

ROOT = Path(__file__).resolve().parents[1]


def load_case():
    raw = json.loads((ROOT/'fixtures'/'scanner-cross-cal.json').read_text(encoding='utf-8'))
    return scanner_capture.ingest(raw)


def batches_of(case):
    return scanner_capture.records(case, 'batch')


def close(a, b):
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def test_single_machine_baseline():
    record = json.loads((ROOT/'fixtures'/'scanner_normal.json').read_text(encoding='utf-8'))
    assert scanner.drift_mm(record) == 1
    assert equipment.state(record) == {'device_id': 'SCAN-1', 'state': '可用'}


def test_capture_ocr_units_direction_and_dedup():
    case = load_case()
    slips = scanner_capture.records(case, 'slip')
    assert [s['slip_id'] for s in slips].count('SLIP-AR-1') == 1
    assert len(case['duplicates']) == 1 and case['duplicates'][0]['duplicate_of'] == 'SLIP-AR-1'
    ar = next(s for s in slips if s['slip_id'] == 'SLIP-AR-1')
    assert (ar['head'], ar['tail'], ar['value_mm']) == ('SCAN-A', 'GAUGE-R', 2.0)
    assert ar['photo'] == {'page': 3, 'bbox': [120, 40, 460, 88]}
    assert ar['direction'] == 'SCAN-A->GAUGE-R'
    ab = next(s for s in slips if s['slip_id'] == 'SLIP-AB-1')
    assert ab['value_mm'] == 1.0 and ab['ocr_confidence'] < 0.5
    br2 = next(s for s in scanner_capture.records(case, 'slip', case['route_gauge_verify'])
               if s['slip_id'] == 'SLIP-BR-2')
    assert br2['unit_raw'] == 'cm' and close(br2['value_mm'], -2.0)


def test_contradictory_loop_reports_minimal_edges_and_worlds():
    report = scanner.assess(load_case())
    assert len(report['conflicts']) == 1
    conflict = report['conflicts'][0]
    assert set(conflict['edges']) == {'SLIP-AR-1', 'SLIP-BR-1', 'SLIP-AB-1'}
    assert close(conflict['residual_mm'], 3.0)
    assert conflict['residual_mm'] > conflict['tolerance_mm']
    assert {w['suspect'] for w in conflict['worlds']} == {'SLIP-AR-1', 'SLIP-BR-1', 'SLIP-AB-1'}
    assert all(w['consistent'] for w in conflict['worlds'])
    for w in conflict['worlds']:
        assert w['offsets']['SCAN-A']['anchored'] is False
    assert report['excluded'] == []


def test_missing_absolute_anchor_means_pending_not_nominal_truth():
    report = scanner.assess(load_case())
    certs = report['certificates']
    assert certs['SCAN-A']['status'] == '待核' and certs['SCAN-B']['status'] == '待核'
    assert certs['SCAN-A']['valid'] is False and certs['SCAN-B']['valid'] is False
    assert '锚' in certs['SCAN-A']['reason']
    assert report['offsets']['SCAN-A']['hi'] is None
    assert certs['SCAN-C']['status'] == '必定可用' and certs['SCAN-C']['valid'] is True
    assert any('标称' in n for n in report['notes'])
    naive = {d: equipment.state({'device_id': d, 'reference_mm': 100, 'measured_mm': m, 'limit_mm': 2})['state']
             for d, m in (('SCAN-A', 102), ('SCAN-B', 98))}
    assert naive == {'SCAN-A': '可用', 'SCAN-B': '可用'}
    assert certs['SCAN-A']['status'] != naive['SCAN-A']
    assert certs['SCAN-B']['status'] != naive['SCAN-B']


def test_gross_error_is_not_averaged_into_certificates():
    report = scanner.assess(load_case())
    fit = report['diagnostic_fit']
    assert close(fit['SCAN-A'], 1.0) and close(fit['SCAN-B'], -1.0)
    assert abs(fit['SCAN-A']) <= 2 and abs(fit['SCAN-B']) <= 2
    certs = report['certificates']
    assert certs['SCAN-A']['status'] == '待核' and certs['SCAN-B']['status'] == '待核'
    assert not [c for c in certs.values() if c['valid'] and c['device_id'] in ('SCAN-A', 'SCAN-B')]
    vals = {e['slip_id']: e['value_mm'] for e in report['edges']}
    assert vals == {'SLIP-AR-1': 2.0, 'SLIP-BR-1': -2.0, 'SLIP-AB-1': 1.0, 'SLIP-CS-1': 0.5}


def test_late_gauge_certificate_alone_cannot_close_conflict():
    case = load_case()
    cert_only = [r for r in case['route_gauge_verify'] if r['kind'] == 'certificate']
    report = scanner.assess(case, cert_only)
    assert len(report['conflicts']) == 1
    assert close(report['anchors']['GAUGE-R']['offset_mm'], 1.0)
    worlds = {w['suspect']: w for w in report['conflicts'][0]['worlds']}
    assert close(worlds['SLIP-AR-1']['offsets']['SCAN-A']['value'], 0.0)
    assert close(worlds['SLIP-BR-1']['offsets']['SCAN-A']['value'], 3.0)
    assert close(worlds['SLIP-AB-1']['offsets']['SCAN-B']['value'], -1.0)
    assert report['certificates']['SCAN-A']['status'] == '待核'
    assert report['certificates']['SCAN-B']['status'] == '待核'
    valid = {c['device_id'] for c in report['certificates'].values() if c['valid']}
    assert valid == {'SCAN-C'}


def test_route_gauge_verify_stops_only_a():
    case = load_case()
    report = scanner.assess(case, case['route_gauge_verify'])
    assert report['conflicts'] == []
    assert all(abs(l['residual_mm']) <= l['tolerance_mm'] for l in report['loops'])
    certs = report['certificates']
    assert certs['SCAN-A']['status'] == '必定超限' and certs['SCAN-A']['valid'] is True
    assert close(certs['SCAN-A']['estimate_mm'], 3.0)
    assert certs['SCAN-B']['status'] == '必定可用'
    assert close(certs['SCAN-B']['estimate_mm'], -1.0)
    stops = equipment.stop_list(report)
    assert stops['停机'] == ['SCAN-A'] and stops['待核'] == []
    review = equipment.batch_review(report, batches_of(case))
    assert review['BATCH-A-1']['verdict'] == '复采' and review['BATCH-A-2']['verdict'] == '复采'
    assert review['BATCH-B-1']['verdict'] == '保留' and review['BATCH-B-2']['verdict'] == '保留'
    assert review['BATCH-C-1']['verdict'] == '保留'
    edge = next(e for e in report['edges'] if e['slip_id'] == 'SLIP-AB-1')
    assert edge['corrected'] is True and edge['original_value_mm'] == 1.0 and edge['value_mm'] == 4.0


def test_route_head_replace_recaptures_only_old_b_batches():
    case = load_case()
    report = scanner.assess(case, case['route_head_replace'])
    assert report['conflicts'] == []
    certs = report['certificates']
    assert certs['SCAN-A']['status'] == '可用'
    assert close(certs['SCAN-A']['estimate_mm'], 2.0)
    assert certs['SCAN-B']['status'] == '必定可用'
    assert equipment.stop_list(report)['停机'] == []
    assert {e['slip_id'] for e in report['excluded']} == {'SLIP-BR-1', 'SLIP-AB-1'}
    assert report['faults'][0]['until_at'] == 610
    review = equipment.batch_review(report, batches_of(case))
    assert review['BATCH-B-1']['verdict'] == '复采'
    assert review['BATCH-B-2']['verdict'] == '保留'
    assert review['BATCH-A-1']['verdict'] == '保留' and review['BATCH-A-2']['verdict'] == '保留'


def test_two_approved_routes_diverge_from_same_records():
    case = load_case()
    r1 = scanner.assess(case, case['route_gauge_verify'])
    r2 = scanner.assess(case, case['route_head_replace'])
    assert equipment.stop_list(r1)['停机'] == ['SCAN-A']
    assert equipment.stop_list(r2)['停机'] == []
    batches = batches_of(case)
    rec1 = {k for k, v in equipment.batch_review(r1, batches).items() if v['verdict'] == '复采'}
    rec2 = {k for k, v in equipment.batch_review(r2, batches).items() if v['verdict'] == '复采'}
    assert rec1 == {'BATCH-A-1', 'BATCH-A-2'}
    assert rec2 == {'BATCH-B-1'}
    assert rec1 != rec2
    e1 = {e['slip_id']: e['value_mm'] for e in r1['edges']}
    e2 = {e['slip_id']: e['value_mm'] for e in r2['edges']}
    assert e1['SLIP-AB-1'] == 4.0
    assert 'SLIP-AB-1' not in e2


def test_arbitrary_reference_cannot_absorb_conflict():
    case = load_case()
    rigged = copy.deepcopy(case)
    for d in rigged['devices']:
        d['reference_mm'] = 12345
    base = scanner.assess(case)
    rig = scanner.assess(rigged)
    assert rig['conflicts'][0]['edges'] == base['conflicts'][0]['edges']
    assert close(rig['conflicts'][0]['residual_mm'], base['conflicts'][0]['residual_mm'])


def test_late_certificate_recomputes_only_dependent_batches():
    case = load_case()
    batches = batches_of(case)
    before = scanner.assess(case)
    assert equipment.batch_review(before, batches)['BATCH-C-1']['verdict'] == '保留'
    assert equipment.batch_review(before, batches)['BATCH-A-1']['verdict'] == '待核'
    cert_only = [r for r in case['route_gauge_verify'] if r['kind'] == 'certificate']
    after = scanner.assess(case, cert_only)
    scope = equipment.recompute_scope(after, 'GAUGE-R', batches)
    assert scope == ['BATCH-A-1', 'BATCH-A-2', 'BATCH-B-1', 'BATCH-B-2']
    assert 'BATCH-C-1' not in scope
    assert equipment.recompute_scope(after, 'GAUGE-S', batches) == ['BATCH-C-1']


def test_sqlite_persistence_and_idempotent_replay():
    case = load_case()
    con = storage.connect(':memory:')
    try:
        scanner.persist(con, case)
        n_events = con.execute('SELECT COUNT(*) c FROM events').fetchone()['c']
        n_dec = con.execute('SELECT COUNT(*) c FROM decisions').fetchone()['c']
        assert n_dec == 3
        outcomes = {row['subject']: row['outcome'] for row in
                    con.execute("SELECT subject, outcome FROM decisions WHERE subsystem='scanner'")}
        assert outcomes == {'SCAN-A': '待核', 'SCAN-B': '待核', 'SCAN-C': '必定可用'}
        scanner.persist(con, case)
        assert con.execute('SELECT COUNT(*) c FROM events').fetchone()['c'] == n_events
        assert con.execute('SELECT COUNT(*) c FROM decisions').fetchone()['c'] == n_dec
        scanner.persist(con, case, case['route_gauge_verify'])
        assert con.execute('SELECT COUNT(*) c FROM events').fetchone()['c'] == n_events+5
        assert con.execute('SELECT COUNT(*) c FROM decisions').fetchone()['c'] == n_dec+3
        latest = {}
        for row in con.execute('SELECT subject, outcome FROM decisions ORDER BY id'):
            latest[row['subject']] = row['outcome']
        assert latest == {'SCAN-A': '必定超限', 'SCAN-B': '必定可用', 'SCAN-C': '必定可用'}
    finally:
        con.close()
