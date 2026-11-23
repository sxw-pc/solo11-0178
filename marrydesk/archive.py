"""Version-aware, page-level archive closing snapshots.

A case is only ``sealed`` when, for every required material kind, there is a
page group that is:

* physically one material/version (the upload batch decides provenance, not
  OCR labels),
* continuous from page 1 through the declared page count,
* free of conflicting digests for the same page,
* not revoked, and

every effective receipt reference resolves to such a group in this case.
Superseded receipts stay in the history but do not have to resolve.  A case
with no capture evidence at all is still handled by the legacy kind-name check
so old kind-only manifests keep their previous disposition.

Receipts are versioned by role: the latest signed receipt for a role is the
effective one.  That is how recovering an old page (R1 still cites v1) and
re-filing under a corrected version (new R2 cites v2) stay distinct outcomes
instead of collapsing into one "newest version wins" rule.
"""
from . import archive_capture as capture

_RECEIPT_ROLES = ('receipt', 'processing-receipt')
_CAPTURE_KEYS = ('batches', 'receipts', 'identities', 'corrections',
                 'revocations', 'windows')

_ISSUE_REASONS = {
    'missing-page': 'missing-pages',
    'required-kind-missing': 'missing-pages',
    'cross-version-page': 'cross-version',
    'conflicting-page': 'conflicting-pages',
    'ocr-mislabel': 'unconfirmed-provenance',
    'page-count-unknown': 'unconfirmed-page-count',
    'page-count-mismatch': 'conflicting-page-count',
    'out-of-range-page': 'out-of-range-page',
    'outside-intake-window': 'outside-intake-window',
    'unconfirmed-batch': 'unconfirmed-provenance',
    'revoked-version': 'revoked',
    'receipt-revoked': 'revoked',
    'dangling-reference': 'dangling-reference',
    'identity-reference-missing': 'dangling-reference',
    'identity-reference-mismatch': 'dangling-reference',
    'dangling-identity': 'dangling-reference',
    'dangling-correction': 'dangling-reference',
    'dangling-revocation': 'dangling-reference',
}
_REASON_ORDER = ('revoked', 'dangling-reference', 'cross-version', 'missing-pages',
                 'conflicting-pages', 'conflicting-page-count', 'out-of-range-page',
                 'outside-intake-window', 'unconfirmed-page-count',
                 'unconfirmed-provenance')


def _issue_reason(code):
    return _ISSUE_REASONS.get(code, code)


def has_capture(record):
    """True for capture-shaped evidence, not for unrelated ``scans`` lists.

    Delivery/status records also use a ``scans`` key, so scans only count when
    they carry a physical page and upload batch.
    """
    if any(record.get(key) for key in _CAPTURE_KEYS):
        return True
    return any(isinstance(scan, dict) and 'page' in scan and 'batch' in scan
               for scan in record.get('scans', []))


def _batch_ids(groups):
    return {batch for group in groups.values() for batch in group['batches']}


def _identity_checks(record, groups, effective):
    """A receipt must cite an identity proof from the same material/version.

    The identity batch must also be present in this case's captured pages.
    """
    by_receipt = {}
    for item in record.get('identities', []):
        by_receipt.setdefault(item.get('receipt'), []).append(item)
    issues = []
    for ref in effective:
        proofs = by_receipt.get(ref['id'], [])
        if not proofs:
            issues.append(_issue('identity-reference-missing', ref['material'], ref['version'],
                                  None, f'receipt {ref["id"]} has no captured identity proof',
                                  receipt=ref['id']))
            continue
        for proof in proofs:
            batch_id = proof.get('batch')
            if batch_id not in _batch_ids(groups):
                issues.append(_issue('dangling-identity', ref['material'], ref['version'], None,
                                      f'identity proof for receipt {ref["id"]} cites unknown '
                                      f'batch {batch_id!r}', receipt=ref['id']))
                continue
            target = (proof.get('material'), int(proof.get('version', 1)))
            if target != (ref['material'], ref['version']):
                issues.append(_issue(
                    'identity-reference-mismatch', ref['material'], ref['version'], None,
                    f'identity proof for receipt {ref["id"]} belongs to '
                    f'{target[0]} v{target[1]}, not {ref["material"]} v{ref["version"]}',
                    receipt=ref['id']))
    return issues


