"""Provable root-cause audit and conditional recovery tests.

Covers the two corruption families built from fixtures/audit-corruption.json:

* 故障甲 projection-only: receipts v2 row mutated, event bytes still match
  their digests -> exact row localization, rebuild from trusted events.
* 故障乙 source-untrusted: v1 payload tampered while its stored digest is
  stale -> first provable fork + causal chain, two authorized routes
  (sealed restore / quarantine), refusal to trust the bad payload,
  crash atomicity and idempotent retries.
"""
import json, tempfile, unittest
from pathlib import Path
from marrydesk import cli, storage, audit_capture as ac

FIXTURE=Path(__file__).resolve().parent.parent/'fixtures'/'audit-corruption.json'
VISIT_NORMAL=Path(__file__).resolve().parent.parent/'fixtures'/'visit_normal.json'


def load_fixture_events(path=FIXTURE):
    return [(r['subsystem'], r['event']) for r in json.loads(Path(path).read_text(encoding='utf-8'))]


def build_db(tmp, name='audit.db'):
    con=storage.connect(Path(tmp)/name)
    for subsystem, event in load_fixture_events():
        cli.run_event(con, subsystem, event)
    return con


def tamper_v1_past_deadline(con):
    """Fault 乙: rewrite v1 payload so effective_at is after deadline=10."""
    row=con.execute("SELECT payload FROM events WHERE id='v1'").fetchone()
    event=json.loads(row['payload'])
    event['effective_at']=11
    raw=storage.canonical(event).decode('utf-8')
    con.execute("UPDATE events SET payload=?, effective_at=11 WHERE id='v1'", (raw,))
    con.commit()
    return raw


def sealed_package(tmp, con, batch_id='sealed-export-1'):
    events=[]
    for r in con.execute('SELECT payload, subsystem FROM events ORDER BY rowid'):
        event=json.loads(r['payload'])
        event['_subsystem']=r['subsystem']
        events.append(event)
    path=Path(tmp)/'export-package'
    return path, ac.build_export_package(path, events, batch_id=batch_id, now=500)


class CrashBeforeCommit:
    """Connection proxy: the next write after arming raises before COMMIT.

    The exception propagates through apply_recovery's ``with con:`` block,
    which rolls the whole SQLite transaction back, exactly like power loss
    before the durability point.
    """
    def __init__(self, real):
        object.__setattr__(self, '_real', real)
        object.__setattr__(self, '_armed', False)
    def arm(self): object.__setattr__(self, '_armed', True)
    def __getattr__(self, key): return getattr(object.__getattribute__(self, '_real'), key)
    def __setattr__(self, key, value): setattr(object.__getattribute__(self, '_real'), key, value)
    def __enter__(self): return self
    def __exit__(self, *args): return object.__getattribute__(self, '_real').__exit__(*args)
    def executescript(self, script): return object.__getattribute__(self, '_real').executescript(script)
    def execute(self, sql, params=()):
        real=object.__getattribute__(self, '_real')
        armed=object.__getattribute__(self, '_armed')
        if armed and sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')):
            object.__setattr__(self, '_armed', False)
            raise RuntimeError('simulated power loss before commit')
        return real.execute(sql, params)


