"""Version-aware archive closing snapshots.

The working case is ``fixtures/archive-mixed-pages.json``: application v1 is
missing page 2, application v2 is complete, OCR mislabels a v2 page as v1, and
the effective receipt R1 still points at v1.  The kind list says
``application`` and ``receipt`` are present, but no single physical version is
provably complete and R1 is dangling.
"""
import copy
import json
from pathlib import Path

from marrydesk import archive, archive_capture, retention

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


def mixed():
    return load('archive-mixed-pages.json')


def page_sha(record, batch, page):
    return next(scan['sha256'] for scan in record['scans']
                if scan['batch'] == batch and int(scan['page']) == page)


def codes(verdict):
    return [issue['code'] for issue in verdict['issues']]


def issue(verdict, code, **match):
    for entry in verdict['issues']:
        if entry['code'] != code:
            continue
        if all(entry.get(key) == value for key, value in match.items()):
            return entry
    raise AssertionError(f'no {code} issue matching {match} in {verdict["issues"]}')


# --- incoherent baseline ----------------------------------------------------

def test_mixed_pages_are_not_sealed_despite_complete_kind_list():
    record = mixed()
    verdict = archive.evaluate(record)
    # This is the trap: the naive kind check still says everything is there.
    assert verdict['kind_list_complete'] is True
    assert archive.complete(record) is False
    assert verdict['state'] == 'open'
    assert verdict['decision'] == 'dangling-reference'
    assert verdict['content'] == []
    assert verdict['documents'] == []


def test_exact_missing_page_and_cross_version_evidence():
    verdict = archive.evaluate(mixed())
    missing = issue(verdict, 'missing-page', material='application', version=1, page=2)
    assert missing['available_pages'] == [1, 3]
    assert [item['version'] for item in missing['alternate_versions']] == [2]
    assert missing['alternate_versions'][0]['sha256'] == page_sha(mixed(), 'B-v2', 2)

    cross = issue(verdict, 'cross-version-page', material='application', version=1, page=2)
    assert cross['alternate_versions'][0]['version'] == 2


def test_ocr_mislabel_does_not_reassign_physical_version():
    verdict = archive.evaluate(mixed())
    v2 = next(v for v in verdict['versions'] if v['version'] == 2)
    mislabel = next(entry for entry in v2['issues'] if entry['code'] == 'ocr-mislabel')
    assert mislabel['page'] == 2
    assert mislabel['ocr_version'] == 1
    assert mislabel['physical_version'] == 2
    assert verdict['ocr_conflicts'][0]['batch'] == 'B-v2'
    # v2 is still complete from its own batch; OCR is evidence, not provenance.
    assert v2['pages'] == [1, 2, 3]
    assert v2['complete'] is True


def test_zero_missing_but_dangling_receipt_is_caught_separately():
    record = mixed()
    # Make v1 complete so page aggregation is no longer the problem.
    archive_capture.receive(record, {
        'id': 'B-v1-found', 'material': 'application', 'version': 1,
        'page_count': 3, 'uploaded_at': 40,
        'pages': [{'page': 2, 'sha256': page_sha(record, 'B-v1', 1) + '-found'}],
    })
    verdict = archive.evaluate(record)
    assert verdict['state'] == 'sealed'
    assert codes(verdict) == []

    # A receipt pointing at version 9 is dangling even with every page present.
    record['receipts'][0]['version'] = 9
    verdict = archive.evaluate(record)
    assert verdict['state'] == 'open'
    dangling = issue(verdict, 'dangling-reference', receipt='R1')
    assert 'target-not-captured' in dangling['message']


def test_receipt_referencing_incomplete_target_is_dangling():
    verdict = archive.evaluate(mixed())
    dangling = issue(verdict, 'dangling-reference', receipt='R1')
    assert 'target-missing-pages:2' in dangling['message']
    assert verdict['receipts'][0]['resolved'] is False


# --- route A: recover the genuine v1 page 2 --------------------------------

