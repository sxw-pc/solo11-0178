"""Settlement views over the cross-dossier bank clearing graph."""
from . import cashier


def balance(record):
    """Legacy single-dossier balance, kept compatible with cashier_normal.json."""
    if isinstance(record, dict) and 'captures' in record:
        raise ValueError('use-settle-for-bank-record')
    return {'case_id': record['case_id'], 'balance_cents': cashier.net_cents(record)}


def settle(record):
    """Per-dossier settled/shortfall amounts and per-payer net balances."""
    graph = cashier.flow_graph(record)
    payments = cashier.case_payments(record)
    dues = dict(record.get('case_dues', []))

    cases = []
    for case_id in sorted(set(payments) | set(dues)):
        bucket = payments.get(case_id)
        net = bucket['net_cents'] if bucket else 0
        due = int(dues.get(case_id, 0))
        cases.append({
            'case_id': case_id,
            'due_cents': due,
            'settled_cents': net,
            'shortfall_cents': max(0, due - net),
            'overpaid_cents': max(0, net - due),
            'payer': bucket['payments'][0]['payer'] if bucket else None,
            'payments': bucket['payments'] if bucket else [],
            'refunds': bucket['refunds'] if bucket else []})

    payers = {}
    for source in graph['transactions']:
        bucket = payers.setdefault(source['payer'], {
            'payer': source['payer'], 'gross_cents': 0, 'reversed_cents': 0,
            'allocated_cents': 0, 'unallocated_cents': 0, 'net_balance_cents': 0})
        bucket['gross_cents'] += source['gross_cents']
        bucket['reversed_cents'] += source['reversed_cents']
        bucket['allocated_cents'] += source['allocated_cents']
        bucket['unallocated_cents'] += source['unallocated_cents']
        bucket['net_balance_cents'] += source['gross_cents'] - source['reversed_cents']

    return {
        'cases': cases,
        'payers': [payers[key] for key in sorted(payers)],
        'suspense_account': {
            'unallocated_cents': graph['unallocated_cents'],
            'pending_reversals': [item['reversal_id'] for item in graph['pending_reversals']]},
        'gross_bank_cents': graph['gross_bank_cents'],
        'reversed_cents': graph['reversed_cents'],
        'net_bank_cents': graph['net_bank_cents'],
        'allocated_cents': sum(edge['net_cents'] for edge in graph['edges']),
        'total_due_cents': sum(dues.values()),
        'total_shortfall_cents': sum(case['shortfall_cents'] for case in cases)}
