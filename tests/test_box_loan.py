import json
import tempfile
from pathlib import Path

from marrydesk import box, loan, box_capture

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'box-cross-room.json'
T1000 = '2026-11-23T10:00:00Z'
T1100 = '2026-11-23T11:00:00Z'


def load_fixture():
    data = json.loads(FIXTURE.read_text(encoding='utf-8'))
    events, stats = box_capture.normalize(data)
    return data, events, stats


def route_events(data, name):
    events, _ = box_capture.normalize(
        {'boxes': data['boxes'],
         'events': data['events'] + data['routes'][name]})
    return events


# ---- 采录：OCR 盒标去重、撤销凭据、按发生时刻排序、SQLite 入库 ----

def test_capture_dedup_sorting_revocation_and_sqlite():
    data, events, stats = load_fixture()
    assert stats['raw_count'] == 11
    assert stats['unique_count'] == 10
    assert stats['dropped_duplicates'] == 1
    assert stats['revoked'] == ['rfid-ghost-0910']
    # 按发生时刻而非上传时刻排序：09:05 的扫描 11:00 才上传仍排在 09:30 之前
    order = [e['id'] for e in events]
    assert order.index('receipt-0905') < order.index('rfid-0930')
    ghost = next(e for e in events if e['id'] == 'rfid-ghost-0910')
    assert ghost['revoked'] is True
    assert ghost['revoke_credential'] == '撤销凭据C-9'
    # OCR 盒标归一化与去空格
    reg2 = next(e for e in events if e['id'] == 'reg-2')
    assert reg2['label_normalized'] == '盒标AX-002'

    with tempfile.TemporaryDirectory() as tmp:
        con = box_capture.connect(Path(tmp) / 'box.db')
        first = box_capture.ingest(con, data)
        assert first['inserted'] == 10 and first['retries'] == 0
        again = box_capture.ingest(con, data)
        assert again['inserted'] == 0 and again['retries'] == 10
        loaded = box_capture.load_events(con)
        assert [e['id'] for e in loaded] == [e['id'] for e in events]
        conflict = dict(loaded[0])
        conflict['actor'] = '被篡改经手人'
        # 同 ID 不同载荷必须拒绝（raw_count 之外的独立冲突事件）
        try:
            box_capture.ingest(con, {'events': [
                {'id': conflict['id'], 'kind': conflict['kind'],
                 'box_id': conflict.get('box_id'),
                 'actor': '被篡改经手人',
                 'occurred_at': '2026-11-23T08:00:00Z',
                 'uploaded_at': '2026-11-23T08:00:00Z'}]})
            raised = False
        except ValueError:
            raised = True
        assert raised
        con.close()


# ---- 双边点收与在途 ----

def test_bilateral_handover_chain_and_in_transit():
    _, events, _ = load_fixture()
    chain = box.custody_chain(events, 'BOX-2')
    kinds = [(e['kind'], e['state'], e.get('bilateral')) for e in chain]
    assert ('handover_dispatch', 'in_transit', False) in kinds
    receipt = next(e for e in chain if e['event_id'] == 'receipt-0905')
    assert receipt['state'] == 'at_room'
    assert receipt['bilateral'] is True
    assert receipt['responsible_party'] == '李扫描'
    # 10:00 时点：09:05 点收尚未上传，BOX-2 仍是在途而非已被扫描室占有
    state_1000 = box.custody(events, 'BOX-2', as_of=T1000)
    assert state_1000['state'] == 'in_transit'
    assert state_1000['open_transfer'] == 'H-2026-001'
    assert box.box_position(events, 'BOX-2', as_of=T1000) == 'in_transit'
    # 完整证据下 BOX-2 在扫描室
    state_full = box.custody(events, 'BOX-2')
    assert state_full['state'] == 'at_room'
    assert state_full['location'] == 'R'
    assert box.lendable(events, 'BOX-2') is False
    # BOX-1 全程在 S-4 可借
    assert box.custody(events, 'BOX-1')['state'] == 'at_shelf'
    assert box.lendable(events, 'BOX-1') is True


# ---- RFID 边界误读：单次读到不能把在途盒报成原库可借 ----