def test_route_a_recovered_page_keeps_r1_on_v1_and_keeps_v2():
    record = mixed()
    archive_capture.receive(record, {
        'id': 'B-v1-recovered', 'material': 'application', 'version': 1,
        'page_count': 3, 'uploaded_at': 40,
        'pages': [{'page': 2, 'sha256': page_sha(record, 'B-v1', 1) + '-recovered'}],
    })
    verdict = archive.evaluate(record)
    assert verdict['state'] == 'sealed'
    assert verdict['decision'] == 'sealed'
    assert verdict['effective_receipts'] == ['R1']
    assert [(doc['material'], doc['version'], doc['pages']) for doc in verdict['content']] == [
        ('application', 1, [1, 2, 3]),
        ('application', 2, [1, 2, 3]),
    ]
    # The recovered page is attributed to a v1 batch, not borrowed from v2.
    v1 = next(v for v in verdict['versions'] if v['version'] == 1)
    assert v1['pages'] == [1, 2, 3]
    assert v1['missing_pages'] == []
    r1 = verdict['receipts'][0]
    assert r1['resolved'] is True and r1['reason'] == 'resolved-in-snapshot'
    # v2 stays as later material, not a replacement of v1.
    assert verdict['history']['materials'] == []


def test_route_a_rejects_a_recovered_page_with_conflicting_digest():
    record = mixed()
    archive_capture.receive(record, {
        'id': 'B-v1-recovered', 'material': 'application', 'version': 1,
        'page_count': 3, 'uploaded_at': 40,
        'pages': [{'page': 2, 'sha256': 'not-the-original'}],
    })
    # Simulate a later scan claiming a different digest for that same page.
    record['scans'].append(dict(record['scans'][-1], sha256='a-third-digest'))
    verdict = archive.evaluate(record)
    assert verdict['state'] == 'open'
    conflict = issue(verdict, 'conflicting-page', material='application', version=1, page=2)
    assert len(conflict['digests']) == 2


# --- route B: redo under v2 with a new receipt R2 ---------------------------

def test_route_b_new_receipt_keeps_r1_history_and_seals_on_v2_only():
    record = mixed()
    archive_capture.add_reference(record, 'R2', role='receipt',
                                  target={'material': 'application', 'version': 2})
    record['receipts'][-1]['signed_at'] = 30
    archive_capture.identity(record, 'ID-v2', receipt='R2', material='application',
                             version=2, batch='B-v2')

    verdict = archive.evaluate(record)
    assert verdict['state'] == 'sealed'
    assert verdict['decision'] == 'sealed'
    # Only the complete v2 is effective content; R1/v1 are history.
    assert [(doc['material'], doc['version'], doc['pages']) for doc in verdict['content']] == [
        ('application', 2, [1, 2, 3]),
    ]
    assert verdict['effective_receipts'] == ['R2']
    history_receipts = {ref['id']: ref for ref in verdict['history']['receipts']}
    assert history_receipts['R1']['resolved'] is False
    assert history_receipts['R1']['superseded_by'] == 'R2'
    history_materials = {item['version']: item for item in verdict['history']['materials']}
    assert history_materials[1]['missing_pages'] == [2]
    assert history_materials[1]['complete'] is False


def test_route_b_without_identity_proof_is_not_sealed():
    record = mixed()
    archive_capture.add_reference(record, 'R2', role='receipt',
                                  target={'material': 'application', 'version': 2})
    record['receipts'][-1]['signed_at'] = 30
    verdict = archive.evaluate(record)
    assert verdict['state'] == 'open'
    issue(verdict, 'identity-reference-missing', receipt='R2')


def test_route_b_receipt_citing_v2_while_v2_was_revoked_is_revoked():
    record = mixed()
    archive_capture.add_reference(record, 'R2', role='receipt',
                                  target={'material': 'application', 'version': 2})
    record['receipts'][-1]['signed_at'] = 30
    archive_capture.identity(record, 'ID-v2', receipt='R2', material='application',
                             version=2, batch='B-v2')
    archive_capture.revoke(record, 'REV-v2', {
        'material': 'application', 'version': 2, 'signed_at': 60, 'received_at': 60})
    verdict = archive.evaluate(record)
    assert verdict['state'] == 'open'
    assert verdict['decision'] == 'revoked'
    assert 'R2' in [ref['receipt'] for ref in verdict['issues'] if ref['code'] == 'receipt-revoked']


