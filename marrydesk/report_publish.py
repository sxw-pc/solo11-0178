"""Versioned monthly report publishing with a crash-safe visibility boundary.

CSV details and JSON summaries are rendered per version into immutable
version-addressed files; a single SQLite transaction flips the current
pointer. Readers resolve the pointer first and then read that version's
artifacts, so the three independent writes (CSV, JSON, pointer) never expose
a mixed statistical cut even though they can never be one local transaction.

Recovery after power loss is authorized in exactly two directions:
- inputs still valid (captured batch bytes unchanged, not revoked): complete
  the interrupted version, re-staging any lost artifact from durable inputs;
- inputs superseded (e.g. source batch revoked): quarantine every fragment
  of the interrupted version and keep the previous current pointer.

Connections must use sqlite3.Row as row_factory.
"""
import hashlib, json, os, shutil
from pathlib import Path
from . import access_report, report_capture

SCHEMA="""
CREATE TABLE IF NOT EXISTS report_versions(month TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL, input_digest TEXT NOT NULL, input_batches TEXT NOT NULL, confirmed INTEGER NOT NULL, requested INTEGER NOT NULL, rate REAL, csv_digest TEXT, json_digest TEXT, csv_path TEXT, json_path TEXT, created_at INTEGER NOT NULL, committed_at INTEGER, diagnostic TEXT, PRIMARY KEY(month,version));
CREATE TABLE IF NOT EXISTS report_current(month TEXT PRIMARY KEY, version INTEGER NOT NULL);
"""
IN_FLIGHT=('staging','staged')

class PowerFailure(Exception):
    """Raised by the _crash test hook to simulate power loss mid-publish."""

def init(con):
    con.executescript(SCHEMA); report_capture.init(con)

def _sha(text): return hashlib.sha256(text.encode('utf-8')).hexdigest()
def _canonical(obj): return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def _fsync_dir(path):
    fd=os.open(path,os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)

def _write_atomic(path,text):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    with open(tmp,'w',encoding='utf-8') as fh:
        fh.write(text); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp,path); _fsync_dir(path.parent)

def render_csv(windows):
    lines=['window,requested,confirmed']+[f"{w['window']},{w['requested']},{w['confirmed']}" for w in windows]
    return '\n'.join(lines)+'\n'

def render_json(month,version,summary,sources,input_digest):
    return json.dumps({'month':month,'version':version,'confirmed':summary['confirmed'],
                       'requested':summary['requested'],'rate':summary['rate'],
                       'input_digest':input_digest,'sources':sources},
                      ensure_ascii=False,sort_keys=True,indent=2)+'\n'

def _windows(rows):
    acc={}
    for r in rows:
        w=acc.setdefault(r['office'],{'window':r['office'],'requested':0,'confirmed':0})
        w['requested']+=r['requested']; w['confirmed']+=r['confirmed']
    return [acc[k] for k in sorted(acc)]

def _validate_rows(rows):
    for r in rows:
        if not isinstance(r.get('requested'),int) or not isinstance(r.get('confirmed'),int): return 'row-not-integral'
        if r['requested']<0 or r['confirmed']<0: return 'row-negative'
        if r['confirmed']>r['requested']: return 'confirmed-exceeds-requested'
    return None

def _plan(con,month):
    batches=report_capture.effective(con,month)
    if not batches: return {'ok':False,'diagnostic':'no-input'}
    rows=[]
    for b in batches: rows.extend(json.loads(b['raw'])['rows'])
    sources=[{'id':b['id'],'window':b['window'],'revision':b['revision'],'digest':b['digest']} for b in batches]
    input_digest=_sha(_canonical(sources))
    problem=_validate_rows(rows)
    if problem: return {'ok':False,'diagnostic':problem,'sources':sources,'input_digest':input_digest}
    return {'ok':True,'rows':rows,'windows':_windows(rows),'summary':access_report.summary(rows),
            'sources':sources,'input_digest':input_digest}

def _next_version(con,month):
    return con.execute('SELECT COALESCE(MAX(version),0)+1 AS nxt FROM report_versions WHERE month=?',(month,)).fetchone()['nxt']

