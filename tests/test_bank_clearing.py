import json
from pathlib import Path

import pytest

from marrydesk import bank_capture, cashier, settlement

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'bank-split.json'
CASHIER_FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'cashier_normal.json'


def load_record():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def edge(graph, case_id):
    rows = [edge for edge in graph['edges'] if edge['case_id'] == case_id]
    assert len(rows) == 1
    return rows[0]


def case_view(result, case_id):
    rows = [case for case in result['cases'] if case['case_id'] == case_id]
    assert len(rows) == 1
    return rows[0]


def test_legacy_single_dossier_record_still_1000_cents():
    record = json.loads(CASHIER_FIXTURE.read_text(encoding='utf-8'))
    assert cashier.net_cents(record) == 1000
    assert settlement.balance(record) == {'case_id': 'COUPLE-1', 'balance_cents': 1000}


def test_reversal_arriving_before_source_is_still_linked_to_b1():
    record = load_record()
    order = [item['capture_id'] for item in
             sorted(record['captures'], key=lambda item: item['received_at'])]
    assert order[0] == 'CAP-R1'  # R1 先于 B1 离线采录

    book = bank_capture.normalize(record['captures'])
    r1 = next(item for item in book['reversals'] if item['reversal_id'] == 'CAP-R1')
    assert r1['original_transaction_id'] == 'B1'
    assert r1['state'] == 'linked'
    assert not book['pending_reversals']


def test_duplicate_bank_receipt_file_names_do_not_double_count_b1():
    record = load_record()
    graph = cashier.flow_graph(record)
    b1 = next(item for item in graph['transactions'] if item['transaction_id'] == 'B1')
    # 两个文件名 + 一份柜台收据，都是同一笔银行交易 B1
    assert len(b1['evidence']) == 3
    assert b1['gross_cents'] == 1500
    assert graph['gross_bank_cents'] == 1500
    assert graph['reversed_cents'] == 400
    assert graph['net_bank_cents'] == 1100


def test_initial_clearing_state_x_1000_y_100_y_shortfall_400():
    record = load_record()
    graph = cashier.flow_graph(record)
    assert edge(graph, 'X')['net_cents'] == 1000
    assert edge(graph, 'Y')['net_cents'] == 100
    assert edge(graph, 'Y')['reversed_cents'] == 400
    assert edge(graph, 'Y')['reversal_ids'] == ['CAP-R1']
    assert graph['unallocated_cents'] == 0

    result = settlement.settle(record)
    assert case_view(result, 'X')['settled_cents'] == 1000
    assert case_view(result, 'X')['shortfall_cents'] == 0
    assert case_view(result, 'Y')['settled_cents'] == 100
    assert case_view(result, 'Y')['shortfall_cents'] == 400
    assert result['net_bank_cents'] == 1100
    assert result['total_shortfall_cents'] == 400


def test_flow_graph_conservation_invariant_holds():
    record = load_record()
    graph = cashier.flow_graph(record)
    allocated = sum(item['net_cents'] for item in graph['edges'])
    assert graph['gross_bank_cents'] == allocated + graph['reversed_cents'] + graph['unallocated_cents']
    # 每案卷局部收减退与净流图一致
    payments = cashier.case_payments(record)
    assert payments['X']['net_cents'] == 1000
    assert payments['Y']['net_cents'] == 100
    assert sum(item['cents'] for item in payments['Y']['refunds']) == 400


def test_bank_transaction_id_dedup_rejects_conflicting_amounts():
    record = load_record()
    record['captures'][1]['gross_cents'] = 9999
    with pytest.raises(bank_capture.BankCaptureError, match='transaction-conflict'):
        bank_capture.normalize(record['captures'])


def test_evidence_book_keeps_page_instants_and_withdrawal_chain_fields():
    record = load_record()
    book = bank_capture.normalize(record['captures'])
    by_id = {item['capture_id']: item for item in book['captures']}
    assert by_id['CAP-B1-A']['source_page'] == 'B1-page-001'
    assert by_id['CAP-B1-A']['posted_at'] == '2026-09-01T09:00:00Z'
    assert by_id['CAP-R1']['original_transaction_id'] == 'B1'
    assert by_id['CAP-C1']['kind'] == 'counter_receipt'


