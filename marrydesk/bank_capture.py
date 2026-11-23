"""Offline bank evidence capture.

Captures four offline evidence kinds without any OCR or network service:

* ``bank_receipt``  : bank credit advice (an inbound bank transaction)
* ``counter_receipt``: in-counter receipt confirming a bank transaction
* ``remittance_memo``: payer message splitting one bank transaction between dossiers
* ``reversal_notice`` : bank reversal/refund, referencing the original transaction

Capture records keep the bank transaction id, original transaction references,
payer, dossier id, source page, posting/received instants, and the
withdrawal/supplement chain (``supersedes``). Dedup is by bank transaction id,
never by file name: the same B1 advice uploaded under two file names is one
transaction. Reversal notices are accepted before their source has arrived and
surface in ``pending_reversals`` so arrival order cannot change the ledger.
"""

BANK_RECEIPT = 'bank_receipt'
COUNTER_RECEIPT = 'counter_receipt'
REMEMBRANCE_MEMO = 'remittance_memo'
REALLOCATION_ORDER = 'reallocation_order'
REVERSAL_NOTICE = 'reversal_notice'
MEMO_WITHDRAWAL = 'memo_withdrawal'

CREDIT_KINDS = (BANK_RECEIPT, COUNTER_RECEIPT)
INSTRUCTION_KINDS = (REMEMBRANCE_MEMO, REALLOCATION_ORDER)

_REQUIRED = ('kind', 'capture_id', 'received_at')
_CREDIT_FIELDS = ('transaction_id', 'payer', 'gross_cents')
_INSTRUCTION_FIELDS = ('transaction_id', 'payer', 'splits', 'gross_cents')
_REVERSAL_FIELDS = ('transaction_id', 'original_transaction_id', 'payer', 'cents', 'case_id')


class BankCaptureError(ValueError):
    """Rejected evidence: malformed record, duplicate id or broken chain."""


def _require(capture, fields):
    for field in fields:
        value = capture.get(field)
        if value is None or value == '':
            raise BankCaptureError(f'missing-field:{field}')


def _check_splits(capture):
    splits = capture['splits']
    if not isinstance(splits, list) or not splits:
        raise BankCaptureError('invalid-splits')
    total = 0
    seen = set()
    for split in splits:
        case_id = split.get('case_id')
        cents = split.get('cents')
        if not case_id or not isinstance(cents, int) or cents < 0:
            raise BankCaptureError('invalid-splits')
        if case_id in seen:
            raise BankCaptureError('duplicate-split-case')
        seen.add(case_id)
        total += cents
    if total > capture['gross_cents']:
        raise BankCaptureError('split-gross-mismatch')


def _validate(capture):
    _require(capture, _REQUIRED)
    kind = capture['kind']
    if kind in CREDIT_KINDS:
        _require(capture, _CREDIT_FIELDS)
        if not isinstance(capture['gross_cents'], int) or capture['gross_cents'] <= 0:
            raise BankCaptureError('invalid-gross-cents')
    elif kind in INSTRUCTION_KINDS:
        _require(capture, _INSTRUCTION_FIELDS)
        _check_splits(capture)
    elif kind == REVERSAL_NOTICE:
        _require(capture, _REVERSAL_FIELDS)
        if not isinstance(capture['cents'], int) or capture['cents'] <= 0:
            raise BankCaptureError('invalid-reversal-cents')
    elif kind == MEMO_WITHDRAWAL:
        _require(capture, ('payer', 'supersedes'))
    else:
        raise BankCaptureError(f'unknown-kind:{kind}')


