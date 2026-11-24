from . import scanner


def state(record):
    return {'device_id':record['device_id'],
            'state':'可用' if scanner.drift_mm(record)<=record['limit_mm'] else '待检修'}


def stop_list(report):
    """按绝对限值圈停机设备; 待核设备不停机也不放行。"""
    out = {'停机': [], '待核': [], '可用': []}
    for dev_id in sorted(report['certificates']):
        status = report['certificates'][dev_id]['status']
        if status == '必定超限':
            out['停机'].append(dev_id)
        elif status == '待核':
            out['待核'].append(dev_id)
        else:
            out['可用'].append(dev_id)
    return out


def batch_review(report, batches):
    """历史采集批次: 超限设备与故障修复前的批次复采, 待核设备批次待核。"""
    faults = {f['device_id']: f for f in report.get('faults', [])}
    review = {}
    for b in batches:
        dev = b['device_id']
        cert = report['certificates'][dev]
        fault = faults.get(dev)
        if fault and (fault['until_at'] is None or b['start_at'] < fault['until_at']):
            review[b['batch_id']] = {'verdict': '复采',
                                     'reason': f"设备故障修复前采集({fault['finding_id']})"}
        elif cert['status'] == '必定超限':
            review[b['batch_id']] = {'verdict': '复采', 'reason': '设备必定超限'}
        elif cert['status'] == '待核':
            review[b['batch_id']] = {'verdict': '待核', 'reason': cert['reason']}
        else:
            review[b['batch_id']] = {'verdict': '保留', 'reason': f"设备{cert['status']}"}
    return review


def recompute_scope(report, gauge_id, batches):
    """晚到检定单只重算依赖该量规(同一连通分量)的批次。"""
    comp = next((c for c in report['components'] if gauge_id in c), [])
    return sorted(b['batch_id'] for b in batches if b['device_id'] in comp)