def _target_checks(record, groups):
    """Corrections and revocations must point at captured material.

    A correction that cites a version not present in this case, or a
    revocation that cites a receipt never captured here, is a dangling
    reference just like a receipt pointing nowhere.
    """
    issues = []
    for item in record.get('corrections', []):
        key = (item['material'], int(item['version']))
        if key not in groups:
            issues.append(_issue('dangling-correction', item['material'], item['version'], None,
                                  f"correction {item.get('id')} points at {key[0]} v{key[1]}, "
                                  f'which was never captured'))
    known_receipts = {ref['id'] for ref in record.get('receipts', [])}
    for item in record.get('revocations', []):
        receipt = item.get('receipt')
        if receipt is not None and receipt not in known_receipts:
            issues.append(_issue('dangling-revocation', item['material'], item['version'], None,
                                  f"revocation {item.get('id')} cites unknown receipt {receipt!r}"))
    return issues


def _issue(code, material, version, page, message, **extra):
    item = {'code': code, 'material': material, 'version': version,
            'page': page, 'message': message}
    item.update(extra)
    return item


def _new_group(material, version):
    return {'material': material, 'version': version, 'batches': [], 'pages': {},
            'page_sources': {}, 'page_counts': set(), 'conflicts': {},
            'ocr_mislabels': [], 'outside_window': [], 'unconfirmed': []}


def _outside_window(record, scan):
    """True only when intake windows are declared and the scan misses them."""
    windows = record.get('windows') or []
    if not windows:
        return False
    received = int(scan.get('received_at', scan.get('signed_at', 0)))
    return not any(int(item['opens_at']) <= received <= int(item['closes_at'])
                   for item in windows)


def _unconfirmed_batch(record, scan):
    """An upload batch not declared up front cannot prove a version."""
    batch = capture.batch_index(record).get(scan.get('batch'))
    return batch is None or batch.get('confirmed') is False


def _groups(record):
    """Build material/version groups from physical batch provenance."""
    groups = {}
    ocr_conflicts = []

    def group_for(material, version):
        key = (material, int(version))
        return groups.setdefault(key, _new_group(material, int(version)))

    for batch in record.get('batches', []):
        group = group_for(batch['material'], batch['version'])
        group['batches'].append(batch['id'])
        if int(batch.get('page_count') or 0):
            group['page_counts'].add(int(batch['page_count']))

    for scan in record.get('scans', []):
        physical = capture.physical_scan(record, scan)
        if physical['material'] is None:
            continue
        group = group_for(physical['material'], physical['version'])
        page = int(scan['page'])
        if int(physical.get('page_count') or 0):
            group['page_counts'].add(int(physical['page_count']))
        if int(scan.get('page_count') or 0):
            group['page_counts'].add(int(scan['page_count']))
        digest = scan.get('sha256')
        previous = group['pages'].get(page)
        if previous is not None and previous != digest:
            group['conflicts'].setdefault(page, {previous, digest})
        else:
            group['pages'][page] = digest
            group['page_sources'][page] = physical.get('batch')
        ocr_material = scan.get('material')
        ocr_version = int(scan.get('version', physical['version']))
        if ocr_material != physical['material'] or ocr_version != physical['version']:
            item = {
                'batch': physical.get('batch'), 'page': page,
                'ocr_material': ocr_material, 'ocr_version': ocr_version,
                'physical_material': physical['material'],
                'physical_version': physical['version'],
            }
            group['ocr_mislabels'].append(item)
            ocr_conflicts.append(item)
        if _outside_window(record, scan):
            group['outside_window'].append({
                'batch': physical.get('batch'), 'page': page,
                'received_at': int(scan.get('received_at', scan.get('signed_at', 0))),
            })
        if _unconfirmed_batch(record, scan):
            group['unconfirmed'].append({'batch': scan.get('batch'), 'page': page})
    return groups, ocr_conflicts


def _page_alternates(groups):
    """Other versions that hold a page the current version is missing."""
    by_material = {}
    for key, group in groups.items():
        by_material.setdefault(group['material'], {})[group['version']] = group
    result = {}
    for key, group in groups.items():
        alternates = {}
        for page in _missing_pages(group):
            found = []
            for version, other in sorted(by_material[group['material']].items()):
                if version == group['version'] or page not in other['pages']:
                    continue
                found.append({
                    'version': version,
                    'batch': other['page_sources'].get(page),
                    'sha256': other['pages'][page],
                })
            if found:
                alternates[page] = found
        result[key] = alternates
    return result


