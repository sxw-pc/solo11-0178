"""采录层: 交叉量测单/检定证书/扫描批次/维护单的 OCR 解析、单位换算与去重。

每条量测单保留原照片坐标与量测方向(value_mm = head - tail), OCR 原文不就地涂改;
纠错只能以补证(原图复核 correction)追加, 低置信 OCR 单保留在网内等待补证排除。
"""
import re

UNITS_MM = {'mm': 1.0, 'cm': 10.0, 'in': 25.4}

_SLIP_RE = re.compile(r'^\s*([A-Za-z0-9-]+)\s*-\s*([A-Za-z0-9-]+)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*(mm|cm|in)\s*$')


def ocr_parse(raw_text):
    m = _SLIP_RE.match(raw_text or '')
    if not m:
        raise ValueError(f'ocr-unparseable:{raw_text!r}')
    head, tail, value, unit = m.groups()
    return {'head': head, 'tail': tail, 'value': float(value), 'unit': unit}


def to_mm(value, unit):
    if unit not in UNITS_MM:
        raise ValueError(f'unknown-unit:{unit}')
    return value*UNITS_MM[unit]


def normalize_slip(raw):
    parsed = ocr_parse(raw.get('raw_text', ''))
    return {'kind': 'slip', 'slip_id': raw['slip_id'],
            'head': parsed['head'], 'tail': parsed['tail'],
            'direction': f"{parsed['head']}->{parsed['tail']}",
            'value_mm': to_mm(parsed['value'], parsed['unit']),
            'unit_raw': parsed['unit'],
            'uncertainty_mm': float(raw.get('uncertainty_mm', 0.5)),
            'ocr_confidence': float(raw.get('ocr_confidence', 1.0)),
            'raw_text': raw.get('raw_text', ''),
            'photo': raw.get('photo'),
            'effective_at': int(raw.get('effective_at', 0))}


def normalize_certificate(raw):
    return {'kind': 'certificate', 'cert_id': raw['cert_id'], 'gauge_id': raw['gauge_id'],
            'offset_mm': float(raw['offset_mm']), 'uncertainty_mm': float(raw['uncertainty_mm']),
            'authority': raw.get('authority', ''), 'effective_at': int(raw.get('effective_at', 0))}


def normalize_batch(raw):
    return {'kind': 'batch', 'batch_id': raw['batch_id'], 'device_id': raw['device_id'],
            'start_at': int(raw['start_at']), 'end_at': int(raw['end_at']),
            'pages': int(raw.get('pages', 0))}


def normalize_maintenance(raw):
    return {'kind': 'maintenance', 'order_id': raw['order_id'], 'device_id': raw['device_id'],
            'action': raw.get('action', ''), 'effective_at': int(raw.get('effective_at', 0))}


def normalize_revocation(raw):
    return {'kind': 'revocation', 'revocation_id': raw['revocation_id'], 'order_id': raw['order_id'],
            'effective_at': int(raw.get('effective_at', 0))}


def normalize_correction(raw):
    return {'kind': 'correction', 'correction_id': raw['correction_id'], 'slip_id': raw['slip_id'],
            'field': raw.get('field', 'value_mm'), 'corrected_mm': float(raw['corrected_mm']),
            'evidence': raw.get('evidence', ''), 'photo': raw.get('photo'),
            'effective_at': int(raw.get('effective_at', 0))}


def normalize_finding(raw):
    return {'kind': 'finding', 'finding_id': raw['finding_id'], 'device_id': raw['device_id'],
            'invalidates': list(raw.get('invalidates', [])), 'cause': raw.get('cause', ''),
            'repaired_by': raw.get('repaired_by'), 'approved_by': raw.get('approved_by', ''),
            'effective_at': int(raw.get('effective_at', 0))}


_NORMALIZERS = {'slip': normalize_slip, 'certificate': normalize_certificate,
                'batch': normalize_batch, 'maintenance': normalize_maintenance,
                'revocation': normalize_revocation, 'correction': normalize_correction,
                'finding': normalize_finding}


def normalize_record(raw):
    kind = raw.get('kind')
    if kind not in _NORMALIZERS:
        raise ValueError(f'unknown-kind:{kind}')
    return _NORMALIZERS[kind](raw)


def dedup(slips):
    """同一 slip_id 且内容一致视为重复采录; 同号不同文拒绝(防涂改)。"""
    by_id, kept, duplicates = {}, [], []
    for s in slips:
        sig = (s['head'], s['tail'], round(s['value_mm'], 6), s['effective_at'])
        prev = by_id.get(s['slip_id'])
        if prev is not None:
            if prev != sig:
                raise ValueError(f"slip-id-conflict:{s['slip_id']}")
            duplicates.append({**s, 'duplicate_of': s['slip_id']})
            continue
        by_id[s['slip_id']] = sig
        kept.append(s)
    return kept, duplicates


def ingest(payload):
    """整包采录: 规范化各段记录, 量测单去重, 重复件留证不删除。"""
    case = {'devices': list(payload.get('devices', [])), 'gauges': list(payload.get('gauges', [])),
            'duplicates': []}
    for key, val in payload.items():
        if key in ('devices', 'gauges') or not isinstance(val, list):
            continue
        case[key] = [normalize_record(r) for r in val]
    slips = [r for r in case.get('captures', []) if r['kind'] == 'slip']
    kept, duplicates = dedup(slips)
    kept_ids = {id(s) for s in kept}
    case['captures'] = [r for r in case.get('captures', []) if r['kind'] != 'slip' or id(r) in kept_ids]
    case['duplicates'] = duplicates
    return case


def records(case, kind, extra=()):
    return [r for r in list(case.get('captures', []))+list(extra) if r.get('kind') == kind]