def test_routes_are_not_equivalent_and_no_latest_version_rule_fits():
    route_a = mixed()
    archive_capture.receive(route_a, {
        'id': 'B-v1-recovered', 'material': 'application', 'version': 1,
        'page_count': 3, 'uploaded_at': 40,
        'pages': [{'page': 2, 'sha256': page_sha(route_a, 'B-v1', 1) + '-recovered'}],
    })
    route_b = mixed()
    archive_capture.add_reference(route_b, 'R2', role='receipt',
                                  target={'material': 'application', 'version': 2})
    route_b['receipts'][-1]['signed_at'] = 30
    archive_capture.identity(route_b, 'ID-v2', receipt='R2', material='application',
                             version=2, batch='B-v2')

    verdict_a = archive.evaluate(route_a)
    verdict_b = archive.evaluate(route_b)
    assert verdict_a['state'] == verdict_b['state'] == 'sealed'
    assert verdict_a['effective_receipts'] == ['R1']
    assert verdict_b['effective_receipts'] == ['R2']
    doc_versions_a = {doc['version'] for doc in verdict_a['content']}
    doc_versions_b = {doc['version'] for doc in verdict_b['content']}
    assert doc_versions_a == {1, 2}
    assert doc_versions_b == {2}
    # A "newest version wins" rule cannot yield route A's v1-backed closure.
    assert 1 in doc_versions_a and 1 not in doc_versions_b


# --- duplicate uploads, late revocation, idempotent snapshots ---------------

def test_duplicate_scan_is_deduplicated_and_does_not_change_verdict():
    record = mixed()
    before = archive.evaluate(record)
    added = archive_capture.receive(record, {
        'id': 'B-v1', 'material': 'application', 'version': 1, 'page_count': 3,
        'uploaded_at': 10,
        'pages': [{'page': 1, 'sha256': page_sha(record, 'B-v1', 1)},
                  {'page': 3, 'sha256': page_sha(record, 'B-v1', 3)}],
    }, duplicate=True)
    assert added == 0
    after = archive.evaluate(record)
    assert after['state'] == before['state']
    assert codes(after) == codes(before)


def test_duplicate_scan_with_other_digest_is_conflicting_page():
    record = mixed()
    archive_capture.receive(record, {
        'id': 'B-v1', 'material': 'application', 'version': 1, 'page_count': 3,
        'pages': [{'page': 1, 'sha256': 'rescan-different-digest'}],
    }, duplicate=True)
    verdict = archive.evaluate(record)
    issue(verdict, 'conflicting-page', material='application', version=1, page=1)


def test_late_revocation_creates_new_snapshot_and_keeps_old_one():
    con = archive_capture.connect(':memory:')
    record = mixed()
    archive_capture.receive(record, {
        'id': 'B-v1-recovered', 'material': 'application', 'version': 1,
        'page_count': 3, 'uploaded_at': 40,
        'pages': [{'page': 2, 'sha256': page_sha(record, 'B-v1', 1) + '-recovered'}],
    })
    first = retention.disposition(record, con)
    assert first['state'] == 'ready'
    assert first['snapshot_version'] == 1

    archive_capture.revoke(record, 'REV-R1', {
        'material': 'application', 'version': 1, 'receipt': 'R1',
        'signed_at': 50, 'received_at': 50})
    second = retention.disposition(record, con)
    assert second['state'] == 'missing'
    assert second['decision'] == 'revoked'
    assert second['snapshot_version'] == 2

    snaps = archive_capture.snapshots(con, 'CASE-X')
    assert len(snaps) == 2
    assert snaps[0]['state'] == 'sealed'
    assert snaps[1]['state'] == 'open'
    # The old snapshot is not rewritten: it still carries the v1 closure.
    assert {(doc['material'], doc['version']) for doc in snaps[0]['documents']} == {
        ('application', 1), ('application', 2)}


