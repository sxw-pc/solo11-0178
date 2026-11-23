"""SQLite event evidence and transactional domain state.

Beyond the original raw-event/digest ledger, this module keeps evidentiary
projection metadata so audits can prove *which* row diverged and from which
source event:

* ``capture_batches``  - provenance of bulk intake (backup verification
  sheets, raw-event export packages, manual repair records), each with an
  independent SHA-256 over the sealed bytes.
* ``event_sources``    - the raw byte digest, sequence number, effective and
  received instants, source batch and trust level for every event id.
* ``projection_sources`` - every projected row (subjects/documents/
  allocations/receipts/decisions) is stamped with its source event id, a
  monotonically increasing projection version and a digest of the row bytes.

All tables are created in a single idempotent schema; opening a legacy
database transparently adds the evidence tables without touching rows.
"""
import sqlite3, json, hashlib
from pathlib import Path

SCHEMA="""
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, subsystem TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL, effective_at INTEGER NOT NULL, received_at INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, subsystem TEXT NOT NULL, subject TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subjects(id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(id TEXT PRIMARY KEY, subject TEXT NOT NULL, resource TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, amount INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capture_batches(batch_id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_path TEXT NOT NULL, sealed_digest TEXT NOT NULL, captured_at INTEGER NOT NULL, trusted INTEGER NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_sources(event_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, effective_at INTEGER NOT NULL, received_at INTEGER NOT NULL, subsystem TEXT NOT NULL, raw_digest TEXT NOT NULL, batch_id TEXT, trusted INTEGER NOT NULL, anchored INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS projection_sources(row_key TEXT PRIMARY KEY, table_name TEXT NOT NULL, source_event_id TEXT NOT NULL, projection_version INTEGER NOT NULL, row_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repair_records(repair_id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL, route TEXT NOT NULL, reason TEXT NOT NULL, applied_at INTEGER NOT NULL, recorded_at INTEGER NOT NULL, plan TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quarantined_events(event_id TEXT PRIMARY KEY, subsystem TEXT NOT NULL, payload TEXT NOT NULL, stored_digest TEXT NOT NULL, actual_digest TEXT NOT NULL, reason TEXT NOT NULL, quarantined_at INTEGER NOT NULL);
"""

PROJECTION_TABLES=('subjects','documents','allocations','receipts','decisions')
TRUST_LEVELS={'untrusted':0,'tentative':1,'trusted':2}
TRUST_NAMES={v:k for k,v in TRUST_LEVELS.items()}


def connect(path):
    if str(path)==':memory:': con=sqlite3.connect(':memory:')
    else:
        path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        con=sqlite3.connect(path)
    con.row_factory=sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version','2')")
    return con


def canonical(raw):
    """Canonical byte serialization shared by every digest in the system.

    ``bytes``/``bytearray`` pass through untouched and ``str`` is encoded as
    its UTF-8 bytes (the exact bytes stored in an events payload cell); only
    structured values are JSON-serialized canonically.
    """
    if isinstance(raw,(bytes,bytearray)): return bytes(raw)
    if isinstance(raw,str): return raw.encode('utf-8')
    return json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')


def digest_of(raw):
    return hashlib.sha256(canonical(raw)).hexdigest()


