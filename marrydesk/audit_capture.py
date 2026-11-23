"""Provenance-grade audit, root-cause localization and conditional recovery.

Protocol (the deliverable is provable root cause plus an immutable recovery
plan, never a silent rewrite):

1. **Source verification** - every ``events.payload`` is re-serialized and
   compared byte-for-byte against the stored SHA-256 digest. A mismatch means
   the source itself is not trustworthy; replay of an untrusted payload is
   simulation only and can never be written back.
2. **Trusted-prefix replay** - only events whose bytes are proven are replayed
   into an in-memory ledger. The first event that cannot be proven is the
   *first provable fork*; every decision/entity/receipt derived from it or
   later events is reported as the causal impact chain.
3. **Projection divergence** - when all sources verify, projection rows are
   compared against the trusted replay, down to the exact row key and its
   stamped source event id (projection-only corruption, e.g. fault 甲).
4. **Conditional recovery** - plans are immutable (same input -> same plan
   digest). They are only *simulated* until an operator presents credentials
   (an independently sealed export package for restore, or explicit
   authorization for quarantine). Application happens in one transaction so
   a crash can never leave ``events`` swapped while ``receipts`` are stale.
"""
import json, sqlite3, hashlib, time, zipfile, io
from pathlib import Path
from . import storage

PROJECTION_TABLES=storage.PROJECTION_TABLES
ROW_ID_KEY={t:'id' for t in PROJECTION_TABLES}
# decisions rows carry an autoincrement rowid; business identity is event_id.
COMPARE_EXCLUDE={'decisions':{'id'}}


# ------------------------------------------------------------- intake seals --

def seal_manifest(entries):
    """Seal a manifest of {name, sha256, bytes-length} entries into one digest."""
    canonical=sorted((e['name'],e['sha256'],int(e.get('bytes',0))) for e in entries)
    return storage.digest_of(canonical)


def _entry_digest(raw):
    return hashlib.sha256(storage.canonical(raw)).hexdigest()


def build_export_package(out_dir, events, batch_id, now=None):
    """Write a sealed raw-event export package (one canonical JSON file/event).

    Layout::

        out_dir/manifest.json   {batch_id, algorithm, entries:[{name,sha256,bytes}]}
        out_dir/events/<id>.json
    """
    now=int(time.time()) if now is None else int(now)
    out=Path(out_dir); (out/'events').mkdir(parents=True,exist_ok=True)
    entries=[]; subsystems={}; seq={}
    for i,ev in enumerate(events):
        # subsystem is manifest metadata; it must never alter event bytes
        source=dict(ev); subsystem=source.pop('_subsystem','')
        raw=storage.canonical(source)
        name=f'events/{ev["id"]}.json'
        (out/'events'/f'{ev["id"]}.json').write_bytes(raw)
        entries.append({'name':name,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)})
        subsystems[ev['id']]=subsystem
        seq[ev['id']]=i+1
    manifest={'batch_id':batch_id,'algorithm':'sha256','sealed_at':now,
              'subsystems':subsystems,'seq':seq,'entries':entries}
    manifest['sealed_digest']=seal_manifest(entries)
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')
    return {'path':str(out),'batch_id':batch_id,'sealed_digest':manifest['sealed_digest'],'events':len(entries)}


