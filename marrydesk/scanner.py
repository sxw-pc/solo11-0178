"""证件扫描仪几何偏移校准网。

共用标尺与每台扫描仪的偏移是共同变量; 每条量测只约束两端相对误差及不确定度。
闭环残差超限时报最小矛盾边集与逐边可行世界, 不把一条粗差平均摊到所有设备;
未取得绝对锚点(量规检定证书)前只给可行范围, 不把标尺标称值当绝对真值。
"""
import hashlib, heapq, json, math
from . import storage


def drift_mm(record):
    return abs(record['measured_mm']-record['reference_mm'])


# ---------- 校准网 ----------

def build_edges(slips, corrections=(), findings=()):
    """量测单 -> 有向边; 补证纠错留原值, 故障认定作废的边留证不进网。"""
    corrected = {c['slip_id']: c for c in corrections}
    voided = {}
    for f in findings:
        for sid in f['invalidates']:
            voided[sid] = f
    active, excluded = [], []
    for s in slips:
        if s['slip_id'] in voided:
            f = voided[s['slip_id']]
            excluded.append({'slip_id': s['slip_id'], 'reason': f['cause'], 'by': f['finding_id']})
            continue
        e = {'slip_id': s['slip_id'], 'head': s['head'], 'tail': s['tail'],
             'value_mm': s['value_mm'], 'uncertainty_mm': s['uncertainty_mm'], 'corrected': False}
        c = corrected.get(s['slip_id'])
        if c and c['field'] == 'value_mm':
            e.update(original_value_mm=s['value_mm'], value_mm=c['corrected_mm'],
                     corrected=True, evidence=c.get('evidence', ''))
        active.append(e)
    return active, excluded


def components(nodes, edges):
    adj = {n: set() for n in nodes}
    for e in edges:
        adj.setdefault(e['head'], set()).add(e['tail'])
        adj.setdefault(e['tail'], set()).add(e['head'])
    seen, comps = set(), []
    for n in adj:
        if n in seen:
            continue
        comp, stack = set(), [n]
        while stack:
            x = stack.pop()
            if x in comp:
                continue
            comp.add(x)
            seen.add(x)
            stack.extend(adj[x]-comp)
        comps.append(sorted(comp))
    return comps


def _tree_path(parent, a, b):
    anc = set()
    x = a
    while x is not None:
        anc.add(x)
        x = parent[x][0] if parent[x] else None
    up_b = []
    x = b
    while x not in anc:
        p, e = parent[x]
        up_b.append((e, 1.0 if e['head'] == x else -1.0))
        x = p
    up_a = []
    y = a
    while y != x:
        p, e = parent[y]
        up_a.append((e, 1.0 if e['head'] == y else -1.0))
        y = p
    return up_a+[(e, -s) for e, s in reversed(up_b)]


def loop_residuals(edges, k=3.0):
    """生成森林外的每条边闭合一个回路; 残差超 k 倍合成不确定度即不一致。"""
    adj = {}
    for e in edges:
        adj.setdefault(e['head'], []).append((e['tail'], e))
        adj.setdefault(e['tail'], []).append((e['head'], e))
    loops, visited = [], set()
    for start in adj:
        if start in visited:
            continue
        parent = {start: (None, None)}
        tree = set()
        stack = [start]
        visited.add(start)
        while stack:
            x = stack.pop()
            for y, e in adj[x]:
                if y not in parent:
                    parent[y] = (x, e)
                    tree.add(e['slip_id'])
                    visited.add(y)
                    stack.append(y)
        for e in edges:
            if e['slip_id'] in tree or e['head'] not in parent:
                continue
            path = _tree_path(parent, e['head'], e['tail'])
            residual = sum(s*x['value_mm'] for x, s in path)-e['value_mm']
            u2 = sum(x['uncertainty_mm']**2 for x, _ in path)+e['uncertainty_mm']**2
            ids = sorted({x['slip_id'] for x, _ in path} | {e['slip_id']})
            loops.append({'edges': ids, 'residual_mm': residual, 'tolerance_mm': k*math.sqrt(u2),
                          'inconsistent': abs(residual) > k*math.sqrt(u2)})
    return loops