def _record(con,month,version,state,plan,now,csv_digest=None,json_digest=None,csv_path=None,json_path=None,diagnostic=None):
    summary=plan.get('summary') or {}
    with con:
        con.execute('INSERT INTO report_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (month,version,state,plan.get('input_digest',''),_canonical(plan.get('sources') or []),
                     summary.get('confirmed',0),summary.get('requested',0),summary.get('rate'),
                     csv_digest,json_digest,csv_path,json_path,now,None,diagnostic))

def _staging(store,month,version,ext): return Path(store)/'staging'/month/f'v{version}.{ext}'

def _artifact_text(store,row,ext):
    for cand in (Path(store)/row[f'{ext}_path'],_staging(store,row['month'],row['version'],ext)):
        if cand.exists(): return cand.read_text(encoding='utf-8')
    return None

def _check_artifacts(con,store,month,version):
    row=con.execute('SELECT * FROM report_versions WHERE month=? AND version=?',(month,version)).fetchone()
    problems=[]
    for ext in ('csv','json'):
        text=_artifact_text(store,row,ext)
        if text is None: problems.append(f'{ext}-missing')
        elif _sha(text)!=row[f'{ext}_digest']: problems.append(f'{ext}-corrupt')
    if problems: return False,','.join(problems)
    doc=json.loads(_artifact_text(store,row,'json'))
    if doc.get('input_digest')!=row['input_digest'] or doc.get('confirmed')!=row['confirmed'] or doc.get('requested')!=row['requested']:
        return False,'json-content-mismatch'
    return True,''

def _cleanup(store,month):
    """Drop staging residue; committed version artifacts are never cleanup targets."""
    shutil.rmtree(Path(store)/'staging'/month,ignore_errors=True)

def _commit(con,store,month,version,now,_crash=None):
    row=con.execute('SELECT * FROM report_versions WHERE month=? AND version=?',(month,version)).fetchone()
    for ext in ('csv','json'):
        staged=_staging(store,month,version,ext)
        if staged.exists():
            final=Path(store)/row[f'{ext}_path']; final.parent.mkdir(parents=True,exist_ok=True)
            os.replace(staged,final); _fsync_dir(final.parent)
        if _crash==f'after_{ext}_commit': raise PowerFailure(_crash)
    with con:
        con.execute("UPDATE report_versions SET state='committed',committed_at=? WHERE month=? AND version=?",(now,month,version))
        con.execute('INSERT OR REPLACE INTO report_current(month,version) VALUES(?,?)',(month,version))
    if _crash=='after_pointer': raise PowerFailure(_crash)
    _cleanup(store,month)

def _quarantine(con,store,month,version,diagnostic):
    store=Path(store); qdir=store/'quarantine'/month/f'v{version}'
    row=con.execute('SELECT * FROM report_versions WHERE month=? AND version=?',(month,version)).fetchone()
    cands=[_staging(store,month,version,'csv'),_staging(store,month,version,'json')]
    cands+=[store/row[f'{ext}_path'] for ext in ('csv','json') if row[f'{ext}_path']]
    for cand in cands:
        if cand.exists():
            qdir.mkdir(parents=True,exist_ok=True)
            shutil.move(str(cand),str(qdir/cand.name))
    with con:
        con.execute("UPDATE report_versions SET state='quarantined',diagnostic=? WHERE month=? AND version=?",(diagnostic,month,version))