def test_rfid_boundary_misread_does_not_override_handover():
    data, events, _ = load_fixture()
    state = box.custody(events, 'BOX-2')
    assert 'rfid-0930' in state['misreads']
    # 08:10 在架可信读到，09:30 为误读；重复读到已在采录层按盒标/时刻去重
    misread_events = [entry for entry in box.custody_chain(events, 'BOX-2')
                      if entry['kind'] == 'rfid_read']
    assert [entry['event_id'] for entry in misread_events] == [
        'rfid-0810-2', 'rfid-0930']
    assert misread_events[0]['credible'] is True
    assert misread_events[1]['credible'] is False
    # 即便存在 09:30 的 S-4 读头记录，BOX-2 位置仍是扫描室而非 S-4
    assert box.box_position(events, 'BOX-2') == 'R'
    assert box.lendable(events, 'BOX-2') is False
    # 10:00 视角（点收迟到未上传）同样不能把在途盒报成在架可借
    plan_1000 = loan.fulfillment_plan(
        events, data['boxes'], ['V1', 'V2'], as_of=T1000)
    assert plan_1000['box_details']['BOX-2']['state'] == 'in_transit'
    assert 'BOX-2' in plan_1000['misread_evidence']


# ---- 迟到上传：按发生时刻补入，10:00 与 11:00 两种可见性 ----

def test_late_upload_visibility_changes_custody_only():
    _, events, _ = load_fixture()
    before = box.custody(events, 'BOX-2', as_of=T1000)
    after = box.custody(events, 'BOX-2', as_of=T1100)
    assert before['state'] == 'in_transit'
    assert after['state'] == 'at_room' and after['location'] == 'R'
    # 迟到点收保留原始发生/上传两个时刻作为凭据
    receipt = next(e for e in events if e['id'] == 'receipt-0905')
    assert receipt['occurred_at'] == box_capture.parse_ts('2026-11-23T09:05:00Z')
    assert receipt['uploaded_at'] == box_capture.parse_ts(T1100)


# ---- 整案全卷门槛：局部可预约但不能伪造整案已交付 ----

def test_no_double_possession_under_late_scan_and_misread():
    _, events, _ = load_fixture()
    # 同一时刻同一盒不得同时“原库可借”和“扫描室已占有”
    for ts in ('2026-11-23T09:01:00Z', '2026-11-23T09:30:00Z',
               T1000, T1100, '2026-11-23T12:00:00Z'):
        state = box.custody(events, 'BOX-2', as_of=ts)
        assert not (state['state'] == 'at_shelf'
                    and state['location'] == 'S-4'
                    and 'dispatch-0900' in [e['event_id']
                                            for e in box.custody_chain(events, 'BOX-2', ts)])
        assert state['responsible_party'] is None or isinstance(
            state['responsible_party'], str)
    # 在途期间责任方为空但移交双方证据完整，不能宣称任何一方仍占有
    in_transit = box.custody(events, 'BOX-2', as_of=T1000)
    assert in_transit['state'] == 'in_transit'
    assert in_transit['responsible_party'] is None
    dispatch = next(e for e in box.custody_chain(events, 'BOX-2', T1000)
                    if e['event_id'] == 'dispatch-0900')
    assert dispatch['from_party'] == '库房S-4' and dispatch['to_party'] == '扫描室R'
    # 迟到点收上传后责任唯一地落在扫描室，而不是库房、扫描室各一份
    settled = box.custody(events, 'BOX-2', as_of=T1100)
    assert (settled['state'], settled['location'],
            settled['responsible_party']) == ('at_room', 'R', '李扫描')


def test_whole_case_all_volumes_gate():
    data, events, _ = load_fixture()
    registry = data['boxes']
    plan_1000 = loan.fulfillment_plan(events, registry, ['V1', 'V2'], as_of=T1000)
    assert plan_1000['deliverable_boxes'] == ['BOX-1']
    assert plan_1000['reservable_boxes'] == ['BOX-1']
    assert plan_1000['recall_boxes'] == ['BOX-2']
    assert plan_1000['whole_case_fulfillable'] is False
    assert plan_1000['partial_only'] is True
    # 不能把只有 BOX-1 的局部可得标成整案已履行
    assert not any(plan_1000['box_details'][b]['action'] == '可交付'
                   for b in plan_1000['recall_boxes'])
    plan_full = loan.fulfillment_plan(events, registry, ['V1', 'V2'])
    assert plan_full['recall_boxes'] == ['BOX-2']
    assert plan_full['box_details']['BOX-2']['recall_reason'] == '扫描室已占有，待召回'
    assert plan_full['whole_case_fulfillable'] is False
    # 先到的局部盒（只借 V1）可独立成立，但不能据此宣称整案已交付
    plan_v1 = loan.fulfillment_plan(events, registry, ['V1'], as_of=T1000)
    assert plan_v1['deliverable_boxes'] == ['BOX-1']
    assert plan_v1['whole_case_fulfillable'] is True
    assert plan_v1['partial_only'] is False