def normalize(captures):
    """Normalize raw capture dicts into an order-independent evidence book."""
    seen_capture_ids = set()
    ordered = []
    for capture in captures:
        _validate(capture)
        capture_id = capture['capture_id']
        if capture_id in seen_capture_ids:
            raise BankCaptureError(f'duplicate-capture-id:{capture_id}')
        seen_capture_ids.add(capture_id)
        ordered.append(capture)
    # Evidence is sorted by received instant for traceability only; ledger
    # correctness never depends on this order (replay uses posted_at).
    ordered.sort(key=lambda item: (item['received_at'], item['capture_id']))

    transactions = {}
    instructions = []
    reversals = {}
    for capture in ordered:
        kind = capture['kind']
        if kind in CREDIT_KINDS:
            txn_id = capture['transaction_id']
            existing = transactions.get(txn_id)
            if existing:
                if existing['gross_cents'] != capture['gross_cents'] or existing['payer'] != capture['payer']:
                    raise BankCaptureError(f'transaction-conflict:{txn_id}')
                existing['evidence'].append(_evidence(capture))
                continue
            transactions[txn_id] = {
                'transaction_id': txn_id,
                'payer': capture['payer'],
                'gross_cents': capture['gross_cents'],
                'kind': kind,
                'posted_at': capture.get('posted_at'),
                'evidence': [_evidence(capture)],
            }
        elif kind in INSTRUCTION_KINDS:
            instructions.append(_instruction(capture))
        elif kind == REVERSAL_NOTICE:
            if capture['transaction_id'] in reversals:
                raise BankCaptureError(f'duplicate-reversal-id:{capture["transaction_id"]}')
            entry = _reversal(capture)
            reversals[capture['transaction_id']] = entry
        else:  # memo withdrawal
            instructions.append(_withdrawal(capture))
    reversals = list(reversals.values())

    known_ids = set(transactions)
    instruction_ids = {item['instruction_id'] for item in instructions}
    for item in instructions:
        ref = item.get('supersedes')
        if ref is not None and ref not in instruction_ids:
            raise BankCaptureError(f'unknown-supersedes:{ref}')
    for item in reversals:
        if item['original_transaction_id'] not in known_ids:
            item['state'] = 'pending_source'

    _check_chain(instructions)
    pending = [dict(item) for item in reversals if item['state'] == 'pending_source']
    return {
        'book': 'bank_evidence',
        'transactions': [transactions[key] for key in sorted(transactions)],
        'instructions': instructions,
        'reversals': reversals,
        'pending_reversals': pending,
        'captures': [_evidence(item) for item in ordered],
    }


def _evidence(capture):
    return {
        'capture_id': capture['capture_id'],
        'kind': capture['kind'],
        'source_page': capture.get('source_page'),
        'file_name': capture.get('file_name'),
        'posted_at': capture.get('posted_at'),
        'received_at': capture['received_at'],
        'transaction_id': capture.get('transaction_id'),
        'original_transaction_id': capture.get('original_transaction_id'),
        'supersedes': capture.get('supersedes'),
        'payer': capture.get('payer'),
        'case_id': capture.get('case_id'),
    }


def _instruction(capture):
    return {
        'instruction_id': capture['capture_id'],
        'kind': capture['kind'],
        'payer': capture['payer'],
        'transaction_id': capture.get('transaction_id'),
        'gross_cents': capture['gross_cents'],
        'splits': [dict(split) for split in capture['splits']],
        'supersedes': capture.get('supersedes'),
        'posted_at': capture.get('posted_at'),
        'received_at': capture['received_at'],
    }


def _reversal(capture):
    return {
        'reversal_id': capture['capture_id'],
        'transaction_id': capture['transaction_id'],
        'original_transaction_id': capture['original_transaction_id'],
        'reference_case_id': capture.get('case_id'),
        'payer': capture['payer'],
        'cents': capture['cents'],
        'state': 'linked',
        'posted_at': capture.get('posted_at'),
        'received_at': capture['received_at'],
    }


def _withdrawal(capture):
    return {
        'instruction_id': capture['capture_id'],
        'kind': MEMO_WITHDRAWAL,
        'payer': capture['payer'],
        'supersedes': capture['supersedes'],
        'posted_at': capture.get('posted_at'),
        'received_at': capture['received_at'],
    }


def _check_chain(instructions):
    by_id = {item['instruction_id']: item for item in instructions}
    for item in instructions:
        ref = item.get('supersedes')
        if ref is None:
            continue
        target = by_id[ref]
        if target['payer'] != item['payer']:
            raise BankCaptureError('payer-chain-mismatch')
        if item['kind'] == MEMO_WITHDRAWAL and target['kind'] not in INSTRUCTION_KINDS:
            raise BankCaptureError('withdrawal-target-not-instruction')