def test_unevidenced_remittance_stays_in_suspense_not_guessed():
    record = {
        'case_dues': [['X', 1000]],
        'captures': [{
            'kind': 'bank_receipt', 'capture_id': 'CAP-B9', 'transaction_id': 'B9',
            'payer': 'PAYER-9', 'gross_cents': 700,
            'source_page': 'p9', 'posted_at': '2026-09-10T08:00:00Z',
            'received_at': '2026-09-10T08:00:00Z'}]}
    graph = cashier.flow_graph(record)
    assert graph['net_bank_cents'] == 700
    assert graph['unallocated_cents'] == 700
    assert graph['edges'] == []
    result = settlement.settle(record)
    assert result['suspense_account']['unallocated_cents'] == 700
    assert case_view(result, 'X')['shortfall_cents'] == 1000


def test_memo_withdrawal_returns_funds_to_suspense():
    record = load_record()
    record['captures'].append({
        'kind': 'memo_withdrawal', 'capture_id': 'MEMO-B1-W1', 'supersedes': 'MEMO-B1-1',
        'payer': 'PAYER-1', 'source_page': 'W-page-1',
        'posted_at': '2026-09-05T09:00:00Z', 'received_at': '2026-09-05T12:00:00Z'})
    graph = cashier.flow_graph(record)
    b1 = next(item for item in graph['transactions'] if item['transaction_id'] == 'B1')
    assert b1['active_instruction_id'] is None
    assert graph['edges'] == []
    # R1 已确认，净 1100 全部回到待分配账户
    assert graph['unallocated_cents'] == 1100
    assert graph['net_bank_cents'] == 1100


def test_reallocation_to_z_must_withdraw_old_clearing_first():
    # 未撤回旧附言就直接改配 Z：同一笔银行金额会同时支撑两套案卷余额
    record = load_record()
    record['captures'].append({
        'kind': 'reallocation_order', 'capture_id': 'ORD-Z-1',
        'transaction_id': 'B1', 'payer': 'PAYER-1', 'gross_cents': 1500,
        'splits': [{'case_id': 'Z', 'cents': 1500}],
        'source_page': 'z-page-1',
        'posted_at': '2026-09-06T09:00:00Z', 'received_at': '2026-09-06T09:00:00Z'})
    with pytest.raises(cashier.ClearingError, match='instruction-conflict'):
        cashier.flow_graph(record)


def test_reallocation_to_z_after_withdrawal_is_limited_by_r1_remaining_share():
    record = load_record()
    record['captures'].extend([
        {'kind': 'memo_withdrawal', 'capture_id': 'MEMO-B1-W1', 'supersedes': 'MEMO-B1-1',
         'payer': 'PAYER-1', 'posted_at': '2026-09-05T09:00:00Z',
         'received_at': '2026-09-05T12:00:00Z'},
        {'kind': 'reallocation_order', 'capture_id': 'ORD-Z-1', 'transaction_id': 'B1',
         'payer': 'PAYER-1', 'gross_cents': 1500,
         'splits': [{'case_id': 'Z', 'cents': 1500}],
         'posted_at': '2026-09-06T09:00:00Z', 'received_at': '2026-09-06T09:00:00Z'}])
    # B1 净额只剩 1100（R1 已冲正 400），1500 的改配超过剩余可冲份额
    with pytest.raises(cashier.ClearingError, match='allocation-exceeds-net-source'):
        cashier.flow_graph(record)

    record['captures'][-1]['splits'] = [{'case_id': 'Z', 'cents': 1100}]
    record['captures'][-1]['gross_cents'] = 1100
    graph = cashier.flow_graph(record)
    assert graph['unallocated_cents'] == 0
    assert edge(graph, 'Z')['net_cents'] == 1100
    assert not any(edge['case_id'] in ('X', 'Y') for edge in graph['edges'])
    result = settlement.settle(record)
    assert case_view(result, 'Z')['settled_cents'] == 1100
    assert case_view(result, 'X')['shortfall_cents'] == 1000
    assert case_view(result, 'Y')['shortfall_cents'] == 500