# ---- 路线 A：扫描室 10:30 返还，两盒实体同借，统一归还期限 ----

def test_route_physical_return_two_box_loan():
    data, _, _ = load_fixture()
    events = route_events(data, 'physical_return')
    outcome = loan.service_outcome(events, data['boxes'], 'X', ['V1', 'V2'])
    assert outcome['whole_case_served'] is True
    assert outcome['route'] == 'physical_full_case'
    assert outcome['loaned_boxes'] == ['BOX-1', 'BOX-2']
    active = outcome['physical_loans']
    assert len(active) == 1 and active[0]['loan_id'] == 'L-2026-07'
    assert active[0]['borrower'] == '赵借阅'
    # 单一借阅凭据、统一归还期限
    assert active[0]['box_ids'] == ['BOX-1', 'BOX-2']
    # 旧交接与误读证据仍在链上，未被 location=out 抹掉
    chain_ids = [e['event_id'] for e in box.custody_chain(events, 'BOX-2')]
    for evidence in ('dispatch-0900', 'receipt-0905', 'r-dispatch-1025',
                     'r-receipt-1030', 'issue-1040'):
        assert evidence in chain_ids
    assert 'rfid-0930' in box.custody(events, 'BOX-2')['misreads']
    # 返还点收后两盒回到库房，凭据转为已还
    returned = loan.loans(events)
    item = next(x for x in returned if x['loan_id'] == 'L-2026-07')
    assert item['state'] == 'returned'
    assert item['returned_boxes'] == ['BOX-1', 'BOX-2']
    assert box.custody(events, 'BOX-2')['state'] == 'at_shelf'


# ---- 路线 B：BOX-2 留扫描室，V2 数字替代件双方确认，只实体借 BOX-1 ----

def test_route_digital_substitute_distinct_custody():
    data, _, _ = load_fixture()
    events = route_events(data, 'digital_substitute')
    outcome = loan.service_outcome(events, data['boxes'], 'X', ['V1', 'V2'])
    assert outcome['whole_case_served'] is True
    assert outcome['route'] == 'digital_substitute'
    assert outcome['loaned_boxes'] == ['BOX-1']
    assert outcome['digital_substitute_volumes'] == ['V2']
    assert outcome['digital_evidence'] == ['digital-1020']
    # BOX-2 保管责任仍在扫描室，没有进借阅清单
    assert outcome['custody']['BOX-2']['state'] == 'at_room'
    assert outcome['custody']['BOX-2']['responsible_party'] == '李扫描'
    assert outcome['physical_loans'][0]['box_ids'] == ['BOX-1']
    # 两种路线不能用同一布尔表达：本路线不存在两盒实体借阅凭据
    assert all(loan_item['box_ids'] != ['BOX-1', 'BOX-2']
               for loan_item in outcome['physical_loans'])
    # 旧交接与误读仍保留
    assert 'dispatch-0900' in [e['event_id']
                               for e in box.custody_chain(events, 'BOX-2')]
    assert 'rfid-0930' in box.custody(events, 'BOX-2')['misreads']
    # BOX-1 实体返还后 BOX-2 仍在扫描室
    assert box.custody(events, 'BOX-1')['state'] == 'at_shelf'
    assert box.custody(events, 'BOX-2')['state'] == 'at_room'


# ---- 迟到扫描只修订交接，不改写旧借阅凭据 ----

def test_late_scan_amends_handover_not_loan_credential():
    data, base_events, _ = load_fixture()
    # 基线流：迟到点收只修订 BOX-2 的保管链，不产生任何借阅凭据
    assert loan.loans(base_events, as_of=T1000) == []
    assert loan.loans(base_events, as_of=T1100) == []
    assert box.custody(base_events, 'BOX-2', as_of=T1000)['state'] == 'in_transit'
    assert box.custody(base_events, 'BOX-2', as_of=T1100)['state'] == 'at_room'

    # 路线 A 的借阅凭据在点收之后产生；迟到点收可见性变化不改写其证据
    events = route_events(data, 'physical_return')
    loans_after = loan.loans(events, as_of=T1100)
    issue = next(x for x in loans_after if x['loan_id'] == 'L-2026-07')
    assert issue['evidence'] == 'issue-1040'
    assert issue['borrower'] == '赵借阅'
    assert box.custody(events, 'BOX-2', as_of=T1000)['state'] == 'in_transit'
    # 返还库房后（10:30 双边点收，10:35 已上传）迟到点收修订的交接链已落到在架
    assert box.custody(events, 'BOX-2',
                       as_of='2026-11-23T10:35:00Z')['state'] == 'at_shelf'
    # 10:00 时尚无任何出借；11:00 后的迟到点收不改变既有出借凭据的证据内容
    assert loan.loans(events, as_of=T1000) == []
    assert all(item['evidence'] == 'issue-1040'
               for item in loan.loans(events, as_of=T1100))