def publish(con,store,month,now=0,_crash=None):
    """Publish the month's current effective input set as the next version."""
    init(con)
    recover(con,store,month,now=now)
    plan=_plan(con,month)
    if not plan['ok'] and plan['diagnostic']=='no-input': return {'outcome':'no-input','month':month}
    if not plan['ok']:
        version=_next_version(con,month)
        _record(con,month,version,'failed',plan,now,diagnostic=plan['diagnostic'])
        return {'outcome':'invalid','month':month,'version':version,'diagnostic':plan['diagnostic']}
    hit=con.execute("SELECT version,rate FROM report_versions WHERE month=? AND input_digest=? AND state='committed'",(month,plan['input_digest'])).fetchone()
    if hit: return {'outcome':'idempotent','month':month,'version':hit['version'],'rate':hit['rate']}
    version=_next_version(con,month)
    csv_text=render_csv(plan['windows'])
    json_text=render_json(month,version,plan['summary'],plan['sources'],plan['input_digest'])
    _record(con,month,version,'staging',plan,now,
            csv_digest=_sha(csv_text),json_digest=_sha(json_text),
            csv_path=f'versions/{month}/v{version}.csv',json_path=f'versions/{month}/v{version}.json')
    _write_atomic(_staging(store,month,version,'csv'),csv_text)
    if _crash=='after_csv_stage': raise PowerFailure(_crash)
    _write_atomic(_staging(store,month,version,'json'),json_text)
    with con: con.execute("UPDATE report_versions SET state='staged' WHERE month=? AND version=?",(month,version))
    if _crash=='after_stage': raise PowerFailure(_crash)
    ok,diag=_check_artifacts(con,store,month,version)
    if not ok:
        _quarantine(con,store,month,version,diag)
        return {'outcome':'invalid','month':month,'version':version,'diagnostic':diag}
    _commit(con,store,month,version,now,_crash=_crash)
    return {'outcome':'committed','month':month,'version':version,
            'confirmed':plan['summary']['confirmed'],'requested':plan['summary']['requested'],'rate':plan['summary']['rate']}

def _restage(con,store,month,row,plan):
    version=row['version']
    texts={'csv':render_csv(plan['windows']),'json':render_json(month,version,plan['summary'],plan['sources'],plan['input_digest'])}
    for ext,text in texts.items():
        if _sha(text)!=row[f'{ext}_digest']: continue
        existing=_artifact_text(store,row,ext)
        if existing is None or _sha(existing)!=row[f'{ext}_digest']:
            _write_atomic(_staging(store,month,version,ext),text)

def _recover_one(con,store,month,row,now):
    version=row['version']
    plan=_plan(con,month)
    inputs_current=plan.get('ok') and plan.get('input_digest')==row['input_digest']
    ok,diag=_check_artifacts(con,store,month,version)
    if inputs_current:
        if not ok:
            _restage(con,store,month,row,plan)
            ok,diag=_check_artifacts(con,store,month,version)
            if not ok:
                _quarantine(con,store,month,version,f'restage-failed:{diag}')
                return {'outcome':'rolled-back','version':version,'diagnostic':diag}
        _commit(con,store,month,version,now)
        return {'outcome':'completed','version':version}
    revoked=[b['id'] for b in json.loads(row['input_batches']) if (report_capture.get(con,b['id']) or {}).get('revoked')]
    diagnostic=f"inputs-superseded revoked={revoked} artifacts={diag or 'ok'}"
    _quarantine(con,store,month,version,diagnostic)
    return {'outcome':'rolled-back','version':version,'diagnostic':diagnostic}

def recover(con,store,month,now=0):
    """Settle interrupted versions for the month after a restart."""
    init(con)
    return [_recover_one(con,Path(store),month,dict(row),now)
            for row in con.execute("SELECT * FROM report_versions WHERE month=? AND state IN ('staging','staged') ORDER BY version",(month,)).fetchall()]

def current_version(con,month):
    init(con)
    row=con.execute('SELECT version FROM report_current WHERE month=?',(month,)).fetchone()
    return row['version'] if row else None

def read_version(con,store,month,version):
    """Re-read one committed version's artifacts; old versions stay readable."""
    init(con); store=Path(store)
    row=con.execute("SELECT * FROM report_versions WHERE month=? AND version=? AND state='committed'",(month,version)).fetchone()
    if row is None: return None
    csv_text=(store/row['csv_path']).read_text(encoding='utf-8')
    json_text=(store/row['json_path']).read_text(encoding='utf-8')
    if _sha(csv_text)!=row['csv_digest'] or _sha(json_text)!=row['json_digest']: raise ValueError('artifact-corrupt')
    return {'month':month,'version':version,'csv':csv_text,'json':json.loads(json_text),
            'confirmed':row['confirmed'],'requested':row['requested'],'rate':row['rate']}

def read_current(con,store,month):
    version=current_version(con,month)
    return read_version(con,store,month,version) if version is not None else None

def versions(con,month):
    init(con)
    return [dict(r) for r in con.execute('SELECT * FROM report_versions WHERE month=? ORDER BY version',(month,))]