def test_repeated_identical_seal_is_idempotent():
    con = archive_capture.connect(':memory:')
    record = mixed()
    archive_capture.receive(record, {
        'id': 'B-v1-recovered', 'material': 'application', 'version': 1,
        'page_count': 3, 'uploaded_at': 40,
        'pages': [{'page': 2, 'sha256': page_sha(record, 'B-v1', 1) + '-recovered'}],
    })
    first = retention.disposition(record, con)
    second = retention.disposition(record, con)
    assert first['snapshot_version'] == second['snapshot_version'] == 1
    assert second['snapshot']['created'] is False
    assert len(archive_capture.snapshots(con, 'CASE-X')) == 1


def test_capture_observations_and_windows_are_stored():
    con = archive_capture.connect(':memory:')
    record = mixed()
    archive_capture.window(record, 'W1', opens_at=0, closes_at=100)
    archive.seal(record, con)
    observations = con.execute(
        'SELECT batch, page, sha256 FROM capture_observations ORDER BY batch, page').fetchall()
    assert len(observations) == len(record['scans'])
    assert {row['batch'] for row in observations} == {'B-v1', 'B-v2'}
    window = con.execute('SELECT * FROM capture_windows WHERE id=?', ('W1',)).fetchone()
    assert (window['opens_at'], window['closes_at']) == (0, 100)


def test_original_pages_are_stored_with_digests_and_images():
    con = archive_capture.connect(':memory:')
    record = mixed()
    archive_capture.receive(record, {
        'id': 'B-bytes', 'material': 'application', 'version': 3, 'page_count': 1,
        'uploaded_at': 40,
        'pages': [{'page': 1, 'image_b64': 'cGFnZS0zLTE='}],  # b'page-3-1'
    })
    archive.seal(record, con)
    row = con.execute(
        'SELECT sha256, image FROM capture_pages WHERE batch=? AND version=? AND page=?',
        ('B-bytes', 3, 1)).fetchone()
    assert row['image'] == b'page-3-1'
    assert row['sha256'] == page_sha(record, 'B-bytes', 1)
    stored_batches = con.execute('SELECT COUNT(*) n FROM capture_batches').fetchone()['n']
    assert stored_batches == len(record['batches'])


def test_intake_window_and_unconfirmed_batch_are_recorded_evidence():
    record = mixed()
    archive_capture.window(record, 'W1', opens_at=0, closes_at=5)
    verdict = archive.evaluate(record)
    assert verdict['state'] == 'open'
    assert 'outside-intake-window' in codes(verdict)
    assert codes(verdict).count('outside-intake-window') == 5

    record = mixed()
    record['batches'][1]['confirmed'] = False
    verdict = archive.evaluate(record)
    assert verdict['state'] == 'open'
    unconfirmed = [entry for entry in verdict['issues']
                   if entry['code'] == 'unconfirmed-batch']
    assert {entry['version'] for entry in unconfirmed} == {2}
    # All three pages were physically uploaded in the unconfirmed v2 batch,
    # including the page OCR mislabelled as v1.
    assert {entry['page'] for entry in unconfirmed} == {1, 2, 3}


def test_correction_and_revocation_are_kept_as_capture_evidence():
    record = mixed()
    archive_capture.correct(record, 'C-v2', {
        'material': 'application', 'version': 2, 'from_version': 1, 'signed_at': 24})
    archive_capture.revoke(record, 'REV-R1', {
        'material': 'application', 'version': 1, 'receipt': 'R1', 'signed_at': 30})
    verdict = archive.evaluate(record)
    assert verdict['corrections'] == record['corrections']
    assert verdict['revocations'] == record['revocations']
    assert verdict['decision'] == 'revoked'


def test_dangling_correction_and_revocation_are_reported():
    record = mixed()
    archive_capture.correct(record, 'C-bad', {
        'material': 'application', 'version': 7, 'from_version': 1})
    archive_capture.revoke(record, 'REV-bad', {
        'material': 'application', 'version': 1, 'receipt': 'R-unknown'})
    verdict = archive.evaluate(record)
    codes_seen = codes(verdict)
    assert 'dangling-correction' in codes_seen
    assert 'dangling-revocation' in codes_seen


