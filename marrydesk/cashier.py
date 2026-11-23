"""Cross-dossier bank clearing engine.

A bank transaction is the conserved source of funds. Payer instructions
(remittance memos / reallocation orders) only split a source within its net
capacity (gross minus confirmed reversals); reversals always reference the
original bank transaction and the exact share they draw back. Replay is by
posted instant, so upload order cannot create or destroy money:

* the same bank transaction id uploaded under several file names counts once;
* a reversal arriving before its source is still a reversal of that source,
  never an independent refund;
* every source cent is either an evidenced net flow to one dossier, a linked
  reversal, or a suspense (unallocated) balance — never guessed by upload
  order or dossier id.
"""
from . import bank_capture as bc


class ClearingError(ValueError):
    """An instruction or reversal violates source-fund conservation."""


def net_cents(record):
    """Legacy single-dossier view, kept compatible with cashier_normal.json."""
    if isinstance(record, dict) and 'captures' in record:
        return flow_graph(record)['net_bank_cents']
    return sum(item['cents'] for item in record.get('payments', [])) - sum(
        item['cents'] for item in record.get('refunds', []))


def flow_graph(record):
    """Return the net flow graph between bank transactions and dossiers."""
    book = bc.normalize(record['captures'])
    sources = [_clear_source(book, source) for source in book['transactions']]

    edges = [edge for source in sources for edge in source['edges']]
    pending = [dict(item) for item in book['reversals'] if item['state'] != 'linked']
    linked_reversed = sum(
        item['cents'] for item in book['reversals'] if item['state'] == 'linked')
    gross_total = sum(source['gross_cents'] for source in sources)
    net_bank = gross_total - linked_reversed
    unallocated_total = sum(source['unallocated_cents'] for source in sources)
    allocated_total = sum(edge['net_cents'] for edge in edges)

    # Hard conservation invariant: gross credits == net dossier flows +
    # linked reversals + suspense. No bank cent may fund two dossier balances.
    if gross_total != allocated_total + linked_reversed + unallocated_total:
        raise ClearingError('conservation-broken')
    if any(source['unallocated_cents'] < 0 for source in sources):
        raise ClearingError('negative-unallocated')

    return {
        'transactions': sources,
        'edges': edges,
        'pending_reversals': pending,
        'unallocated_cents': unallocated_total,
        'gross_bank_cents': gross_total,
        'reversed_cents': linked_reversed,
        'net_bank_cents': net_bank,
    }


def case_payments(record):
    """Per-dossier evidenced payments/refunds derived from the flow graph."""
    graph = flow_graph(record)
    cases = {}
    for edge in graph['edges']:
        bucket = cases.setdefault(edge['case_id'], {
            'case_id': edge['case_id'], 'payments': [], 'refunds': [],
            'gross_paid_cents': 0, 'reversed_cents': 0, 'net_cents': 0})
        bucket['payments'].append({
            'transaction_id': edge['transaction_id'],
            'instruction_id': edge['instruction_id'],
            'payer': edge['payer'], 'cents': edge['gross_cents']})
        bucket['gross_paid_cents'] += edge['gross_cents']
        if edge['reversed_cents']:
            bucket['refunds'].append({
                'transaction_id': edge['transaction_id'],
                'reversal_ids': list(edge['reversal_ids']),
                'cents': edge['reversed_cents']})
            bucket['reversed_cents'] += edge['reversed_cents']
        bucket['net_cents'] += edge['net_cents']
    return cases


def _clear_source(book, source):
    txn_id = source['transaction_id']
    own_instructions = {item['instruction_id'] for item in book['instructions']
                        if item.get('transaction_id') == txn_id}
    events = []
    for item in book['instructions']:
        if item['kind'] in bc.INSTRUCTION_KINDS and item.get('transaction_id') == txn_id:
            events.append(('instruction', item))
        elif item['kind'] == bc.MEMO_WITHDRAWAL and item.get('supersedes') in own_instructions:
            events.append(('withdrawal', item))
    for item in book['reversals']:
        if item['state'] == 'linked' and item['original_transaction_id'] == txn_id:
            events.append(('reversal', item))
    events.sort(key=lambda pair: (
        pair[1].get('posted_at') or pair[1].get('received_at') or '',
        pair[1].get('instruction_id') or pair[1].get('reversal_id') or ''))

    current = None
    charged = {}
    total_reversed = 0
    reversal_log = []
    for kind, event in events:
        if kind == 'instruction':
            if event['payer'] != source['payer']:
                raise ClearingError('payer-mismatch')
            if event.get('supersedes') is None:
                if current is not None:
                    raise ClearingError('instruction-conflict')
            elif event['supersedes'] not in own_instructions:
                raise ClearingError('instruction-chain-broken')
            capacity = source['gross_cents'] - total_reversed
            if sum(split['cents'] for split in event['splits']) > capacity:
                raise ClearingError('allocation-exceeds-net-source')
            current = event
            charged = {}
        elif kind == 'withdrawal':
            if current is None or event['supersedes'] != current['instruction_id']:
                raise ClearingError('instruction-chain-broken')
            if event['payer'] != source['payer']:
                raise ClearingError('payer-mismatch')
            current = None
            charged = {}
        else:
            if event['payer'] != source['payer']:
                raise ClearingError('payer-mismatch')
            total_reversed += event['cents']
            if total_reversed > source['gross_cents']:
                raise ClearingError('reversal-limit')
            ref_case = event.get('reference_case_id')
            if current is None or ref_case not in {split['case_id'] for split in current['splits']}:
                raise ClearingError('reversal-no-source-share')
            share = next(split['cents'] for split in current['splits']
                         if split['case_id'] == ref_case)
            if charged.get(ref_case, 0) + event['cents'] > share:
                raise ClearingError('reversal-limit')
            charged[ref_case] = charged.get(ref_case, 0) + event['cents']
            reversal_log.append(dict(event))

    edges = []
    if current is not None:
        for split in current['splits']:
            case_id = split['case_id']
            reversed_here = charged.get(case_id, 0)
            edge_reversals = [item['reversal_id'] for item in reversal_log
                              if item.get('reference_case_id') == case_id]
            edges.append({
                'transaction_id': txn_id,
                'instruction_id': current['instruction_id'],
                'payer': source['payer'],
                'case_id': case_id,
                'gross_cents': split['cents'],
                'reversed_cents': reversed_here,
                'reversal_ids': edge_reversals,
                'net_cents': split['cents'] - reversed_here})

    net_allocated = sum(edge['net_cents'] for edge in edges)
    unallocated = source['gross_cents'] - total_reversed - net_allocated
    return {
        'transaction_id': txn_id,
        'payer': source['payer'],
        'gross_cents': source['gross_cents'],
        'reversed_cents': total_reversed,
        'available_cents': source['gross_cents'] - total_reversed,
        'allocated_cents': net_allocated,
        'unallocated_cents': unallocated,
        'active_instruction_id': current['instruction_id'] if current else None,
        'reversals': reversal_log,
        'edges': edges,
        'evidence': [dict(item) for item in source['evidence']]}
