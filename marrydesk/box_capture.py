"""Physical box evidence capture.

采集库房 RFID 扫描、人工移交单、扫描室出入台账、借阅申请与返还点收单，
规范化为统一事件流：盒标 OCR 去重、按发生时刻排序、撤销凭据留存、SQLite 入库。
迟到扫描以其真实发生时刻参与重建，只修订对应交接，不覆盖任何旧凭据。
"""
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS box_events(
  id TEXT PRIMARY KEY,
  box_id TEXT,
  kind TEXT NOT NULL,
  occurred_at INTEGER NOT NULL,
  uploaded_at INTEGER NOT NULL,
  raw TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0,
  revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS box_events_box ON box_events(box_id, occurred_at);
"""


def parse_ts(value):
    """ISO-8601 (含 Z) 或 epoch 秒/毫秒，统一转 epoch 秒。"""
    if isinstance(value, (int, float)):
        value = int(value)
        return value // 1000 if value > 10_000_000_000 else value
    text = str(value).strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def normalize_label(label):
    """盒标 OCR 归一化：去空白与标点差异，只保留字母数字与连字符。"""
    return ''.join(ch for ch in str(label).replace(' ', '').upper()
                   if ch.isalnum() or ch == '-')


def _registry_lookup(registry):
    by_label = {}
    for box in (registry or {}).values():
        by_label[normalize_label(box.get('label', box['box_id']))] = box['box_id']
    return by_label


def normalize(source):
    """把采录来源（fixture dict 或事件列表）规范化为去重、排序后的事件流。

    返回 (events, stats)。撤销事件保留在原始凭据中，被撤销的读头事件带
    revoked=True；同一事件 ID 重复上传按重试丢弃，不重复入库。
    """
    if isinstance(source, (bytes, str)):
        source = json.loads(source)
    if isinstance(source, dict):
        registry = source.get('boxes', {})
        raw_events = list(source.get('events', []))
    else:
        registry, raw_events = {}, list(source)
    by_label = _registry_lookup(registry)

    deduped, dropped = {}, 0
    rfid_fingerprints = set()
    for raw in raw_events:
        event = dict(raw)
        if event['id'] in deduped:
            dropped += 1
            continue
        event['occurred_at'] = parse_ts(event['occurred_at'])
        event['uploaded_at'] = parse_ts(event['uploaded_at'])
        event.setdefault('revoked', False)
        if 'label' in event and 'box_id' not in event:
            event['box_id'] = by_label.get(normalize_label(event['label']))
        if 'ocr_text' in event and event.get('kind') == 'register':
            event['label_normalized'] = normalize_label(event['ocr_text'])
        # 盒标去重：同一读头对同一盒在同一时刻的重复读到只保留首条。
        if event.get('kind') == 'rfid_read':
            fingerprint = (event.get('box_id'), event['reader'],
                            event['occurred_at'], event['location'])
            if fingerprint in rfid_fingerprints:
                dropped += 1
                continue
            rfid_fingerprints.add(fingerprint)
        deduped[event['id']] = event

    for event in deduped.values():
        if event.get('kind') != 'event_cancel':
            continue
        target = deduped.get(event['target_id'])
        if target is not None and not target.get('revoked'):
            target['revoked'] = True
            target['revoke_credential'] = event.get('credential')
            target['revoke_reason'] = event.get('reason')

    events = sorted(deduped.values(),
                    key=lambda item: (item['occurred_at'], item['id']))
    stats = {'raw_count': len(raw_events), 'unique_count': len(events),
             'dropped_duplicates': dropped,
             'revoked': sorted(e['id'] for e in events if e.get('revoked'))}
    return events, stats


def connect(path):
    if str(path) == ':memory:':
        con = sqlite3.connect(':memory:')
    else:
        from pathlib import Path
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def ingest(con, source):
    """规范化后写入 SQLite；已存在且原始载荷一致视为重试，冲突拒绝。"""
    events, stats = normalize(source)
    inserted, retries = 0, 0
    for event in events:
        raw = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with con:
            old = con.execute('SELECT raw FROM box_events WHERE id=?',
                              (event['id'],)).fetchone()
            if old is not None:
                if old['raw'] != raw:
                    raise ValueError('event-id-conflict:' + event['id'])
                retries += 1
                continue
            con.execute(
                'INSERT INTO box_events(id,box_id,kind,occurred_at,uploaded_at,raw,revoked,revoke_reason)'
                ' VALUES(?,?,?,?,?,?,?,?)',
                (event['id'], event.get('box_id'), event['kind'],
                 event['occurred_at'], event['uploaded_at'], raw,
                 1 if event.get('revoked') else 0, event.get('revoke_reason')))
            inserted += 1
    stats.update({'inserted': inserted, 'retries': retries})
    return stats


def load_events(con):
    return [json.loads(row['raw'])
            for row in con.execute('SELECT raw FROM box_events ORDER BY occurred_at,id')]