def test_late_scan_creates_second_snapshot_without_rewriting_first():
    con = archive_capture.connect(':memory:')
    record = mixed()
    first = retention.disposition(record, con)
    assert first['state'] == 'missing'
    assert first['snapshot_version'] == 1

    archive_capture.receive(record, {
        'id': 'B-v1-recovered', 'material': 'application', 'version': 1,
        'page_count': 3, 'uploaded_at': 40,
        'pages': [{'page': 2, 'sha256': page_sha(record, 'B-v1', 1) + '-recovered'}],
    })
    second = retention.disposition(record, con)
    assert second['state'] == 'ready'
    assert second['snapshot_version'] == 2
    snaps = archive_capture.snapshots(con, 'CASE-X')
    assert snaps[0]['state'] == 'open'
    assert snaps[0]['documents'] == []
    assert snaps[1]['state'] == 'sealed'


def test_stored_snapshot_payload_is_immutable_after_later_changes():
    con = archive_capture.connect(':memory:')
    record = mixed()
    archive_capture.receive(record, {
        'id': 'B-v1-recovered', 'material': 'application', 'version': 1,
        'page_count': 3, 'uploaded_at': 40,
        'pages': [{'page': 2, 'sha256': page_sha(record, 'B-v1', 1) + '-recovered'}],
    })
    retention.disposition(record, con)
    stored = archive_capture.snapshots(con, 'CASE-X')[0]

    record['batches'][0]['material'] = 'tampered'
    record['scans'][0]['sha256'] = 'tampered'
    assert archive_capture.snapshots(con, 'CASE-X')[0] == stored


# --- retention gating and legacy sample ------------------------------------

def test_retention_does_not_publish_manifest_until_sealed():
    record = mixed()
    result = retention.disposition(record)
    assert result['state'] == 'missing'
    assert 'manifest' not in result
    assert result['dangling_references'] == ['R1']
    assert result['blocking_pages'] == [2]
    assert retention.manifest(record) is None


def test_retention_publishes_manifest_after_closure():
    record = mixed()
    archive_capture.receive(record, {
        'id': 'B-v1-recovered', 'material': 'application', 'version': 1,
        'page_count': 3, 'uploaded_at': 40,
        'pages': [{'page': 2, 'sha256': page_sha(record, 'B-v1', 1) + '-recovered'}],
    })
    result = retention.disposition(record)
    assert result['state'] == 'ready'
    manifest = retention.manifest(record)
    assert manifest is not None
    assert manifest == result['manifest']
    assert [(doc['material'], doc['version'], doc['pages']) for doc in manifest['content']] == [
        ('application', 1, [1, 2, 3]),
        ('application', 2, [1, 2, 3]),
    ]


def test_unrelated_complete_material_is_history_not_content():
    record = mixed()
    archive_capture.receive(record, {
        'id': 'B-v1-recovered', 'material': 'application', 'version': 1,
        'page_count': 3, 'uploaded_at': 40,
        'pages': [{'page': 2, 'sha256': page_sha(record, 'B-v1', 1) + '-recovered'}],
    })
    archive_capture.receive(record, {
        'id': 'B-photo', 'material': 'photo', 'version': 1,
        'page_count': 1, 'uploaded_at': 40,
        'pages': [{'page': 1, 'sha256': 'photo-page-1'}],
    })
    verdict = archive.evaluate(record)
    assert verdict['state'] == 'sealed'
    assert [doc['material'] for doc in verdict['content']] == ['application', 'application']
    history = {(item['material'], item['version']) for item in verdict['history']['materials']}
    assert history == {('photo', 1)}


def test_delivery_status_scans_are_not_treated_as_capture():
    # delivery.py fixtures use a `scans` list without pages or batches.
    record = load('delivery_normal.json')
    assert archive.has_capture(record) is False
    assert archive.complete({'case_id': 'C', 'required': ['delivery'],
                             'documents': ['delivery']}) is True


def test_old_kind_only_sample_still_works():
    record = load('archive_normal.json')
    assert archive.complete(record) is True
    assert retention.disposition(record) == {'case_id': 'COUPLE-1', 'state': 'ready'}
    assert retention.manifest(record) is None  # no page evidence to enumerate


def test_capture_normalize_does_not_mutate_input():
    record = mixed()
    snapshot = copy.deepcopy(record)
    archive.evaluate(record)
    assert record == snapshot