def test_path_a_fresh_t1_400_to_y_settles_both_cases():
    record = load_record()
    record['captures'].extend([
        {'kind': 'bank_receipt', 'capture_id': 'CAP-T1', 'transaction_id': 'T1',
         'payer': 'PAYER-1', 'gross_cents': 400, 'source_page': 'T1-page-1',
         'posted_at': '2026-09-07T09:00:00Z', 'received_at': '2026-09-07T10:00:00Z'},
        {'kind': 'remittance_memo', 'capture_id': 'MEMO-T1-1', 'transaction_id': 'T1',
         'payer': 'PAYER-1', 'gross_cents': 400,
         'splits': [{'case_id': 'Y', 'cents': 400}],
         'posted_at': '2026-09-07T09:05:00Z', 'received_at': '2026-09-07T10:05:00Z'}])
    graph = cashier.flow_graph(record)
    assert graph['net_bank_cents'] == 1500
    assert edge(graph, 'X')['net_cents'] == 1000
    assert sum(edge['net_cents'] for edge in graph['edges'] if edge['case_id'] == 'Y') == 500
    result = settlement.settle(record)
    assert case_view(result, 'X')['shortfall_cents'] == 0
    assert case_view(result, 'Y')['shortfall_cents'] == 0
    assert result['total_shortfall_cents'] == 0
    assert result['suspense_account']['unallocated_cents'] == 0


def test_path_b_signed_reallocation_moves_400_from_x_to_y():
    record = load_record()
    record['captures'].extend([
        {'kind': 'memo_withdrawal', 'capture_id': 'MEMO-B1-W1', 'supersedes': 'MEMO-B1-1',
         'payer': 'PAYER-1', 'posted_at': '2026-09-05T09:00:00Z',
         'received_at': '2026-09-05T12:00:00Z'},
        {'kind': 'reallocation_order', 'capture_id': 'ORD-B1-2', 'transaction_id': 'B1',
         'supersedes': 'MEMO-B1-1', 'payer': 'PAYER-1', 'gross_cents': 1100,
         'splits': [{'case_id': 'X', 'cents': 600}, {'case_id': 'Y', 'cents': 500}],
         'posted_at': '2026-09-06T09:00:00Z', 'received_at': '2026-09-06T09:00:00Z'}])
    graph = cashier.flow_graph(record)
    # 没有新增资金，银行净额仍是 1100；R1 不超 B1 相关份额，新份额不被旧冲正二次扣减
    assert graph['net_bank_cents'] == 1100
    assert edge(graph, 'X')['net_cents'] == 600
    assert edge(graph, 'Y')['net_cents'] == 500
    b1 = next(item for item in graph['transactions'] if item['transaction_id'] == 'B1')
    assert b1['reversed_cents'] == 400
    result = settlement.settle(record)
    assert case_view(result, 'Y')['shortfall_cents'] == 0
    assert case_view(result, 'X')['shortfall_cents'] == 400
    assert result['total_shortfall_cents'] == 400
    payer = next(item for item in result['payers'] if item['payer'] == 'PAYER-1')
    assert payer['net_balance_cents'] == 1100


def test_reversal_cannot_exceed_referenced_b1_share():
    record = load_record()
    record['captures'].append({
        'kind': 'reversal_notice', 'capture_id': 'CAP-R2', 'transaction_id': 'R2',
        'original_transaction_id': 'B1', 'payer': 'PAYER-1', 'case_id': 'Y',
        'cents': 200, 'source_page': 'R2-page-1',
        'posted_at': '2026-09-03T10:00:00Z', 'received_at': '2026-09-04T13:00:00Z'})
    # Y 份额只有 500，R1 已冲 400，再冲 200 超过该份额
    with pytest.raises(cashier.ClearingError, match='reversal-limit'):
        cashier.flow_graph(record)


def test_reversal_without_a_live_source_share_is_rejected_not_booked_as_refund():
    record = load_record()
    record['captures'].extend([
        {'kind': 'memo_withdrawal', 'capture_id': 'MEMO-B1-W1', 'supersedes': 'MEMO-B1-1',
         'payer': 'PAYER-1', 'posted_at': '2026-09-05T09:00:00Z',
         'received_at': '2026-09-05T12:00:00Z'},
        {'kind': 'reversal_notice', 'capture_id': 'CAP-R3', 'transaction_id': 'R3',
         'original_transaction_id': 'B1', 'payer': 'PAYER-1', 'case_id': 'Y',
         'cents': 10, 'posted_at': '2026-09-05T15:00:00Z',
         'received_at': '2026-09-05T16:00:00Z'}])
    with pytest.raises(cashier.ClearingError, match='reversal-no-source-share'):
        cashier.flow_graph(record)