# ---- 一个盒含多个卷号 ----

def test_box_carrying_multiple_volumes():
    registry = {'B-MANY': {'box_id': 'B-MANY', 'label': 'M',
                           'volumes': ['V8', 'V9'], 'home_shelf': 'S-9'}}
    events, _ = box_capture.normalize({
        'boxes': registry,
        'events': [
            {'id': 'r1', 'kind': 'register', 'box_id': 'B-MANY',
             'location': 'S-9', 'party': '库房S-9', 'actor': '王',
             'occurred_at': '2026-11-23T08:00:00Z',
             'uploaded_at': '2026-11-23T08:00:00Z'}]})
    plan = loan.fulfillment_plan(events, registry, ['V8', 'V9'])
    assert plan['deliverable_boxes'] == ['B-MANY']
    assert plan['whole_case_fulfillable'] is True
    # 缺卷场景不得被标记为整案可履行
    plan_missing = loan.fulfillment_plan(events, registry, ['V8', 'V404'])
    assert plan_missing['missing_volumes'] == ['V404']
    assert plan_missing['whole_case_fulfillable'] is False


# ---- 原单盒样例继续可运行 ----

def test_naive_last_upload_would_misallocate_but_reconciler_does_not():
    # 反例锚点：若只按“最后上传记录”判断，11:00 上传的点收会让 BOX-2
    # 同时被扫描室和借阅人占有；核验器必须以双边交接链为准。
    data, events, _ = load_fixture()
    naive_last = max(
        (event for event in events if event.get('box_id') == 'BOX-2'),
        key=lambda event: event['uploaded_at'])
    assert naive_last['id'] == 'receipt-0905'
    # 朴素读法会把 BOX-2 当成“最新记录=扫描室”，同时 09:30 读头仍显示 S-4，
    # 造成一房两占；核验器给出唯一责任方且不在架可借。
    state = box.custody(events, 'BOX-2')
    assert state['state'] == 'at_room'
    assert box.lendable(events, 'BOX-2') is False
    assert box.box_position(events, 'BOX-2') == 'R'
    # 10:00 的整案请求在任何可见性下都不能被标成已履行
    for ts in (T1000, T1100):
        plan = loan.fulfillment_plan(events, data['boxes'], ['V1', 'V2'],
                                     as_of=ts)
        assert plan['whole_case_fulfillable'] is False


def test_orphan_receipt_is_evidence_only():
    # 没有对应移交单的单边点收不能改变占有，只作为留证进入链。
    events, _ = box_capture.normalize({
        'events': [
            {'id': 'reg', 'kind': 'register', 'box_id': 'BX', 'label': 'L',
             'volumes': ['V'], 'home_shelf': 'S-1', 'location': 'S-1',
             'party': '库房S-1', 'actor': '王',
             'occurred_at': '2026-11-23T08:00:00Z',
             'uploaded_at': '2026-11-23T08:00:00Z'},
            {'id': 'lone-receipt', 'kind': 'handover_receipt', 'box_id': 'BX',
             'slip_id': 'NO-SUCH-SLIP', 'location': 'R', 'receiver': '李',
             'actor': '李', 'occurred_at': '2026-11-23T09:00:00Z',
             'uploaded_at': '2026-11-23T09:00:00Z'}]})
    state = box.custody(events, 'BX')
    assert state['state'] == 'at_shelf' and state['location'] == 'S-1'
    assert state['unmatched_receipts'] == ['lone-receipt']
    assert box.lendable(events, 'BX') is True


def test_legacy_single_box_record_still_works():
    record = json.loads((Path(__file__).resolve().parents[1]
                         / 'fixtures' / 'box_normal.json').read_text(encoding='utf-8'))
    assert box.location(record) == 'S-4'
    assert loan.available(record) == {'box_id': 'BOX-1', 'result': '可调阅'}
    out = dict(record, checked_out=True)
    assert box.location(out) == 'out'
    assert loan.available(out) == {'box_id': 'BOX-1', 'result': '外借中'}