def _page_count(group):
    counts = {count for count in group['page_counts'] if count}
    if not counts:
        return 0, False
    return max(counts), len(counts) > 1


def _missing_pages(group):
    count, _ = _page_count(group)
    if not count:
        return []
    return [page for page in range(1, count + 1) if page not in group['pages']]


def _revoked_sets(record):
    versions = set()
    receipts = set()
    for item in record.get('revocations', []):
        if item.get('receipt'):
            receipts.add(item['receipt'])
        if item.get('material') is not None:
            versions.add((item['material'], int(item.get('version', 1))))
    return versions, receipts


def _analyze(groups, record):
    """Attach page-level issues and completeness to every version group."""
    revoked_versions, _ = _revoked_sets(record)
    alternates = _page_alternates(groups)
    for key, group in groups.items():
        count, count_conflict = _page_count(group)
        group['page_count'] = count
        group['missing_pages'] = _missing_pages(group)
        group['alternates'] = alternates.get(key, {})
        group['revoked'] = key in revoked_versions
        issues = []
        if not count:
            issues.append(_issue('page-count-unknown', group['material'], group['version'],
                                  None, 'declared page count is missing; completeness is unprovable'))
        if count_conflict:
            issues.append(_issue('page-count-mismatch', group['material'], group['version'],
                                  None, f'batches disagree on page count: {sorted(group["page_counts"])}'))
        for page in sorted(group['conflicts']):
            issues.append(_issue('conflicting-page', group['material'], group['version'], page,
                                  f'page {page} has conflicting digests {sorted(group["conflicts"][page])}',
                                  digests=sorted(group['conflicts'][page])))
        for page in group['missing_pages']:
            evidence = {
                'available_pages': sorted(group['pages']),
                'alternate_versions': group['alternates'].get(page, []),
            }
            issues.append(_issue('missing-page', group['material'], group['version'], page,
                                  f'page {page} of {group["material"]} v{group["version"]} is absent',
                                  **evidence))
            if group['alternates'].get(page):
                versions = [item['version'] for item in group['alternates'][page]]
                issues.append(_issue(
                    'cross-version-page', group['material'], group['version'], page,
                    f'page {page} exists only under version(s) {versions}; assembling it into '
                    f'v{group["version"]} would create a cross-version pseudo-document',
                    alternate_versions=group['alternates'][page]))
        for page in sorted(set(group['pages']) - set(range(1, count + 1))) if count else []:
            issues.append(_issue('out-of-range-page', group['material'], group['version'], page,
                                  f'page {page} exceeds declared page count {count}'))
        for item in group['ocr_mislabels']:
            issues.append(_issue('ocr-mislabel', group['material'], group['version'], item['page'],
                                  f'OCR labelled page {item["page"]} as {item["ocr_material"]} '
                                  f'v{item["ocr_version"]} but its upload batch is '
                                  f'{item["physical_material"]} v{item["physical_version"]}',
                                  ocr_material=item['ocr_material'],
                                  ocr_version=item['ocr_version'],
                                  physical_material=item['physical_material'],
                                  physical_version=item['physical_version'],
                                  batch=item.get('batch')))
        if group['revoked']:
            issues.append(_issue('revoked-version', group['material'], group['version'], None,
                                  f'{group["material"]} v{group["version"]} was revoked'))
        for item in sorted(group['outside_window'], key=lambda entry: entry['page']):
            issues.append(_issue('outside-intake-window', group['material'], group['version'],
                                  item['page'],
                                  f'page {item["page"]} was received at {item["received_at"]}, '
                                  f'outside every declared intake window',
                                  batch=item['batch'], received_at=item['received_at']))
        for item in sorted(group['unconfirmed'], key=lambda entry: entry['page']):
            issues.append(_issue('unconfirmed-batch', group['material'], group['version'],
                                  item['page'],
                                  f'page {item["page"]} has no confirmed upload batch; '
                                  f'it cannot prove an agreed version',
                                  batch=item['batch']))
        group['issues'] = issues
        group['complete'] = (bool(count) and not group['missing_pages']
                             and not group['conflicts'] and not count_conflict
                             and not group['revoked'] and not group['outside_window']
                             and not group['unconfirmed']
                             and not any(x['code'] == 'out-of-range-page' for x in issues)
                             and not any(x['code'] == 'page-count-unknown' for x in issues))