def record(con, subsystem, event):
    raw=json.dumps(event,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    digest=hashlib.sha256(raw.encode()).hexdigest()
    old=con.execute('SELECT digest FROM events WHERE id=?',(event['id'],)).fetchone()
    if old:
        if old['digest'] != digest: raise ValueError('event-id-conflict')
        return False
    con.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',(event['id'],subsystem,event['action'],event['subject'],int(event.get('effective_at',0)),int(event.get('received_at',0)),raw,digest))
    return True


def decide(con,event,subsystem,outcome,reason,state):
    con.execute('INSERT INTO decisions(event_id,subsystem,subject,outcome,reason,state) VALUES(?,?,?,?,?,?)',(event['id'],subsystem,event['subject'],outcome,reason,state))
    return {'event':event['id'],'subsystem':subsystem,'subject':event['subject'],'outcome':outcome,'reason':reason,'state':state}


def latest(con, table, key):
    return con.execute(f'SELECT * FROM {table} WHERE id=?',(key,)).fetchone()


def report(con):
    events=[dict(x) for x in con.execute('SELECT id,subsystem,action,subject,effective_at,digest FROM events ORDER BY effective_at,id')]
    decisions=[dict(x) for x in con.execute('SELECT event_id,subsystem,subject,outcome,reason,state FROM decisions ORDER BY id')]
    return {'events':events,'decisions':decisions,'subjects':[dict(x) for x in con.execute('SELECT * FROM subjects ORDER BY id')], 'allocations':[dict(x) for x in con.execute('SELECT * FROM allocations ORDER BY id')], 'receipts':[dict(x) for x in con.execute('SELECT * FROM receipts ORDER BY id')]}


# ---------------------------------------------------------------- evidence --

def capture_batch(con, batch_id, kind, source_path, sealed_digest, captured_at,
                  trusted, detail=None):
    """Register a bulk intake batch (backup sheet / export package / repair).

    The sealed digest is computed by the caller over the original bytes of
    the intake artifact; this function only records the evidence. Re-registering
    the same batch id with identical evidence is idempotent, a conflicting
    re-registration raises ``ValueError`` so batches stay immutable.
    """
    detail_text=json.dumps(detail if detail is not None else {},ensure_ascii=False,sort_keys=True)
    old=con.execute('SELECT sealed_digest,trusted FROM capture_batches WHERE batch_id=?',(batch_id,)).fetchone()
    if old:
        if old['sealed_digest']!=sealed_digest or bool(old['trusted'])!=bool(trusted):
            raise ValueError('capture-batch-conflict')
        return False
    con.execute('INSERT INTO capture_batches VALUES(?,?,?,?,?,?,?)',
                (batch_id,kind,str(source_path),sealed_digest,int(captured_at),1 if trusted else 0,detail_text))
    return True


def note_event_source(con, event_id, seq, effective_at, received_at, subsystem,
                      raw_digest, batch_id=None, trusted='trusted', anchored=False,
                      detail=None):
    """Upsert source evidence for an event id, never silently downgrading trust."""
    if isinstance(trusted,str): trust=TRUST_LEVELS[trusted]
    else: trust=int(trusted)
    detail_text=json.dumps(detail if detail is not None else {},ensure_ascii=False,sort_keys=True)
    old=con.execute('SELECT trusted,anchored FROM event_sources WHERE event_id=?',(event_id,)).fetchone()
    if old:
        trust=max(trust,old['trusted'])
        anchored=bool(anchored) or bool(old['anchored'])
    con.execute('INSERT OR REPLACE INTO event_sources VALUES(?,?,?,?,?,?,?,?,?,?)',
                (event_id,int(seq),int(effective_at),int(received_at),subsystem,
                 raw_digest,batch_id,trust,1 if anchored else 0,detail_text))
    return trust


def stamp_projection(con, table_name, row, source_event_id):
    """Stamp one projected row with its source event and a row digest.

    The projection version only advances when the row bytes actually changed;
    re-stamping an unchanged row still refreshes the attributed source event
    (the latest event that touched it) without fabricating a new version.
    """
    row_key=f'{table_name}:{row["id"]}'
    row_digest=digest_of({k:row[k] for k in row.keys()})
    old=con.execute('SELECT projection_version,row_digest FROM projection_sources WHERE row_key=?',(row_key,)).fetchone()
    version=old['projection_version'] if old and old['row_digest']==row_digest else ((old['projection_version']+1) if old else 1)
    con.execute('INSERT OR REPLACE INTO projection_sources VALUES(?,?,?,?,?)',
                (row_key,table_name,source_event_id,version,row_digest))
    return version


def _projection_row_ids(con, table_name, event_id):
    """All projection rows whose current bytes were (re)written by this event.

    Besides rows keyed by the event itself (decisions, documents/receipts and
    the allocation produced by a booking), handlers also mutate rows that an
    earlier event created - e.g. a cancel/outage/revoke updates another
    allocation or the subject row. Such a row's source is the *latest* event
    that changed its bytes, so every row attached to the event's subject is
    restamped; the version increments on every actual byte change.
    """
    ev=con.execute('SELECT subject FROM events WHERE id=?',(event_id,)).fetchone()
    if not ev: return []
    subject=ev['subject']
    if table_name=='decisions':
        return [r['id'] for r in con.execute('SELECT id FROM decisions WHERE event_id=?',(event_id,))]
    if table_name=='subjects':
        return [r['id'] for r in con.execute('SELECT id FROM subjects WHERE id=?',(subject,))]
    if table_name=='allocations':
        rows=con.execute('SELECT id FROM allocations WHERE id=? OR subject=?',(event_id,subject)).fetchall()
        return [r['id'] for r in rows]
    # documents and receipts are keyed by the event id with material suffixes,
    # and return/checkin receipts are attached to the event's subject
    rows=con.execute(
        f'SELECT id FROM {table_name} WHERE id=? OR id LIKE ? OR subject=?',
        (event_id,event_id+'-%',subject)).fetchall()
    return [r['id'] for r in rows]


def stamp_event_projections(con, event_id):
    """Stamp every projection row attributed to ``event_id`` after a handler."""
    for table in PROJECTION_TABLES:
        for row_id in _projection_row_ids(con,table,event_id):
            row=con.execute(f'SELECT * FROM {table} WHERE id=?',(row_id,)).fetchone()
            if row is not None:
                stamp_projection(con,table,row,event_id)


def replace_projection_state(con, replay_con):
    """Atomically replace all projection tables with a replayed image.

    ``events`` and the source-ledger tables are untouched. The five domain
    projection tables plus ``projection_sources`` are swapped inside one
    transaction, so a reader can never observe a half-rebuilt database.
    """
    with con:
        for table in PROJECTION_TABLES:
            con.execute(f'DELETE FROM {table}')
            con.execute("DELETE FROM projection_sources WHERE table_name=?",(table,))
            cols=[r[1] for r in con.execute(f'PRAGMA table_info({table})').fetchall()]
            placeholders=','.join('?' for _ in cols)
            for row in replay_con.execute(f'SELECT {",".join(cols)} FROM {table}'):
                con.execute(f'INSERT INTO {table} VALUES({placeholders})',tuple(row))
        for ps in replay_con.execute('SELECT row_key,table_name,source_event_id,projection_version,row_digest FROM projection_sources'):
            con.execute('INSERT INTO projection_sources VALUES(?,?,?,?,?)',tuple(ps))


def insert_repair(con, repair_id, plan_digest, route, reason, applied_at, plan, result):
    con.execute('INSERT INTO repair_records VALUES(?,?,?,?,?,?,?,?)',
                (repair_id,plan_digest,route,reason,int(applied_at),int(applied_at),
                 json.dumps(plan,ensure_ascii=False,sort_keys=True),
                 json.dumps(result,ensure_ascii=False,sort_keys=True)))


def insert_quarantine(con, event_id, subsystem, payload, stored_digest,
                      actual_digest, reason, quarantined_at):
    con.execute('INSERT OR REPLACE INTO quarantined_events VALUES(?,?,?,?,?,?,?)',
                (event_id,subsystem,payload,stored_digest,actual_digest,reason,int(quarantined_at)))


def get_repair(con, plan_digest):
    row=con.execute('SELECT * FROM repair_records WHERE plan_digest=?',(plan_digest,)).fetchone()
    return dict(row) if row else None