def _solve_linear(mat, vec):
    n = len(vec)
    a = [row[:]+[vec[i]] for i, row in enumerate(mat)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            raise ValueError('singular-system')
        a[col], a[piv] = a[piv], a[col]
        for r in range(n):
            if r != col and a[r][col] != 0.0:
                f = a[r][col]/a[col][col]
                for c in range(col, n+1):
                    a[r][c] -= f*a[col][c]
    return [a[i][n]/a[i][i] for i in range(n)]


def _lstsq(nodes, edges, pin):
    """钉住 pin=0 的加权最小二乘(仅作解算/诊断, 不作签证依据)。"""
    idx = {n: i for i, n in enumerate(n for n in nodes if n != pin)}
    m = len(idx)
    mat = [[0.0]*m for _ in range(m)]
    vec = [0.0]*m
    for e in edges:
        w = 1.0/e['uncertainty_mm']**2
        a = [0.0]*m
        if e['head'] in idx:
            a[idx[e['head']]] = 1.0
        if e['tail'] in idx:
            a[idx[e['tail']]] = -1.0
        for i in range(m):
            if a[i] == 0.0:
                continue
            vec[i] += w*a[i]*e['value_mm']
            for j in range(m):
                if a[j] != 0.0:
                    mat[i][j] += w*a[i]*a[j]
    sol = _solve_linear(mat, vec) if m else []
    fit = {pin: 0.0}
    for n, i in idx.items():
        fit[n] = sol[i]
    return fit


def _path_uncertainty(nodes, edges, src):
    dist = {n: math.inf for n in nodes}
    dist[src] = 0.0
    adj = {n: [] for n in nodes}
    for e in edges:
        w = e['uncertainty_mm']**2
        adj[e['head']].append((e['tail'], w))
        adj[e['tail']].append((e['head'], w))
    heap = [(0.0, src)]
    while heap:
        d, x = heapq.heappop(heap)
        if d > dist[x]:
            continue
        for y, w in adj[x]:
            if d+w < dist[y]:
                dist[y] = d+w
                heapq.heappush(heap, (d+w, y))
    return {n: math.sqrt(d) for n, d in dist.items()}


def solve_offsets(nodes, edges, certificates=()):
    """逐连通分量解算; 有检定证书的分量给绝对区间, 无锚分量只给相对偏移。"""
    latest = {}
    for c in certificates:
        g = c['gauge_id']
        if g not in latest or c['effective_at'] >= latest[g]['effective_at']:
            latest[g] = c
    anchors, offsets = {}, {}
    for comp in components(nodes, edges):
        anchor = None
        for n in comp:
            if n in latest and (anchor is None or latest[n]['effective_at'] >= anchor['effective_at']):
                anchor = latest[n]
        sub = [e for e in edges if e['head'] in comp and e['tail'] in comp]
        if anchor is None:
            pin = comp[0]
            fit = _lstsq(comp, sub, pin)
            for n in comp:
                offsets[n] = {'value': fit[n], 'lo': None, 'hi': None,
                              'anchored': False, 'reference': pin}
        else:
            pin = anchor['gauge_id']
            anchors[pin] = {'offset_mm': anchor['offset_mm'],
                            'uncertainty_mm': anchor['uncertainty_mm'], 'cert_id': anchor['cert_id']}
            fit = _lstsq(comp, sub, pin)
            sig = _path_uncertainty(comp, sub, pin)
            for n in comp:
                v = anchor['offset_mm']+fit[n]
                hw = math.sqrt(anchor['uncertainty_mm']**2+sig[n]**2)
                offsets[n] = {'value': v, 'lo': v-hw, 'hi': v+hw,
                              'anchored': True, 'reference': pin}
    return {'components': components(nodes, edges), 'anchors': anchors, 'offsets': offsets}


def assess(case, extra=()):
    """组校准网并出报告: 闭环矛盾->逐边可行世界; 锚定->绝对区间; 否则待核。"""
    records = list(case.get('captures', []))+list(extra)
    devices = {d['device_id']: d for d in case.get('devices', [])}
    gauges = {g['gauge_id']: g for g in case.get('gauges', [])}
    slips = [r for r in records if r['kind'] == 'slip']
    certificates = [r for r in records if r['kind'] == 'certificate']
    corrections = [r for r in records if r['kind'] == 'correction']
    findings = [r for r in records if r['kind'] == 'finding']
    revoked = {r['order_id'] for r in records if r['kind'] == 'revocation'}
    maintenance = [r for r in records if r['kind'] == 'maintenance' and r['order_id'] not in revoked]
    edges, excluded = build_edges(slips, corrections, findings)
    nodes = list(devices)+list(gauges)
    sol = solve_offsets(nodes, edges, certificates)
    loops = loop_residuals(edges)
    comp_of = {n: i for i, comp in enumerate(sol['components']) for n in comp}
    conflicts = []
    for lp in loops:
        if not lp['inconsistent']:
            continue
        worlds = []
        for sid in lp['edges']:
            sub = [e for e in edges if e['slip_id'] != sid]
            sub_sol = solve_offsets(nodes, sub, certificates)
            worlds.append({'suspect': sid,
                           'consistent': not any(l['inconsistent'] for l in loop_residuals(sub)),
                           'offsets': sub_sol['offsets']})
        conflicts.append({'edges': lp['edges'], 'residual_mm': lp['residual_mm'],
                          'tolerance_mm': lp['tolerance_mm'], 'worlds': worlds})
    conflict_comps = set()
    for c in conflicts:
        for e in edges:
            if e['slip_id'] in c['edges']:
                conflict_comps.add(comp_of[e['head']])
    certs = {}
    for dev_id, dev in devices.items():
        limit = float(dev['limit_mm'])
        off = sol['offsets'].get(dev_id)
        base = {'device_id': dev_id, 'limit_mm': limit}
        causes = []
        if comp_of.get(dev_id) in conflict_comps:
            causes.append('存在未决矛盾回路, 逐边可行世界见 conflicts')
        if off is None or not off['anchored']:
            causes.append('缺少绝对锚点(量规检定证书), 仅能给相对偏移')
        if causes:
            certs[dev_id] = {**base, 'status': '待核', 'valid': False, 'estimate_mm': None,
                             'offset_mm': None, 'reason': '; '.join(causes)}
        else:
            lo, hi, v = off['lo'], off['hi'], off['value']
            if lo > limit or hi < -limit:
                status = '必定超限'
            elif lo >= -limit and hi <= limit:
                status = '必定可用'
            elif abs(v) <= limit:
                status = '可用'
            else:
                status = '待核'
            certs[dev_id] = {**base, 'status': status, 'valid': status != '待核',
                             'estimate_mm': v, 'offset_mm': [lo, hi], 'reason': '锚定校准网解算'}
    faults = []
    for f in findings:
        order = next((m for m in maintenance if m['order_id'] == f.get('repaired_by')), None)
        faults.append({'device_id': f['device_id'], 'finding_id': f['finding_id'],
                       'cause': f['cause'], 'until_at': order['effective_at'] if order else None})
    diagnostic = {}
    for comp in sol['components']:
        pin = next((n for n in comp if n in gauges), comp[0])
        sub = [e for e in edges if e['head'] in comp and e['tail'] in comp]
        diagnostic.update(_lstsq(comp, sub, pin))
    notes = ['共用标尺标称值不作为绝对真值; 无检定证书的分量只给相对偏移']
    if conflicts:
        notes.append('矛盾回路未决期间不出具有效证书; 残差由可疑边承担, 不平均摊到各边')
    return {'devices': sorted(devices), 'gauges': sorted(gauges), 'edges': edges,
            'excluded': excluded, 'components': sol['components'], 'anchors': sol['anchors'],
            'offsets': sol['offsets'], 'loops': loops, 'conflicts': conflicts,
            'certificates': certs, 'faults': faults, 'maintenance': maintenance,
            'diagnostic_fit': diagnostic, 'notes': notes}


def persist(con, case, extra=()):
    """采录记录与裁定入库; 同一事件重放幂等, 晚到补证触发新一次裁定。"""
    report = assess(case, extra)
    records = list(case.get('captures', []))+list(extra)
    key_of = {'slip': 'slip_id', 'certificate': 'cert_id', 'batch': 'batch_id',
              'maintenance': 'order_id', 'revocation': 'revocation_id',
              'correction': 'correction_id', 'finding': 'finding_id'}
    with con:
        for rec in records:
            rid = rec[key_of[rec['kind']]]
            subject = (rec.get('device_id') or rec.get('gauge_id') or rec.get('slip_id')
                       or rec.get('order_id') or f"{rec.get('head')}~{rec.get('tail')}")
            storage.record(con, 'scanner', {'id': rid, 'subject': subject, 'action': rec['kind'],
                                            'effective_at': rec.get('effective_at', 0),
                                            'received_at': rec.get('effective_at', 0), 'data': rec})
        for i, dup in enumerate(case.get('duplicates', [])):
            storage.record(con, 'scanner', {'id': f"{dup['slip_id']}#dup{i+1}",
                                            'subject': dup['slip_id'], 'action': 'duplicate',
                                            'effective_at': dup.get('effective_at', 0),
                                            'received_at': dup.get('effective_at', 0), 'data': dup})
        at = max((r.get('effective_at', 0) for r in records), default=0)
        digest = hashlib.sha256(json.dumps(report['certificates'], ensure_ascii=False,
                                           sort_keys=True).encode()).hexdigest()[:12]
        event = {'id': f'assess-{digest}', 'subject': 'calibration', 'action': 'assess',
                 'effective_at': at, 'received_at': at}
        if storage.record(con, 'scanner', event):
            for dev_id in sorted(report['certificates']):
                cert = report['certificates'][dev_id]
                storage.decide(con, {'id': event['id'], 'subject': dev_id}, 'scanner',
                               cert['status'], cert['reason'], cert['status'])
    return report