def _effective_receipts(record, groups, revoked_receipts):
    """Latest receipt per role is effective; older ones are retained history."""
    by_role = {}
    for ref in record.get('receipts', []):
        by_role.setdefault(ref.get('role', 'receipt'), []).append(ref)
    effective, historical = [], []
    for role in sorted(by_role):
        ordered = sorted(by_role[role],
                         key=lambda ref: (int(ref.get('signed_at', 0)), str(ref['id'])))
        latest = ordered[-1]
        effective.append(_resolve_receipt(latest, groups, revoked_receipts))
        for old in ordered[:-1]:
            item = _resolve_receipt(old, groups, revoked_receipts)
            item['superseded_by'] = latest['id']
            historical.append(item)
    return effective, historical


def _resolve_receipt(ref, groups, revoked_receipts):
    key = (ref['material'], int(ref['version']))
    group = groups.get(key)
    resolved, reason = True, 'resolved-in-snapshot'
    if ref['id'] in revoked_receipts:
        resolved, reason = False, 'receipt-revoked'
    elif group is None:
        resolved, reason = False, 'target-not-captured'
    elif group['revoked']:
        resolved, reason = False, 'target-revoked'
    elif not group['complete']:
        resolved, reason = False, 'target-incomplete'
        if group['missing_pages']:
            reason = 'target-missing-pages:' + ','.join(map(str, group['missing_pages']))
    return {
        'id': ref['id'], 'role': ref.get('role', 'receipt'),
        'material': ref['material'], 'version': int(ref['version']),
        'signed_at': int(ref.get('signed_at', 0)),
        'resolved': resolved, 'reason': reason,
    }


def _document(group):
    return {
        'material': group['material'], 'version': group['version'],
        'pages': sorted(group['pages']),
        'sha256': {str(page): group['pages'][page] for page in sorted(group['pages'])},
    }


def _history_material(group):
    item = {
        'material': group['material'], 'version': group['version'],
        'pages': sorted(group['pages']), 'missing_pages': group['missing_pages'],
        'complete': group['complete'], 'revoked': group['revoked'],
        'issues': [entry['code'] for entry in group['issues']],
    }
    return item


