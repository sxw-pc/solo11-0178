"""Capture daily window reports: raw bytes, revisions, arrivals, revocations.

Connections must use sqlite3.Row as row_factory.
"""
import hashlib, json

SCHEMA="""
CREATE TABLE IF NOT EXISTS report_batches(id TEXT PRIMARY KEY, month TEXT NOT NULL, window TEXT NOT NULL, revision INTEGER NOT NULL, arrived_at INTEGER NOT NULL, raw TEXT NOT NULL, digest TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, revoked_at INTEGER, revoke_reason TEXT);
CREATE TABLE IF NOT EXISTS report_capture_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, kind TEXT NOT NULL, at INTEGER NOT NULL, digest TEXT NOT NULL);
"""

def init(con):
    con.executescript(SCHEMA)

def _canonical(payload):
    return json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def digest_of(raw):
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def raw_of(batch):
    return _canonical({'window':batch['window'],'revision':int(batch['revision']),'rows':batch['rows']})

def ingest(con,batch):
    """Store one daily report batch; identical redelivery is idempotent."""
    init(con)
    raw=raw_of(batch); digest=digest_of(raw)
    old=con.execute('SELECT digest FROM report_batches WHERE id=?',(batch['id'],)).fetchone()
    if old:
        if old['digest']!=digest: raise ValueError('batch-id-conflict')
        return {'outcome':'idempotent','batch':batch['id'],'digest':digest}
    with con:
        con.execute('INSERT INTO report_batches(id,month,window,revision,arrived_at,raw,digest) VALUES(?,?,?,?,?,?,?)',(batch['id'],batch['month'],batch['window'],int(batch['revision']),int(batch['arrived_at']),raw,digest))
        con.execute('INSERT INTO report_capture_events(batch_id,kind,at,digest) VALUES(?,?,?,?)',(batch['id'],'ingest',int(batch['arrived_at']),digest))
    return {'outcome':'captured','batch':batch['id'],'digest':digest}

def revoke(con,batch_id,revoked_at,reason=''):
    """Record a source revocation event; the batch stops feeding new versions."""
    init(con)
    row=con.execute('SELECT digest,revoked FROM report_batches WHERE id=?',(batch_id,)).fetchone()
    if row is None: raise ValueError('unknown-batch')
    if row['revoked']: return {'outcome':'idempotent','batch':batch_id}
    with con:
        con.execute('UPDATE report_batches SET revoked=1,revoked_at=?,revoke_reason=? WHERE id=?',(int(revoked_at),reason,batch_id))
        con.execute('INSERT INTO report_capture_events(batch_id,kind,at,digest) VALUES(?,?,?,?)',(batch_id,'revoke',int(revoked_at),row['digest']))
    return {'outcome':'revoked','batch':batch_id}

def get(con,batch_id):
    init(con)
    row=con.execute('SELECT * FROM report_batches WHERE id=?',(batch_id,)).fetchone()
    return dict(row) if row else None

def effective(con,month):
    """Latest non-revoked revision per window, ordered by window."""
    init(con)
    chosen={}
    for row in con.execute('SELECT * FROM report_batches WHERE month=? AND revoked=0 ORDER BY window,revision',(month,)):
        row=dict(row)
        if row['window'] not in chosen or row['revision']>chosen[row['window']]['revision']: chosen[row['window']]=row
    return [chosen[w] for w in sorted(chosen)]

def events(con):
    init(con)
    return [dict(r) for r in con.execute('SELECT batch_id,kind,at,digest FROM report_capture_events ORDER BY seq')]
