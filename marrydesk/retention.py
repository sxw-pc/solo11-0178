"""Retention disposition: publish ``ready`` only after a closed snapshot.

A kind-name-complete manifest is not enough.  For captured cases the
disposition is derived from the page/version/receipt evidence in
``archive.evaluate``; the content manifest is published only when that
snapshot is sealed.  Later scans or revocations create a new snapshot instead
of rewriting the earlier one.
"""
from . import archive
from . import archive_capture


def _ready_result(record, verdict):
    result = {'case_id': record['case_id'], 'state': 'ready', 'decision': verdict['decision']}
    result['manifest'] = {
        'case_id': record['case_id'],
        'content': verdict['content'],
        'effective_receipts': verdict['effective_receipts'],
    }
    return result


def _open_result(record, verdict):
    return {
        'case_id': record['case_id'],
        'state': 'missing',
        'decision': verdict['decision'],
        'issues': verdict['issues'],
        'blocking_pages': sorted({
            issue['page'] for issue in verdict['issues'] if issue.get('page') is not None}),
        'dangling_references': sorted({
            issue['receipt'] for issue in verdict['issues']
            if issue['code'] == 'dangling-reference'}),
    }


def manifest(record, con=None):
    """Published content list, or ``None`` while the case is still open."""
    if con is not None:
        verdict, _ = archive.seal(record, con)
    else:
        verdict = archive.evaluate(record)
    if verdict['state'] != 'sealed':
        return None
    return {'case_id': record['case_id'],
            'content': verdict['content'],
            'effective_receipts': verdict['effective_receipts']}


def disposition(record, con=None):
    if not archive.has_capture(record):
        # Legacy kind-only manifests have no page evidence to snapshot; keep
        # their original ready/missing disposition.
        return {'case_id': record['case_id'],
                'state': 'ready' if archive.complete(record) else 'missing'}

    if con is None:
        verdict = archive.evaluate(record)
        result = _ready_result(record, verdict) if verdict['state'] == 'sealed' \
            else _open_result(record, verdict)
        return result

    verdict, info = archive.seal(record, con)
    result = _ready_result(record, verdict) if verdict['state'] == 'sealed' \
        else _open_result(record, verdict)
    result['snapshot'] = info
    result['snapshot_version'] = info['seq']
    result['snapshots'] = archive_capture.snapshots(con, record['case_id'])
    return result