def evaluate(record):
    """Full closing verdict with page, version and receipt evidence."""
    record = capture.normalize(record)
    required = list(dict.fromkeys(record.get('required', [])))
    groups, ocr_conflicts = _groups(record)
    _analyze(groups, record)
    revoked_versions, revoked_receipts = _revoked_sets(record)
    effective, historical = _effective_receipts(record, groups, revoked_receipts)

    issues = []
    for ref in effective:
        if not ref['resolved']:
            issues.append(_issue('dangling-reference', ref['material'], ref['version'], None,
                                  f'effective receipt {ref["id"]} does not resolve in this snapshot: '
                                  f'{ref["reason"]}', receipt=ref['id']))
        if ref['reason'] in ('receipt-revoked', 'target-revoked'):
            issues.append(_issue('receipt-revoked', ref['material'], ref['version'], None,
                                  f'effective receipt {ref["id"]} is revoked', receipt=ref['id']))

    required_materials = [kind for kind in required if kind not in _RECEIPT_ROLES]
    for kind in required_materials:
        matching = [g for g in groups.values() if g['material'] == kind]
        if not matching:
            issues.append(_issue('required-kind-missing', kind, None, None,
                                  f'required material {kind!r} has no captured version'))
            continue
        if not any(group['complete'] for group in matching):
            issues.append(_issue('required-kind-missing', kind, None, None,
                                  f'no complete version of required material {kind!r}'))

    # Precise page-level evidence for versions that matter: any version an
    # effective receipt points at, plus every version of a required kind that
    # has no complete alternative.  Both missing pages and cross-version
    # alternates are emitted, so a caller can see exactly why closure failed.
    # An incomplete older version does not block a case that has a complete
    # newer version, so its defects are not emitted as blocking issues.
    relevant = {(ref['material'], ref['version']) for ref in effective}
    for kind in required_materials:
        versions = [g for g in groups.values() if g['material'] == kind]
        if versions and not any(group['complete'] for group in versions):
            relevant.update((group['material'], group['version']) for group in versions)
    emitted = set()
    for key in sorted(relevant, key=lambda item: (str(item[0]), item[1])):
        group = groups.get(key)
        if group is None or group['complete']:
            continue
        for entry in group['issues']:
            signature = (entry['code'], entry['material'], entry['version'], entry['page'])
            if signature in emitted:
                continue
            emitted.add(signature)
            issues.append(entry)

    receipt_kinds = [kind for kind in required if kind in _RECEIPT_ROLES]
    for kind in receipt_kinds:
        if not any(ref['role'] == kind for ref in effective):
            issues.append(_issue('required-kind-missing', kind, None, None,
                                  f'required kind {kind!r} has no captured receipt reference'))
    issues.extend(_identity_checks(record, groups, effective))
    issues.extend(_target_checks(record, groups))

    # Content is every proven-complete version that actually supports this
    # closure: a required material, or a version an effective receipt cites.
    # Other complete versions (and every incomplete/revoked one) are retained
    # as history, so a stray complete upload never widens the sealed manifest.
    evidence_materials = set(required_materials) | {
        ref['material'] for ref in effective if ref['resolved']}
    content, history = [], []
    content_keys = set()
    for group in sorted(groups.values(), key=lambda item: (item['material'], item['version'])):
        if group['complete'] and group['material'] in evidence_materials:
            content.append(_document(group))
            content_keys.add((group['material'], group['version']))
    for group in sorted(groups.values(), key=lambda item: (item['material'], item['version'])):
        if (group['material'], group['version']) not in content_keys:
            history.append(_history_material(group))

    # Version-level defects are kept as evidence on the version record but do
    # not block closure when another version of the same material is complete.
    reasons = {_issue_reason(issue['code']) for issue in issues}
    if not issues:
        state, decision = 'sealed', 'sealed'
    else:
        state = 'open'
        decision = next((reason for reason in _REASON_ORDER if reason in reasons),
                        sorted(reasons)[0])

    sealed_content = content if state == 'sealed' else []
    return {
        'case_id': record['case_id'],
        'state': state,
        'decision': decision,
        'required': required,
        'kind_list_complete': all(kind in record.get('documents', []) for kind in required),
        'issues': issues,
        'ocr_conflicts': ocr_conflicts,
        'versions': [
            {'material': g['material'], 'version': g['version'], 'batch': g['batches'],
             'page_count': g['page_count'], 'pages': sorted(g['pages']),
             'missing_pages': g['missing_pages'], 'complete': g['complete'],
             'revoked': g['revoked'], 'issues': g['issues']}
            for g in sorted(groups.values(), key=lambda item: (item['material'], item['version']))
        ],
        'content': sealed_content,
        'documents': sealed_content,
        'history': {'materials': history,
                    'receipts': historical},
        'receipts': effective,
        'effective_receipts': [ref['id'] for ref in effective if ref['resolved']],
        'corrections': record.get('corrections', []),
        'revocations': record.get('revocations', []),
        'windows': record.get('windows', []),
    }


def _snapshot_payload(verdict):
    """Stable closure projection: provenance-only changes do not reseal."""
    return {
        'case_id': verdict['case_id'],
        'state': verdict['state'],
        'decision': verdict['decision'],
        'required': verdict['required'],
        'issues': [
            {key: issue.get(key) for key in ('code', 'material', 'version', 'page', 'receipt')}
            for issue in verdict['issues']
        ],
        'documents': [
            {'material': doc['material'], 'version': doc['version'],
             'pages': doc['pages'], 'sha256': doc['sha256']}
            for doc in verdict['documents']
        ],
        'effective_receipts': verdict['effective_receipts'],
        'history_receipts': [
            {'id': ref['id'], 'material': ref['material'], 'version': ref['version'],
             'resolved': ref['resolved'], 'reason': ref['reason'],
             'superseded_by': ref.get('superseded_by')}
            for ref in verdict['history']['receipts']
        ],
        'history_materials': [
            {'material': item['material'], 'version': item['version'],
             'pages': item['pages'], 'missing_pages': item['missing_pages'],
             'complete': item['complete']}
            for item in verdict['history']['materials']
        ],
    }


def seal(record, con):
    """Persist original pages and a new immutable snapshot when closure changed."""
    verdict = evaluate(record)
    capture.store(con, record)
    info = capture.seal(con, verdict, _snapshot_payload(verdict))
    return verdict, info


def complete(record):
    if has_capture(record):
        return evaluate(record)['state'] == 'sealed'
    return all(kind in record.get('documents', []) for kind in record['required'])


def evidence(record):
    return evaluate(record) if has_capture(record) else {
        'case_id': record['case_id'], 'state': 'sealed',
        'decision': 'legacy-kind-complete', 'issues': [], 'documents': [],
    }