class IntakeAndNormalAudit(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory()
    def tearDown(self): self.tmp.cleanup()

    def test_normal_audit_is_consistent_and_idempotent(self):
        con=build_db(self.tmp.name)
        first=cli.audit(con)
        self.assertTrue(first['ok'])
        self.assertEqual(first['classification'], 'consistent')
        self.assertEqual(first['mismatches'], [])
        self.assertEqual(first['recovery_plans'], [])
        self.assertTrue(all(c['byte_match'] for c in first['source_checks']))
        # repeated audits are deterministic and never mutate state
        second=cli.audit(con)
        self.assertEqual(second['source_checks'], first['source_checks'])
        self.assertEqual(ac.verify_projection_stamps(con), [])
        self.assertEqual(storage.report(con), storage.report(con))
        con.close()

    def test_capture_keeps_digest_sequence_instants_batch_and_row_provenance(self):
        con=build_db(self.tmp.name)
        for seq, row in enumerate(con.execute('SELECT * FROM events ORDER BY rowid')):
            src=con.execute('SELECT * FROM event_sources WHERE event_id=?', (row['id'],)).fetchone()
            self.assertEqual(src['raw_digest'], row['digest'])
            self.assertEqual(src['seq'], seq+1)
            self.assertEqual(src['effective_at'], row['effective_at'])
            self.assertEqual(src['received_at'], row['received_at'])
        stamps={r['row_key']: r for r in con.execute('SELECT * FROM projection_sources')}
        # every projected row carries its source event id, version and row digest
        self.assertEqual(stamps['receipts:v2']['source_event_id'], 'v2')
        self.assertEqual(stamps['receipts:v2']['projection_version'], 1)
        self.assertEqual(stamps['subjects:couple-1']['source_event_id'], 'v2')
        self.assertTrue(stamps['decisions:1']['row_digest'])
        con.close()

    def test_backup_sheet_and_export_package_intake_verify_seals(self):
        con=build_db(self.tmp.name)
        rows=[{'event_id': r['id'], 'subsystem': r['subsystem'], 'seq': i,
               'effective_at': r['effective_at'], 'received_at': r['received_at'],
               'raw_digest': r['digest'], 'bytes': len(r['payload'])}
              for i, r in enumerate(con.execute('SELECT * FROM events ORDER BY rowid'))]
        sheet_path=Path(self.tmp.name)/'sheet.json'
        sheet=ac.build_backup_sheet(sheet_path, rows, 'sheet-1', now=10)
        intake=ac.ingest_capture(con, sheet['path'], now=11)
        self.assertEqual(intake['events'], 4)
        self.assertTrue(intake['trusted'])
        # tampering a sheet entry breaks the sealed manifest and is refused
        doc=json.loads(sheet_path.read_text(encoding='utf-8'))
        doc['rows'][0]['raw_digest']='0'*64
        sheet_path.write_text(json.dumps(doc, sort_keys=True), encoding='utf-8')
        with self.assertRaises(ValueError):
            ac.ingest_capture(storage.connect(':memory:'), sheet_path)
        # package intake anchors trusted source bytes
        path, info=sealed_package(self.tmp.name, con)
        pkg_intake=ac.ingest_capture(storage.connect(':memory:'), path)
        self.assertEqual(pkg_intake['sealed_digest'], info['sealed_digest'])
        anchored=storage.connect(':memory:')
        ac.ingest_capture(anchored, path)
        v1=anchored.execute('SELECT * FROM event_sources WHERE event_id=?', ('v1',)).fetchone()
        self.assertEqual(v1['subsystem'], 'visit')
        self.assertEqual(v1['seq'], 3)
        self.assertEqual(v1['anchored'], 1)
        self.assertEqual(v1['trusted'], storage.TRUST_LEVELS['trusted'])
        anchored.close(); con.close()

    def test_manual_repair_record_is_sealed(self):
        path=Path(self.tmp.name)/'repair.json'
        out=ac.build_repair_record(path, {'repair_id': 'manual-1', 'note': 'operator note'}, now=22)
        doc=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(doc['record_digest'], out['sealed_digest'])
        self.assertEqual(doc['recorded_at'], 22)


class FaultAProjectionOnly(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.con=build_db(self.tmp.name)
        self.con.execute("UPDATE receipts SET state='needs-correction' WHERE id='v2'")
        self.con.commit()

    def tearDown(self): self.con.close(); self.tmp.cleanup()

    def test_legacy_mismatch_list_still_only_names_receipts(self):
        result=cli.audit(self.con)
        self.assertFalse(result['ok'])
        self.assertEqual(result['mismatches'], ['receipts'])

    def test_classification_and_exact_row_localization(self):
        result=cli.audit(self.con)
        self.assertEqual(result['classification'], 'projection-only')
        self.assertTrue(all(c['byte_match'] for c in result['source_checks']))
        first=result['evidence']['first_divergent_row']
        self.assertEqual(first['table'], 'receipts')
        self.assertEqual(first['row_id'], 'v2')
        diff=result['evidence']['projection_diffs']['receipts'][0]
        self.assertEqual(diff['live']['state'], 'needs-correction')
        self.assertEqual(diff['expected']['state'], 'completed')
        failures=result['evidence']['stamp_failures']
        self.assertEqual([f['row_key'] for f in failures], ['receipts:v2'])
        self.assertEqual(failures[0]['source_event_id'], 'v2')

    def test_rebuild_from_trusted_events_restores_completed(self):
        plan=cli.audit(self.con)['recovery_plans'][0]
        self.assertEqual(plan['route'], 'rebuild-from-trusted-events')
        self.assertTrue(plan['immutable'])
        # nothing changes before an operator selects the plan
        self.assertEqual(storage.latest(self.con, 'receipts', 'v2')['state'], 'needs-correction')
        out=ac.apply_recovery(self.con, plan['plan_digest'])
        self.assertFalse(out['idempotent'])
        self.assertEqual(storage.latest(self.con, 'receipts', 'v2')['state'], 'completed')
        self.assertTrue(cli.audit(self.con)['ok'])
        record=storage.get_repair(self.con, plan['plan_digest'])
        self.assertEqual(record['route'], 'rebuild-from-trusted-events')
        # same plan digest reapplies idempotently instead of double-repairing
        again=ac.apply_recovery(self.con, plan['plan_digest'])
        self.assertTrue(again['idempotent'])
        self.assertEqual(self.con.execute('SELECT COUNT(*) n FROM repair_records').fetchone()['n'], 1)

    def test_rebuild_refused_when_source_later_becomes_untrusted(self):
        plan=cli.audit(self.con)['recovery_plans'][0]
        tamper_v1_past_deadline(self.con)  # source trust lost after plan was produced
        with self.assertRaises(ac.RecoveryError):
            ac.apply_recovery(self.con, plan['plan_digest'])


class FaultBSourceUntrusted(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.con=build_db(self.tmp.name, 'faultB.db')
        self.pkg_path, self.pkg=sealed_package(self.tmp.name, self.con)
        ac.ingest_capture(self.con, self.pkg_path, now=501)
        self.tampered_raw=tamper_v1_past_deadline(self.con)

    def tearDown(self): self.con.close(); self.tmp.cleanup()

    def test_legacy_audit_reports_three_tables(self):
        self.assertEqual(cli.audit(self.con)['mismatches'], ['decisions', 'events', 'receipts'])

    def test_first_provable_fork_and_causal_impact_chain(self):
        result=cli.audit(self.con)
        self.assertEqual(result['classification'], 'source-untrusted')
        fork=result['evidence']['first_untrusted_event']
        self.assertEqual(fork['event_id'], 'v1')
        self.assertEqual(fork['stored_digest'],
                         hashlib_sha(self._original_v1_bytes()))
        self.assertNotEqual(fork['stored_digest'], fork['actual_digest'])
        impact=result['evidence']['causal_impact']
        self.assertEqual(impact['downstream_event_ids'], ['v1', 'v2'])
        self.assertEqual({d['event_id'] for d in impact['affected_decisions']}, {'v1', 'v2'})
        self.assertEqual({r['id'] for r in impact['affected_receipts']}, {'v1', 'v2'})
        self.assertEqual({s['id'] for s in impact['affected_subjects']}, {'couple-1'})
        # tampered replay is explicitly labeled simulation, never a recovery target
        sim=result['evidence']['tampered_replay_simulation']
        self.assertIn('never a recovery target', sim['note'])
        self.assertEqual(sorted(sim['mismatches']), ['decisions', 'receipts'])

    def _original_v1_bytes(self):
        # the pristine bytes are exactly what the sealed package holds
        return (self.pkg_path/'events'/'v1.json').read_bytes()

    def test_two_immutable_plans_with_distinct_conclusions(self):
        plans={p['route']: p for p in cli.audit(self.con)['recovery_plans']}
        restore=plans['restore-sealed-source']
        quarantine=plans['quarantine-untrusted-source']
        self.assertTrue(restore['immutable'] and quarantine['immutable'])
        self.assertTrue(restore['available'])  # sealed package exists
        self.assertEqual(restore['requires'], 'sealed-export-credential')
        self.assertEqual(quarantine['requires'], 'explicit-operator-authorization')
        self.assertNotEqual(restore['plan_digest'], quarantine['plan_digest'])
        # simulated post-recovery states are genuinely different
        self.assertEqual([(r[0], r[2]) for r in restore['plan']['expected_summary']['receipts']],
                         [('v1', 'checked'), ('v2', 'completed')])
        self.assertEqual(quarantine['plan']['expected_summary']['receipts'], [])
        # plans are deterministic: same database state yields same digest
        again={p['route']: p['plan_digest'] for p in cli.audit(self.con)['recovery_plans']}
        self.assertEqual(again['restore-sealed-source'], restore['plan_digest'])
        self.assertEqual(again['quarantine-untrusted-source'], quarantine['plan_digest'])

    def test_restore_route_requires_credential_and_preserves_tamper_evidence(self):
        plan=next(p for p in cli.audit(self.con)['recovery_plans']
                  if p['route']=='restore-sealed-source')
        with self.assertRaises(ac.RecoveryError):
            ac.apply_recovery(self.con, plan['plan_digest'])
        credential=plan['plan']['credentials'][0]
        out=ac.apply_recovery(self.con, plan['plan_digest'],
                              authorization={'credential': credential})
        result=out['result']
        self.assertEqual(storage.latest(self.con, 'events', 'v1')['effective_at'], 3)
        self.assertEqual([(r['id'], r['state']) for r in storage.report(self.con)['receipts']],
                         [('v1', 'checked'), ('v2', 'completed')])
        # tampered bytes are retained in the repair record, never silently dropped
        preserved=result['preserved_tampered_bytes']
        self.assertEqual(preserved['actual_digest'], hashlib_sha(self.tampered_raw.encode('utf-8')))
        self.assertNotEqual(preserved['actual_digest'], preserved['stored_digest'])
        self.assertTrue(cli.audit(self.con)['ok'])
        # idempotent retry
        self.assertTrue(ac.apply_recovery(self.con, plan['plan_digest'])['idempotent'])

    def test_forged_or_mismatched_credential_is_refused(self):
        plan=next(p for p in cli.audit(self.con)['recovery_plans']
                  if p['route']=='restore-sealed-source')
        forged=dict(plan['plan']['credentials'][0])
        forged['raw_digest']='f'*64
        with self.assertRaises(ac.RecoveryError):
            ac.apply_recovery(self.con, plan['plan_digest'], authorization={'credential': forged})
        # database untouched after refusal
        self.assertEqual(storage.latest(self.con, 'events', 'v1')['effective_at'], 11)

    def test_quarantine_route_requires_authorization_and_blocks_dependent_event(self):
        plan=next(p for p in cli.audit(self.con)['recovery_plans']
                  if p['route']=='quarantine-untrusted-source')
        with self.assertRaises(ac.RecoveryError):
            ac.apply_recovery(self.con, plan['plan_digest'])
        out=ac.apply_recovery(self.con, plan['plan_digest'],
                              authorization={'decision': 'quarantine-untrusted-source'})
        review=out['result']['manual_review']
        self.assertEqual(review[0], {'event_id': 'v1', 'reason': 'untrusted-source-quarantined'})
        self.assertIn({'event_id': 'v2', 'reason': 'no-valid-checkin'}, review)
        # v1 is isolated with both digests, v2 remains an event but re-projects blocked
        q=self.con.execute('SELECT * FROM quarantined_events WHERE event_id=?', ('v1',)).fetchone()
        self.assertIsNotNone(q)
        self.assertNotEqual(q['stored_digest'], q['actual_digest'])
        self.assertIsNone(self.con.execute("SELECT 1 FROM events WHERE id='v1'").fetchone())
        self.assertIsNotNone(self.con.execute("SELECT 1 FROM events WHERE id='v2'").fetchone())
        decisions=storage.report(self.con)['decisions']
        self.assertEqual([d for d in decisions if d['event_id']=='v2'][0]['outcome'], 'blocked')
        self.assertEqual(storage.report(self.con)['receipts'], [])
        # readers see one self-consistent version
        self.assertTrue(cli.audit(self.con)['ok'])
        self.assertEqual(ac.verify_projection_stamps(self.con), [])
        # untrusted source is marked so it can never anchor a later replay
        src=self.con.execute('SELECT trusted,anchored FROM event_sources WHERE event_id=?', ('v1',)).fetchone()
        self.assertEqual((src['trusted'], src['anchored']), (0, 0))

    def test_restore_unavailable_when_archive_is_missing(self):
        # a database with the same tamper but no sealed intake
        bare=build_db(self.tmp.name, 'bare.db')
        tamper_v1_past_deadline(bare)
        restore=next(p for p in cli.audit(bare)['recovery_plans']
                     if p['route']=='restore-sealed-source')
        self.assertFalse(restore['available'])
        self.assertEqual(restore['plan']['credentials'], [])
        with self.assertRaises(ac.RecoveryError):
            ac.apply_recovery(bare, restore['plan_digest'])
        # quarantine remains the authorized fallback
        q=next(p for p in cli.audit(bare)['recovery_plans']
               if p['route']=='quarantine-untrusted-source')
        self.assertTrue(q['available'])
        out=ac.apply_recovery(bare, q['plan_digest'],
                              authorization={'decision': 'quarantine-untrusted-source'})
        self.assertEqual(out['route'], 'quarantine-untrusted-source')


class CrashAndRetry(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory()
    def tearDown(self): self.tmp.cleanup()

    def _fault_a(self):
        con=build_db(self.tmp.name, 'crash-a.db')
        con.execute("UPDATE receipts SET state='needs-correction' WHERE id='v2'"); con.commit()
        return con

    def _fault_b(self):
        con=build_db(self.tmp.name, 'crash-b.db')
        path, _=sealed_package(self.tmp.name, con)
        ac.ingest_capture(con, path, now=600)
        tamper_v1_past_deadline(con)
        return con

    def test_rebuild_crash_leaves_no_half_repair_and_retries(self):
        con=self._fault_a(); digest=cli.audit(con)['recovery_plans'][0]['plan_digest']
        proxy=CrashBeforeCommit(con); proxy.arm()
        with self.assertRaises(RuntimeError):
            ac.apply_recovery(proxy, digest)
        self.assertEqual(storage.latest(con, 'receipts', 'v2')['state'], 'needs-correction')
        self.assertEqual(con.execute('SELECT COUNT(*) n FROM repair_records').fetchone()['n'], 0)
        self.assertTrue(any(p['plan_digest']==digest for p in cli.audit(con)['recovery_plans']))
        ac.apply_recovery(con, digest)
        self.assertEqual(storage.latest(con, 'receipts', 'v2')['state'], 'completed')

    def test_quarantine_crash_keeps_events_and_receipts_self_consistent(self):
        con=self._fault_b()
        digest=next(p['plan_digest'] for p in cli.audit(con)['recovery_plans']
                    if p['route']=='quarantine-untrusted-source')
        proxy=CrashBeforeCommit(con); proxy.arm()
        with self.assertRaises(RuntimeError):
            ac.apply_recovery(proxy, digest, authorization={'decision': 'quarantine-untrusted-source'})
        # no half state: v1 still present, old receipts still the visible version
        self.assertIsNotNone(con.execute("SELECT 1 FROM events WHERE id='v1'").fetchone())
        self.assertEqual(con.execute('SELECT COUNT(*) n FROM quarantined_events').fetchone()['n'], 0)
        self.assertEqual([r['state'] for r in con.execute('SELECT state FROM receipts ORDER BY id')],
                         ['checked', 'completed'])
        ac.apply_recovery(con, digest, authorization={'decision': 'quarantine-untrusted-source'})
        self.assertTrue(cli.audit(con)['ok'])

    def test_restore_crash_does_not_swap_events_without_receipts(self):
        con=self._fault_b()
        digest=next(p['plan_digest'] for p in cli.audit(con)['recovery_plans']
                    if p['route']=='restore-sealed-source')
        credential=next(p for p in cli.audit(con)['recovery_plans']
                        if p['route']=='restore-sealed-source')['plan']['credentials'][0]
        proxy=CrashBeforeCommit(con); proxy.arm()
        with self.assertRaises(RuntimeError):
            ac.apply_recovery(proxy, digest, authorization={'credential': credential})
        # events were not swapped while receipts stayed old
        self.assertEqual(storage.latest(con, 'events', 'v1')['effective_at'], 11)
        self.assertEqual([r['state'] for r in con.execute('SELECT state FROM receipts ORDER BY id')],
                         ['checked', 'completed'])
        self.assertEqual(con.execute('SELECT COUNT(*) n FROM repair_records').fetchone()['n'], 0)
        ac.apply_recovery(con, digest, authorization={'credential': credential})
        self.assertEqual(storage.latest(con, 'events', 'v1')['effective_at'], 3)
        self.assertTrue(cli.audit(con)['ok'])


class LegacyFixtureCompatibility(unittest.TestCase):
    def test_visit_normal_first_three_events_match_corruption_fixture_prefix(self):
        normal=load_fixture_events(VISIT_NORMAL)
        corruption=load_fixture_events(FIXTURE)
        self.assertEqual(normal, corruption[:3])
        # the prefix alone replays to eligible / confirmed / checked
        with tempfile.TemporaryDirectory() as tmp:
            con=storage.connect(Path(tmp)/'legacy.db')
            for subsystem, event in normal:
                cli.run_event(con, subsystem, event)
            report=storage.report(con)
            self.assertEqual(report['subjects'][0]['state'], 'eligible')
            self.assertEqual(storage.latest(con, 'allocations', 'b1')['state'], 'confirmed')
            self.assertEqual(storage.latest(con, 'receipts', 'v1')['state'], 'checked')
            self.assertTrue(cli.audit(con)['ok'])
            con.close()

    def test_audit_result_keeps_legacy_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            con=build_db(tmp)
            result=cli.audit(con)
            for key in ('ok', 'mismatches', 'replayed_events'):
                self.assertIn(key, result)
            self.assertEqual(result['replayed_events'], 4)
            con.close()

    def test_legacy_v1_database_opens_and_keeps_event_conflict_protection(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'legacy.db'
            raw=sqlite3.connect(path)
            raw.executescript(
                'CREATE TABLE events(id TEXT PRIMARY KEY, subsystem TEXT NOT NULL, action TEXT NOT NULL,'
                ' subject TEXT NOT NULL, effective_at INTEGER NOT NULL, received_at INTEGER NOT NULL,'
                " payload TEXT NOT NULL, digest TEXT NOT NULL);"
                'CREATE TABLE decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,'
                ' subsystem TEXT NOT NULL, subject TEXT NOT NULL, outcome TEXT NOT NULL,'
                ' reason TEXT NOT NULL, state TEXT NOT NULL);'
                'CREATE TABLE subjects(id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL);'
                'CREATE TABLE documents(id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL);'
                'CREATE TABLE allocations(id TEXT PRIMARY KEY, subject TEXT NOT NULL, resource TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL);'
                'CREATE TABLE receipts(id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, amount INTEGER NOT NULL DEFAULT 0);')
            event={'id': 'e1', 'subject': 's1', 'action': 'applicant',
                   'data': {'partner': 's2', 'valid_until': 10, 'region': 'local'},
                   'effective_at': 1, 'received_at': 1}
            canon=json.dumps(event, sort_keys=True, separators=(',', ':'))
            raw.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',
                        ('e1', 'applicant', 'applicant', 's1', 1, 1, canon, storage.digest_of(canon)))
            raw.commit(); raw.close()
            # connect transparently migrates the schema without touching rows
            con=storage.connect(path)
            self.assertTrue(ac.verify_event_bytes(con)[0]['byte_match'])
            self.assertEqual(cli.audit(con)['classification'], 'projection-only')
            plan=cli.audit(con)['recovery_plans'][0]
            ac.apply_recovery(con, plan['plan_digest'])
            self.assertTrue(cli.audit(con)['ok'])
            # original event-id conflict protection still rejects divergent bytes
            event['data']['region'] = 'bad'
            with self.assertRaises(ValueError):
                cli.run_event(con, 'applicant', event)
            con.close()


def hashlib_sha(raw):
    import hashlib
    return hashlib.sha256(storage.canonical(raw)).hexdigest()


if __name__ == '__main__':
    unittest.main()