def test_pending_reversal_is_reported_when_source_not_yet_captured():
    record = {
        'case_dues': [],
        'captures': [{
            'kind': 'reversal_notice', 'capture_id': 'CAP-RX', 'transaction_id': 'RX',
            'original_transaction_id': 'BX', 'payer': 'PAYER-2', 'case_id': 'X',
            'cents': 50, 'posted_at': '2026-09-08T08:00:00Z',
            'received_at': '2026-09-08T08:00:00Z'}]}
    graph = cashier.flow_graph(record)
    assert [item['reversal_id'] for item in graph['pending_reversals']] == ['CAP-RX']
    assert graph['net_bank_cents'] == 0
    result = settlement.settle(record)
    assert result['suspense_account']['pending_reversals'] == ['CAP-RX']


def test_partial_memo_leaves_evidenced_remainder_in_suspense():
    record = load_record()
    record['captures'][0]['cents'] = 100  # R1 冲 Y 份额内的 100
    record['captures'][4]['splits'] = [
        {'case_id': 'X', 'cents': 1000}, {'case_id': 'Y', 'cents': 200}]
    graph = cashier.flow_graph(record)
    # 毛 1500 - 冲 100 - 已配净 1100 (X1000 + Y100) = 300 待分配，禁止猜分
    assert graph['unallocated_cents'] == 300
    assert edge(graph, 'X')['net_cents'] == 1000
    assert edge(graph, 'Y')['net_cents'] == 100
    assert graph['gross_bank_cents'] == (
        sum(item['net_cents'] for item in graph['edges'])
        + graph['reversed_cents'] + graph['unallocated_cents'])


def test_duplicate_reversal_transaction_id_is_rejected():
    record = load_record()
    record['captures'].append(dict(record['captures'][0]))
    record['captures'][-1]['capture_id'] = 'CAP-R1-COPY'
    record['captures'][-1]['received_at'] = '2026-09-04T18:00:00Z'
    with pytest.raises(bank_capture.BankCaptureError, match='duplicate-reversal-id'):
        bank_capture.normalize(record['captures'])


def test_supersedes_cannot_reach_an_instruction_of_another_transaction():
    record = {
        'case_dues': [['X', 1000], ['Y', 900]],
        'captures': [
            {'kind': 'bank_receipt', 'capture_id': 'CB1', 'transaction_id': 'B1',
             'payer': 'P1', 'gross_cents': 1000,
             'posted_at': '2026-09-01T09:00:00Z', 'received_at': '2026-09-01T09:00:00Z'},
            {'kind': 'remittance_memo', 'capture_id': 'M1', 'transaction_id': 'B1',
             'payer': 'P1', 'gross_cents': 1000, 'splits': [{'case_id': 'X', 'cents': 1000}],
             'posted_at': '2026-09-01T09:10:00Z', 'received_at': '2026-09-01T09:10:00Z'},
            {'kind': 'bank_receipt', 'capture_id': 'CT1', 'transaction_id': 'T1',
             'payer': 'P1', 'gross_cents': 900,
             'posted_at': '2026-09-02T09:00:00Z', 'received_at': '2026-09-02T09:00:00Z'},
            {'kind': 'reallocation_order', 'capture_id': 'M2', 'transaction_id': 'T1',
             'supersedes': 'M1', 'payer': 'P1', 'gross_cents': 900,
             'splits': [{'case_id': 'Y', 'cents': 900}],
             'posted_at': '2026-09-02T09:10:00Z', 'received_at': '2026-09-02T09:10:00Z'}]}
    with pytest.raises(cashier.ClearingError, match='instruction-chain-broken'):
        cashier.flow_graph(record)


def test_splits_must_not_exceed_source_gross_and_chain_payer_must_match():
    record = load_record()
    record['captures'][4]['splits'][0]['cents'] = 1100
    with pytest.raises(bank_capture.BankCaptureError, match='split-gross-mismatch'):
        bank_capture.normalize(record['captures'])

    record = load_record()
    record['captures'].append({
        'kind': 'memo_withdrawal', 'capture_id': 'MEMO-B1-W2', 'supersedes': 'MEMO-B1-1',
        'payer': 'OTHER-PAYER',
        'posted_at': '2026-09-05T09:00:00Z', 'received_at': '2026-09-05T12:00:00Z'})
    with pytest.raises(bank_capture.BankCaptureError, match='payer-chain-mismatch'):
        bank_capture.normalize(record['captures'])
