"""Capture scanned archive material with physical provenance.

A scan carries an upload ``batch`` and an OCR ``material``/``version``.  The
batch is the physical provenance: if OCR relabels a page, the batch still
decides which material and version the page belongs to.  Original page digests
(and image bytes when supplied) plus immutable seal snapshots are stored in
SQLite.
"""
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

_COLLECTIONS = ('batches', 'scans', 'receipts', 'identities', 'corrections',
                'revocations', 'windows')
_SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_batches(
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, material TEXT NOT NULL,
    version INTEGER NOT NULL, page_count INTEGER NOT NULL,
    uploaded_at INTEGER NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capture_pages(
    batch TEXT NOT NULL, version INTEGER NOT NULL, page INTEGER NOT NULL,
    kind TEXT NOT NULL, sha256 TEXT NOT NULL, image BLOB,
    PRIMARY KEY(batch, version, page));
CREATE TABLE IF NOT EXISTS seal_snapshots(
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
    seq INTEGER NOT NULL, open INTEGER NOT NULL, digest TEXT NOT NULL,
    payload TEXT NOT NULL, UNIQUE(case_id, seq));
CREATE TABLE IF NOT EXISTS capture_windows(
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, opens_at INTEGER NOT NULL,
    closes_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS capture_observations(
    batch TEXT NOT NULL, material TEXT NOT NULL, version INTEGER NOT NULL,
    page INTEGER NOT NULL, sha256 TEXT NOT NULL, received_at INTEGER NOT NULL,
    duplicate INTEGER NOT NULL, PRIMARY KEY(batch, page, sha256));
"""


def normalize(record):
    result = copy.deepcopy(record)
    for key in _COLLECTIONS:
        result.setdefault(key, [])
    result.setdefault('required', [])
    return result


def capture(record):
    return normalize(record)


def batch_index(record):
    return {batch['id']: batch for batch in record.get('batches', [])}


def physical_scan(record, scan):
    """Resolve material/version/page_count for a scan from its upload batch."""
    batch = batch_index(record).get(scan.get('batch'))
    if batch is not None:
        return {
            'batch': batch['id'],
            'material': batch.get('material', scan.get('material')),
            'version': int(batch.get('version', scan.get('version', 1))),
            'page_count': int(batch.get('page_count') or 0),
        }
    return {
        'batch': None,
        'material': scan.get('material'),
        'version': int(scan.get('version', 1)),
        'page_count': int(scan.get('page_count') or 0),
    }


def _append_unique(record, key, item, identity):
    for old in record[key]:
        if old.get(identity) != item.get(identity):
            continue
        if canonical(old) != canonical(item):
            raise ValueError(f'{key}-conflict:{item.get(identity)}')
        return False
    record[key].append(copy.deepcopy(item))
    return True


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _page_blob(page):
    if page.get('image_b64') is not None:
        import base64
        return base64.b64decode(page['image_b64'])
    return None


def receive(record, batch, *, duplicate=False):
    """Record one PULL upload batch.

    ``duplicate=True`` models a re-scan of the same physical pages.  Batches are
    written once; identical page digests are deduplicated, while a different
    digest for the same batch/page is kept so the archive evaluator can flag it.
    """
    record.setdefault('batches', [])
    record.setdefault('scans', [])
    pages = []
    for page in batch['pages']:
        number = int(page['page'])
        sha = page.get('sha256') or hashlib.sha256(_page_blob(page) or b'').hexdigest()
        pages.append({
            'page': number,
            'sha256': sha,
            'image_b64': page.get('image_b64'),
            'ocr_material': page.get('ocr_material', batch['material']),
            'ocr_version': int(page.get('ocr_version', batch['version'])),
        })
    stored = {
        'id': batch['id'],
        'batch': batch['id'],
        'material': batch['material'],
        'version': int(batch['version']),
        'page_count': int(batch.get('page_count') or len(pages)),
        'pages': pages,
        'uploaded_at': int(batch.get('uploaded_at', len(record['batches']) + 1)),
    }
    known = next((item for item in record['batches'] if item.get('id') == batch['id']), None)
    if known is None:
        record['batches'].append(stored)
    elif (known.get('material'), int(known.get('version', 1))) != (
            stored['material'], stored['version']):
        # Reusing a batch id for a different material/version would silently
        # move the physical provenance of every page already recorded.
        raise ValueError(f"batch-identity-conflict:{batch['id']}")
    # If the batch id is reused for the same material/version, keep the first
    # physical page set.  A later upload claiming a different digest for the
    # same page is still appended as a scan so the archive evaluator can report
    # a conflicting page; the original batch record is never overwritten.
    added = 0
    for page in pages:
        same = any(
            scan.get('batch') == batch['id']
            and int(scan['page']) == page['page']
            and scan.get('sha256') == page['sha256']
            for scan in record['scans'])
        if same:
            continue
        record['scans'].append({
            'batch': batch['id'],
            'material': page['ocr_material'] if duplicate else stored['material'],
            'version': page['ocr_version'] if duplicate else stored['version'],
            'page': page['page'],
            'page_count': stored['page_count'],
            'sha256': page['sha256'],
            'signed_at': int(batch.get('signed_at', stored['uploaded_at'])),
            'received_at': int(batch.get('received_at', stored['uploaded_at'])),
            'duplicate': bool(duplicate),
        })
        added += 1
    return added


def add_reference(record, url, *, role, target):
    record.setdefault('receipts', [])
    return _append_unique(record, 'receipts', {
        'id': url, 'role': role,
        'material': target['material'], 'version': int(target['version']),
    }, 'id')


def correct(record, batch, correction):
    """Record a correction (补正) pointing from an older version to a newer one."""
    record.setdefault('corrections', [])
    return _append_unique(record, 'corrections', {
        'id': correction.get('id', batch),
        'batch': batch, 'material': correction['material'],
        'version': int(correction['version']),
        'from_version': int(correction.get('from_version', correction['version'])),
        'signed_at': int(correction.get('signed_at', 0)),
        'received_at': int(correction.get('received_at', correction.get('signed_at', 0))),
    }, 'id')


def revoke(record, batch, revocation):
    """Record a withdrawal (撤销) of a version and/or the receipt that cited it."""
    record.setdefault('revocations', [])
    return _append_unique(record, 'revocations', {
        'id': revocation.get('id', batch),
        'batch': batch, 'material': revocation['material'],
        'version': int(revocation['version']),
        'receipt': revocation.get('receipt'),
        'signed_at': int(revocation.get('signed_at', 0)),
        'received_at': int(revocation.get('received_at', revocation.get('signed_at', 0))),
    }, 'id')


def window(record, url, *, opens_at, closes_at):
    """Record the intake window a batch was received in."""
    record.setdefault('windows', [])
    return _append_unique(record, 'windows', {
        'id': url, 'case_id': record.get('case_id'),
        'opens_at': int(opens_at), 'closes_at': int(closes_at),
    }, 'id')


def identity(record, url, *, receipt, material, version, batch):
    """Record which physical batch proved the identity behind a receipt."""
    record.setdefault('identities', [])
    return _append_unique(record, 'identities', {
        'id': url, 'receipt': receipt, 'material': material,
        'version': int(version), 'batch': batch,
    }, 'id')


def capture_digest(record):
    return hashlib.sha256(canonical(record).encode()).hexdigest()


def inventory(record):
    """Deterministic page-level listing of captured evidence."""
    rows = {}
    for scan in record.get('scans', []):
        physical = physical_scan(record, scan)
        key = (physical['material'], physical['version'], int(scan['page']))
        rows.setdefault(key, {
            'batch': physical.get('batch') or scan.get('batch'),
            'material': key[0], 'version': key[1], 'page': key[2],
            'sha256': scan.get('sha256'),
        })
    return [rows[key] for key in sorted(rows)]


# --- SQLite persistence -----------------------------------------------------

def connect(path):
    if str(path) == ':memory:':
        con = sqlite3.connect(':memory:')
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _batch_page_blobs(batch):
    return {int(page['page']): _page_blob(page) for page in batch.get('pages', [])}


def store(con, record):
    """Insert original batches/pages once; identical retries are no-ops."""
    last = None
    blobs = {}
    for batch in record.get('batches', []):
        blobs[batch['id']] = _batch_page_blobs(batch)
        digest = hashlib.sha256(canonical(batch).encode()).hexdigest()
        old = con.execute('SELECT digest FROM capture_batches WHERE id=?', (batch['id'],)).fetchone()
        if old:
            if old['digest'] != digest:
                raise ValueError(f"batch-conflict:{batch['id']}")
            last = batch
            continue
        con.execute(
            'INSERT INTO capture_batches VALUES(?,?,?,?,?,?,?)',
            (batch['id'], record['case_id'], batch['material'], int(batch['version']),
             int(batch.get('page_count') or 0), int(batch.get('uploaded_at', 0)), digest))
        last = batch
    for scan in record.get('scans', []):
        physical = physical_scan(record, scan)
        batch_id = physical.get('batch') or scan.get('batch')
        if batch_id is None or physical['material'] is None:
            continue
        version = int(physical['version'])
        page = int(scan['page'])
        sha = scan.get('sha256')
        image = blobs.get(batch_id, {}).get(page)
        if image is None and scan.get('image_b64') is not None:
            image = _page_blob(scan)
        old = con.execute(
            'SELECT sha256 FROM capture_pages WHERE batch=? AND version=? AND page=?',
            (batch_id, version, page)).fetchone()
        if old:
            # First original wins; a later conflicting scan stays visible in
            # capture_observations and is reported by the archive evaluator.
            continue
        con.execute(
            'INSERT INTO capture_pages VALUES(?,?,?,?,?,?)',
            (batch_id, version, page, physical['material'], sha, image))
    for item in record.get('windows', []):
        con.execute(
            'INSERT OR IGNORE INTO capture_windows VALUES(?,?,?,?)',
            (item['id'], record['case_id'], int(item['opens_at']), int(item['closes_at'])))
    for scan in record.get('scans', []):
        physical = physical_scan(record, scan)
        batch_id = physical.get('batch') or scan.get('batch')
        if batch_id is None or physical['material'] is None:
            continue
        con.execute(
            'INSERT OR IGNORE INTO capture_observations VALUES(?,?,?,?,?,?,?)',
            (batch_id, scan.get('material', physical['material']),
             int(scan.get('version', physical['version'])), int(scan['page']),
             scan.get('sha256'), int(scan.get('received_at', 0)),
             1 if scan.get('duplicate') else 0))
    return last


def seal(con, record, verdict):
    """Persist a new immutable snapshot; an identical repeated seal is a no-op."""
    payload = canonical(verdict)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    same = con.execute(
        'SELECT id, seq FROM seal_snapshots WHERE case_id=? AND digest=?',
        (record['case_id'], digest)).fetchone()
    if same:
        return {'seq': same['seq'], 'digest': digest, 'created': False}
    seq = con.execute(
        'SELECT COALESCE(MAX(seq),0)+1 n FROM seal_snapshots WHERE case_id=?',
        (record['case_id'],)).fetchone()['n']
    con.execute(
        'INSERT INTO seal_snapshots(case_id,seq,open,digest,payload) VALUES(?,?,?,?,?)',
        (record['case_id'], seq, 1 if verdict.get('state') == 'sealed' else 0, digest, payload))
    return {'seq': seq, 'digest': digest, 'created': True}


def snapshots(con, case_id):
    return [json.loads(row['payload']) for row in con.execute(
        'SELECT payload FROM seal_snapshots WHERE case_id=? ORDER BY seq', (case_id,))]