def build_backup_sheet(path, rows, batch_id, db_snapshot_digest=None, now=None):
    """Write a database backup verification sheet.

    ``rows`` is a list of capture records for individual events; a sheet-level
    ``db_snapshot_digest`` (e.g. a SHA over the backup file) may be attached.
    """
    now=int(time.time()) if now is None else int(now)
    entries=[{'name':f'event:{r["event_id"]}','sha256':r['raw_digest'],'bytes':int(r.get('bytes',0))} for r in rows]
    sheet={'batch_id':batch_id,'kind':'backup-sheet','algorithm':'sha256',
           'sealed_at':now,'db_snapshot_digest':db_snapshot_digest,'rows':rows}
    sheet['sealed_digest']=seal_manifest(entries)
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(sheet,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')
    return {'path':str(p),'batch_id':batch_id,'sealed_digest':sheet['sealed_digest']}


def build_repair_record(path, repair, now=None):
    """Serialize an immutable manual-repair record artifact."""
    now=int(time.time()) if now is None else int(now)
    doc=repair if isinstance(repair,dict) else json.loads(repair)
    doc=dict(doc); doc.setdefault('recorded_at',now)
    raw=storage.canonical(doc)
    doc['record_digest']=hashlib.sha256(raw).hexdigest()
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(doc,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')
    return {'path':str(p),'sealed_digest':doc['record_digest']}


def _load_manifest_artifact(artifact_path):
    p=Path(artifact_path)
    if p.is_dir():
        manifest=json.loads((p/'manifest.json').read_text(encoding='utf-8'))
        files={e['name']:(p/e['name']).read_bytes() for e in manifest['entries']}
        return manifest, files
    if zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as zf:
            manifest=json.loads(zf.read('manifest.json').decode('utf-8'))
            files={e['name']:zf.read(e['name']) for e in manifest['entries']}
        return manifest, files
    # single JSON document: backup sheet or repair record
    doc=json.loads(p.read_text(encoding='utf-8'))
    return doc, None


def ingest_capture(con, artifact_path, kind=None, batch_id=None, trusted=True,
                   now=None, detail=None):
    """Bulk intake a backup sheet / export package / manual repair record.

    Every per-event byte digest is re-verified here; the whole artifact is
    rejected (``ValueError``) if any entry fails its own SHA-256 or the
    manifest seal does not match. Only after verification are capture batch
    and ``event_sources`` rows written, in the caller's transaction.
    """
    now=int(time.time()) if now is None else int(now)
    manifest, files=_load_manifest_artifact(artifact_path)
    kind=kind or manifest.get('kind') or ('export-package' if files is not None else 'capture')
    batch_id=batch_id or manifest['batch_id']
    sealed=manifest['sealed_digest']

    records=[]
    if kind=='backup-sheet' and files is None:
        entries=[{'name':f'event:{r["event_id"]}','sha256':r['raw_digest'],'bytes':int(r.get('bytes',0))} for r in manifest['rows']]
        if seal_manifest(entries)!=sealed: raise ValueError('seal-mismatch')
        for r in manifest['rows']:
            records.append({'event_id':r['event_id'],'subsystem':r.get('subsystem',''),
                            'seq':int(r.get('seq',0)),'effective_at':int(r.get('effective_at',0)),
                            'received_at':int(r.get('received_at',0)),'raw_digest':r['raw_digest'],
                            'anchored':bool(r.get('anchored',False))})
    else:
        entries=manifest['entries']
        if seal_manifest(entries)!=sealed: raise ValueError('seal-mismatch')
        for e in entries:
            raw=files[e['name']]
            if hashlib.sha256(raw).hexdigest()!=e['sha256']: raise ValueError('entry-digest-mismatch')
            ev=json.loads(raw.decode('utf-8'))
            records.append({'event_id':ev['id'],'subsystem':manifest.get('subsystems',{}).get(ev['id'],''),
                            'seq':int(manifest.get('seq',{}).get(ev['id'],0)),
                            'effective_at':int(ev.get('effective_at',0)),
                            'received_at':int(ev.get('received_at',0)),
                            'raw_digest':e['sha256'],'event':ev,'anchored':True})

    storage.capture_batch(con,batch_id,kind,str(artifact_path),sealed,now,trusted,detail)
    for r in records:
        storage.note_event_source(con,r['event_id'],r['seq'],r['effective_at'],
                                  r['received_at'],r['subsystem'],r['raw_digest'],
                                  batch_id=batch_id,trusted='trusted' if trusted else 'untrusted',
                                  anchored=r['anchored'],detail={'ingested_via':kind})
    return {'batch_id':batch_id,'kind':kind,'sealed_digest':sealed,'events':len(records),'trusted':bool(trusted)}


# ----------------------------------------------------------- source checks --

def verify_event_bytes(con):
    """Byte-level check that each stored payload matches its stored digest."""
    out=[]
    for row in con.execute('SELECT id,subsystem,payload,digest,effective_at,received_at FROM events ORDER BY rowid'):
        actual=hashlib.sha256(row['payload'].encode('utf-8')).hexdigest()
        out.append({'event_id':row['id'],'subsystem':row['subsystem'],
                    'stored_digest':row['digest'],'actual_digest':actual,
                    'byte_match':actual==row['digest'],
                    'effective_at':row['effective_at'],'received_at':row['received_at']})
    return out


def find_credentials(con, event_id):
    """Return independently sealed evidence available for an event id."""
    out=[]
    for src in con.execute('SELECT * FROM event_sources WHERE event_id=?',(event_id,)):
        batches=[]
        if src['batch_id']:
            for b in con.execute('SELECT * FROM capture_batches WHERE batch_id=?',(src['batch_id'],)):
                batches.append({'batch_id':b['batch_id'],'kind':b['kind'],
                                'source_path':b['source_path'],'sealed_digest':b['sealed_digest'],
                                'trusted':bool(b['trusted'])})
        out.append({'event_id':event_id,'raw_digest':src['raw_digest'],
                    'trusted':storage.TRUST_NAMES[src['trusted']],'anchored':bool(src['anchored']),
                    'batch_id':src['batch_id'],'batches':batches})
    return out


def restore_credential_for(con, event_id, canonical_bytes):
    """A credential is a trusted sealed batch whose digest matches the bytes."""
    want=hashlib.sha256(storage.canonical(canonical_bytes)).hexdigest()
    for c in find_credentials(con,event_id):
        if c['raw_digest']==want and c['trusted']=='trusted' and c['anchored']:
            for b in c['batches']:
                if b['trusted']:
                    return {'event_id':event_id,'raw_digest':want,
                            'batch_id':b['batch_id'],'kind':b['kind'],
                            'source_path':b['source_path'],'sealed_digest':b['sealed_digest']}
    return None


# ------------------------------------------------------------------ replay --

def replay_events(con, events_by_id=None, overrides=None):
    """Replay (a subset of) stored events into a fresh in-memory database.

    ``overrides`` maps event id -> replacement event dict; this is strictly
    for *simulating* a restore candidate and its downstream effects.
    """
    from . import cli  # local import: cli imports storage only, no cycle at import time
    events_by_id=events_by_id if events_by_id is not None else None
    overrides=overrides or {}
    mem=storage.connect(':memory:')
    rows=con.execute('SELECT id,subsystem,payload FROM events ORDER BY rowid').fetchall()
    for row in rows:
        if events_by_id is not None and row['id'] not in events_by_id: continue
        ev=overrides.get(row['id']) or json.loads(row['payload'])
        cli.run_event(mem,row['subsystem'],ev)
    return mem


def _row_identity(table, row):
    data=dict(row)
    for k in COMPARE_EXCLUDE.get(table,()): data.pop(k,None)
    return data


def table_rows(con, table):
    if table=='decisions':
        return [_row_identity('decisions',r) for r in con.execute('SELECT * FROM decisions ORDER BY id')]
    return [dict(r) for r in con.execute(f'SELECT * FROM {table} ORDER BY id')]


def diff_tables(live_con, replay_con):
    """Row-level diff returning exact row keys, not just table names."""
    diffs={}
    for table in PROJECTION_TABLES:
        live={r.get('event_id',r.get('id')):r for r in table_rows(live_con,table)}
        expected={r.get('event_id',r.get('id')):r for r in table_rows(replay_con,table)}
        rows=[]
        for key in sorted(set(live)|set(expected)):
            if live.get(key)!=expected.get(key):
                rows.append({'row_id':key,'table':table,
                             'live':live.get(key),'expected':expected.get(key)})
        if rows: diffs[table]=rows
    return diffs


def _stamped_row_digests(con):
    return {r['row_key']:r for r in con.execute('SELECT * FROM projection_sources')}


def verify_projection_stamps(con):
    """Check each stamped row digest against the row bytes currently stored."""
    bad=[]
    stamps=_stamped_row_digests(con)
    for key,st in stamps.items():
        row=con.execute('SELECT * FROM %s WHERE id=?' % st['table_name'],
                        (key.split(':',1)[1],)).fetchone()
        if row is None:
            bad.append({'row_key':key,'table':st['table_name'],'problem':'row-missing'}); continue
        actual=storage.digest_of({k:row[k] for k in row.keys()})
        if actual!=st['row_digest']:
            bad.append({'row_key':key,'table':st['table_name'],'source_event_id':st['source_event_id'],
                        'stored_row_digest':st['row_digest'],'actual_row_digest':actual,
                        'problem':'row-digest-mismatch'})
    return bad


# ------------------------------------------------------------------- audit --

def _source_impact(con, fork_event_id):
    """Causal impact chain: every decision/entity/receipt at or after the fork."""
    evs=con.execute('SELECT id,rowid,subject FROM events ORDER BY rowid').fetchall()
    fork_seq=next(r['rowid'] for r in evs if r['id']==fork_event_id)
    downstream=[r['id'] for r in evs if r['rowid']>=fork_seq]
    affected_decisions=[dict(r) for r in con.execute(
        'SELECT event_id,subsystem,subject,outcome,reason,state FROM decisions WHERE event_id IN (%s) ORDER BY id'
        % ','.join('?'*len(downstream)),downstream)] if downstream else []
    subjects={r['subject'] for r in con.execute(
        'SELECT DISTINCT subject FROM events WHERE id IN (%s)' % ','.join('?'*len(downstream)),downstream)} if downstream else set()
    affected_subjects=[dict(r) for r in con.execute('SELECT * FROM subjects WHERE id IN (%s) ORDER BY id'
                       % ','.join('?'*len(subjects)),tuple(subjects))] if subjects else []
    affected_receipts=[dict(r) for r in con.execute(
        'SELECT * FROM receipts WHERE subject IN (%s) ORDER BY id' % ','.join('?'*len(subjects)),
        tuple(subjects))] if subjects else []
    return {'fork_event_id':fork_event_id,'downstream_event_ids':downstream,
            'affected_decisions':affected_decisions,'affected_subjects':affected_subjects,
            'affected_receipts':affected_receipts}


def audit_evidence(con, now=None):
    """Full provenance audit. See module docstring for the four-step protocol."""
    now=int(time.time()) if now is None else int(now)
    checks=verify_event_bytes(con)
    untrusted=[c for c in checks if not c['byte_match']]
    first_untrusted=untrusted[0]['event_id'] if untrusted else None

    if first_untrusted:
        # replay only the provable prefix; never build state from bad bytes
        trusted_ids={c['event_id'] for c in checks if c['byte_match']}
        mem=replay_events(con,events_by_id=trusted_ids) if trusted_ids else storage.connect(':memory:')
        try:
            fork=next(c for c in checks if c['event_id']==first_untrusted)
            impact=_source_impact(con,first_untrusted)
            # what the tampered bytes *would* produce - simulation, labeled untrusted
            sim_mem=replay_events(con)
            try:
                sim_diffs=diff_tables(con,sim_mem)
                classification='source-untrusted'
                evidence={'first_untrusted_event':{
                            'event_id':fork['event_id'],'subsystem':fork['subsystem'],
                            'stored_digest':fork['stored_digest'],'actual_digest':fork['actual_digest'],
                            'reason':'payload-digest-mismatch'},
                          'causal_impact':impact,
                          'tampered_replay_simulation':{'mismatches':sorted(sim_diffs),
                            'note':'derived from untrusted payload; never a recovery target'},
                          'credentials':find_credentials(con,first_untrusted)}
                plans=_source_recovery_plans(con,checks,first_untrusted,impact,now)
            finally:
                sim_mem.close()
        finally:
            mem.close()
    else:
        mem=replay_events(con)
        try:
            diffs=diff_tables(con,mem)
            stamp_failures=verify_projection_stamps(con)
            classification='projection-only' if (diffs or stamp_failures) else 'consistent'
            evidence={'projection_diffs':diffs,'stamp_failures':stamp_failures,
                      'first_divergent_row':_first_divergent_row(diffs,stamp_failures)}
            plans=_projection_recovery_plans(con,mem,diffs,stamp_failures,now) if classification=='projection-only' else []
        finally:
            mem.close()

    mismatches=sorted(set(_table_level_mismatches(con,checks)))
    return {'ok':classification=='consistent','classification':classification,
            'replayed_events':len(checks),'mismatches':mismatches,
            'source_checks':checks,'evidence':evidence,'recovery_plans':plans}


def _first_divergent_row(diffs, stamp_failures):
    if diffs:
        table=next(iter(diffs)); return {'table':table,'row_id':diffs[table][0]['row_id'],
                                         'source_event_id':diffs[table][0]['row_id']}
    if stamp_failures:
        s=stamp_failures[0]; return {'table':s['table'],'row_key':s['row_key'],
                                     'source_event_id':s.get('source_event_id')}
    return None


def _table_level_mismatches(con, checks):
    """Reproduce the legacy table-name list so old callers keep working."""
    if any(not c['byte_match'] for c in checks):
        # legacy behavior: replay stored payloads verbatim and compare reports
        mem=replay_events(con)
        actual=storage.report(con); expected=storage.report(mem); mem.close()
        return [k for k in ('events','decisions','subjects','allocations','receipts')
                if actual[k]!=expected[k]]
    mem=replay_events(con)
    diffs=diff_tables(con,mem); mem.close()
    return list(diffs)


# --------------------------------------------------------- recovery plans --

def _plan_id(route, event_id, digest):
    return storage.digest_of({'route':route,'event_id':event_id,'plan_digest':digest})[:16]


def _projection_recovery_plans(con, replay_con, diffs, stamp_failures, now):
    target_rows={}
    for table, rows in diffs.items():
        for r in rows: target_rows[(table,str(r['row_id']))]={'table':table,'row_id':r['row_id']}
    for s in stamp_failures:
        table=s['table']; row_id=s['row_key'].split(':',1)[1]
        target_rows.setdefault((table,row_id),{'table':table,'row_id':row_id})
    expected=storage.report(replay_con)
    plan={'route':'rebuild-from-trusted-events','classification':'projection-only',
          'target_rows':[target_rows[k] for k in sorted(target_rows)],
          'expected_summary':_summary_from_report(expected)}
    plan_digest=storage.digest_of(plan)
    return [{'plan_id':_plan_id(plan['route'],'*',plan_digest),'route':plan['route'],
             'plan_digest':plan_digest,'immutable':True,'requires':'operator-selection',
             'plan':plan,'simulation':{'expected_summary':plan['expected_summary']}}]


def _summary_from_report(report):
    return {'events':len(report['events']),
            'decisions':[(d['event_id'],d['outcome'],d['state']) for d in report['decisions']],
            'subjects':[(s['id'],s['state'],s['version']) for s in report['subjects']],
            'allocations':[(a['id'],a['state']) for a in report['allocations']],
            'receipts':[(r['id'],r['kind'],r['state']) for r in report['receipts']]}


def _source_recovery_plans(con, checks, fork_event_id, impact, now):
    bad=next(c for c in checks if c['event_id']==fork_event_id)
    plans=[]
    # Route 1: restore from an independently sealed export package
    restore_candidates=[]
    for c in find_credentials(con,fork_event_id):
        if c['trusted']=='trusted' and c['anchored']:
            for b in c['batches']:
                if b['trusted']:
                    restore_candidates.append({'batch_id':b['batch_id'],'kind':b['kind'],
                                               'source_path':b['source_path'],
                                               'sealed_digest':b['sealed_digest'],
                                               'raw_digest':c['raw_digest']})
    restore_plan={'route':'restore-sealed-source','classification':'source-untrusted',
                  'fork_event_id':fork_event_id,
                  'stored_tampered_digest':bad['actual_digest'],
                  'expected_source_digest':bad['stored_digest'],
                  'credentials':restore_candidates,
                  'downstream_event_ids':impact['downstream_event_ids'],
                  'expected_summary':None}
    if restore_candidates:
        # simulate the restore to attach a concrete post-recovery image
        sim=_simulate_restore(con,fork_event_id,restore_candidates[0])
        if sim:
            restore_plan['expected_summary']=sim['summary']
            restore_plan['simulation']={'replayed_events':sim['replayed_events'],
                                        'mismatches_after_restore':sim['mismatches']}
    restore_plan['available']=bool(restore_candidates)
    rp_digest=storage.digest_of(restore_plan)
    plans.append({'plan_id':_plan_id('restore-sealed-source',fork_event_id,rp_digest),
                  'route':'restore-sealed-source','plan_digest':rp_digest,'immutable':True,
                  'requires':'sealed-export-credential','available':bool(restore_candidates),
                  'plan':restore_plan,
                  'simulation':restore_plan.get('simulation')})

    # Route 2: quarantine the untrusted event; dependents re-project blocked
    qsim=_simulate_quarantine(con,fork_event_id)
    quarantine_plan={'route':'quarantine-untrusted-source','classification':'source-untrusted',
                     'fork_event_id':fork_event_id,
                     'stored_tampered_digest':bad['actual_digest'],
                     'quarantined_event_ids':qsim['quarantined_event_ids'],
                     'downstream_event_ids':impact['downstream_event_ids'],
                     'expected_summary':qsim['summary'],
                     'manual_review':qsim['manual_review']}
    qp_digest=storage.digest_of(quarantine_plan)
    plans.append({'plan_id':_plan_id('quarantine-untrusted-source',fork_event_id,qp_digest),
                  'route':'quarantine-untrusted-source','plan_digest':qp_digest,'immutable':True,
                  'requires':'explicit-operator-authorization','available':True,
                  'plan':quarantine_plan,
                  'simulation':{'replayed_events':qsim['replayed_events'],
                                'mismatches_after_quarantine':qsim['mismatches'],
                                'manual_review':qsim['manual_review']}})
    return plans


def _simulate_restore(con, fork_event_id, credential):
    """Load sealed bytes for the fork event and replay the whole chain."""
    p=Path(credential['source_path'])
    try:
        manifest, files=_load_manifest_artifact(p)
    except (OSError,KeyError,json.JSONDecodeError):
        return None
    raw=None
    for e in manifest['entries']:
        if e['name']==f'events/{fork_event_id}.json':
            raw=files[e['name']]
            if hashlib.sha256(raw).hexdigest()!=credential['raw_digest']: return None
    if raw is None: return None
    restored=json.loads(raw.decode('utf-8'))
    mem=replay_events(con,overrides={fork_event_id:restored})
    diffs=diff_tables(con,mem)
    summary=_summary_from_report(storage.report(mem))
    replayed=mem.execute('SELECT COUNT(*) n FROM events').fetchone()['n']
    mem.close()
    return {'summary':summary,'replayed_events':replayed,'mismatches':sorted(diffs)}


def _simulate_quarantine(con, fork_event_id):
    """Replay without the untrusted event; dependents must re-project blocked."""
    keep={r['id'] for r in con.execute('SELECT id FROM events') if r['id']!=fork_event_id}
    mem=replay_events(con,events_by_id=keep)
    diffs=diff_tables(con,mem)
    summary=_summary_from_report(storage.report(mem))
    replayed=mem.execute('SELECT COUNT(*) n FROM events').fetchone()['n']
    manual=[{'event_id':fork_event_id,'reason':'untrusted-source-quarantined'}]
    for r in mem.execute('SELECT event_id,outcome,reason FROM decisions WHERE outcome=?',('blocked',)):
        manual.append({'event_id':r['event_id'],'reason':r['reason']})
    mem.close()
    return {'summary':summary,'replayed_events':replayed,'mismatches':sorted(diffs),
            'quarantined_event_ids':[fork_event_id],'manual_review':manual}


# ----------------------------------------------------------- apply recovery --

class RecoveryError(RuntimeError): pass


def apply_recovery(con, plan_digest, authorization=None, now=None):
    """Apply a previously produced immutable plan, only with credentials.

    The whole switch happens in one SQLite transaction; an exception/crash
    before COMMIT rolls every table back to the previously visible state, so
    no half-repaired database (new events, old receipts) is ever observable.
    Re-applying the same plan digest is idempotent.
    """
    now=int(time.time()) if now is None else int(now)
    prior=storage.get_repair(con,plan_digest)
    if prior:
        return {'repair_id':prior['repair_id'],'route':prior['route'],
                'idempotent':True,'result':json.loads(prior['result'])}

    evidence=audit_evidence(con,now=now)
    selected=next((p for p in evidence['recovery_plans'] if p['plan_digest']==plan_digest),None)
    if selected is None: raise RecoveryError('unknown-or-stale-plan-digest')
    route=selected['route']; plan=selected['plan']

    with con:
        if route=='rebuild-from-trusted-events':
            return _apply_rebuild(con,selected,now)
        if route=='restore-sealed-source':
            return _apply_restore(con,selected,authorization,now)
        if route=='quarantine-untrusted-source':
            return _apply_quarantine(con,selected,authorization,now)
        raise RecoveryError('unknown-route')


def _apply_rebuild(con, selected, now):
    """Fault 甲: sources all verify, rebuild projections from trusted events."""
    if any(not c['byte_match'] for c in verify_event_bytes(con)):
        raise RecoveryError('refuse-rebuild: source bytes no longer verify')
    mem=replay_events(con)
    diffs=diff_tables(con,mem)
    stamp_failures=verify_projection_stamps(con)
    if not diffs and not stamp_failures:
        mem.close(); raise RecoveryError('plan-stale: nothing to repair')
    expected=selected['plan']['expected_summary']
    if _summary_from_report(storage.report(mem))!=expected:
        mem.close(); raise RecoveryError('simulation-drift: plan no longer matches database')
    storage.replace_projection_state(con,mem)
    result={'route':'rebuild-from-trusted-events','repaired_rows':selected['plan']['target_rows'],
            'post_summary':expected}
    repair_id=selected['plan_id']
    storage.insert_repair(con,repair_id,selected['plan_digest'],selected['route'],
                          'projection-only corruption rebuilt from verified source events',
                          now,selected['plan'],result)
    mem.close()
    return {'repair_id':repair_id,'route':selected['route'],'idempotent':False,'result':result}


def _apply_restore(con, selected, authorization, now):
    """Fault 乙 route 1: restore sealed v1 bytes, replay, keep tamper evidence."""
    plan=selected['plan']; fork=plan['fork_event_id']
    if not authorization or authorization.get('credential',{}).get('batch_id') not in \
            {c['batch_id'] for c in plan['credentials']}:
        raise RecoveryError('restore requires a sealed export package credential')
    cred=authorization['credential']
    manifest, files=_load_manifest_artifact(cred['source_path'])
    if seal_manifest(manifest['entries'])!=manifest['sealed_digest']:
        raise RecoveryError('credential seal failed')
    raw=next((files[e['name']] for e in manifest['entries']
              if e['name']==f'events/{fork}.json'),None)
    if raw is None or hashlib.sha256(raw).hexdigest()!=cred['raw_digest']:
        raise RecoveryError('credential digest does not anchor restored event')
    restored=json.loads(raw.decode('utf-8'))
    if storage.digest_of(restored)!=plan['expected_source_digest']:
        raise RecoveryError('restored bytes do not match the digest recorded at intake')

    # preserve the tampered bytes before they are overwritten
    bad_row=con.execute('SELECT * FROM events WHERE id=?',(fork,)).fetchone()
    preserved={'event_id':fork,'tampered_payload':bad_row['payload'],
               'stored_digest':bad_row['digest'],'actual_digest':
               hashlib.sha256(bad_row['payload'].encode('utf-8')).hexdigest()}

    mem=replay_events(con,overrides={fork:restored})
    if _summary_from_report(storage.report(mem))!=plan['expected_summary']:
        mem.close(); raise RecoveryError('simulation-drift: plan no longer matches database')

    canonical_raw=storage.canonical(restored)
    con.execute('UPDATE events SET payload=?, effective_at=?, received_at=?, digest=? WHERE id=?',
                (canonical_raw.decode('utf-8'),int(restored.get('effective_at',0)),
                 int(restored.get('received_at',0)),plan['expected_source_digest'],fork))
    storage.replace_projection_state(con,mem)
    src_row=con.execute('SELECT seq FROM event_sources WHERE event_id=?',(fork,)).fetchone()
    storage.note_event_source(con,fork, src_row['seq'] if src_row else 0,
        int(restored.get('effective_at',0)),int(restored.get('received_at',0)),
        bad_row['subsystem'],plan['expected_source_digest'],
        batch_id=cred['batch_id'],trusted='trusted',anchored=True,
        detail={'restored_from':cred['batch_id']})
    result={'route':'restore-sealed-source','restored_event':fork,
            'preserved_tampered_bytes':preserved,'post_summary':plan['expected_summary'],
            'credential_batch':cred['batch_id']}
    storage.insert_repair(con,selected['plan_id'],selected['plan_digest'],selected['route'],
                          'untrusted source replaced with independently sealed original; tampered bytes retained',
                          now,selected['plan'],result)
    mem.close()
    return {'repair_id':selected['plan_id'],'route':selected['route'],'idempotent':False,'result':result}


def _apply_quarantine(con, selected, authorization, now):
    """Fault 乙 route 2: isolate v1; v2 re-projects to blocked/manual review."""
    plan=selected['plan']; fork=plan['fork_event_id']
    if not authorization or authorization.get('decision')!='quarantine-untrusted-source':
        raise RecoveryError('quarantine requires explicit operator authorization')
    bad_row=con.execute('SELECT * FROM events WHERE id=?',(fork,)).fetchone()
    actual=hashlib.sha256(bad_row['payload'].encode('utf-8')).hexdigest()
    if actual==bad_row['digest']:
        raise RecoveryError('plan-stale: event bytes now verify, quarantine unnecessary')

    keep={r['id'] for r in con.execute('SELECT id FROM events') if r['id']!=fork}
    mem=replay_events(con,events_by_id=keep)
    if _summary_from_report(storage.report(mem))!=plan['expected_summary']:
        mem.close(); raise RecoveryError('simulation-drift: plan no longer matches database')

    storage.insert_quarantine(con,fork,bad_row['subsystem'],bad_row['payload'],
                              bad_row['digest'],actual,'payload-digest-mismatch',now)
    con.execute('DELETE FROM events WHERE id=?',(fork,))
    storage.replace_projection_state(con,mem)
    src=con.execute('SELECT * FROM event_sources WHERE event_id=?',(fork,)).fetchone()
    if src:
        con.execute("UPDATE event_sources SET trusted=0, anchored=0, detail=? WHERE event_id=?",
                    (json.dumps({'quarantined':True,'reason':'payload-digest-mismatch'},
                                ensure_ascii=False,sort_keys=True),fork))
    result={'route':'quarantine-untrusted-source','quarantined_event':fork,
            'manual_review':plan['manual_review'],'post_summary':plan['expected_summary']}
    storage.insert_repair(con,selected['plan_id'],selected['plan_digest'],selected['route'],
                          'untrusted source isolated; dependent events re-projected blocked pending review',
                          now,selected['plan'],result)
    mem.close()
    return {'repair_id':selected['plan_id'],'route':selected['route'],'idempotent':False,'result':result}
