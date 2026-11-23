"""实体盒保管位置与责任链。

在原单盒 shelf/checked_out 布尔之上，增加双边点收、在途状态与 RFID
边界误读的核验。盒的位置只能由双边点收或在架可信读头确认；单次 RFID
读到不能覆盖正在进行的交接，迟到上传的扫描按其真实发生时刻补入链中，
只修订对应交接，不产生新的责任占有。
"""
from .box_capture import parse_ts


def location(record):
    """兼容旧样例：单盒 record 只含 shelf 与 checked_out。"""
    return record['shelf'] if not record.get('checked_out') else 'out'


def _visible(events, as_of=None):
    if as_of is not None:
        as_of = parse_ts(as_of)
    visible = []
    for event in sorted(events, key=lambda item: (item['occurred_at'], item['id'])):
        if event.get('revoked'):
            continue
        if as_of is not None and event['uploaded_at'] > as_of:
            continue
        visible.append(event)
    return visible


def custody_chain(events, box_id, as_of=None):
    """返回该盒连续保管链；每个条目含责任方、位置与核验标记。"""
    chain = []
    visible = _visible(events, as_of)
    home = next((e.get('home_shelf') for e in visible
                 if e['kind'] == 'register' and e.get('box_id') == box_id), None)
    state = {'state': 'unknown', 'location': None, 'responsible_party': None,
             'open_transfer': None}
    seen_rfid = set()
    for event in visible:
        kind = event['kind']
        entry = {'event_id': event['id'], 'kind': kind,
                 'occurred_at': event['occurred_at'],
                 'uploaded_at': event['uploaded_at'], 'box_id': box_id,
                 'actor': event.get('actor')}
        if kind == 'register' and event.get('box_id') == box_id:
            state = {'state': 'at_shelf', 'location': event['location'],
                     'responsible_party': event.get('party'),
                     'open_transfer': None}
            entry.update(state)
        elif kind == 'handover_dispatch' and event.get('box_id') == box_id:
            state = {'state': 'in_transit',
                     'location': None,
                     'responsible_party': None,
                     'open_transfer': event['slip_id'],
                     'from_location': event['from_location'],
                     'to_location': event['to_location'],
                     'from_party': event['from_party'],
                     'to_party': event['to_party'],
                     'bilateral': False}
            entry.update(state)
        elif kind == 'handover_receipt' and event.get('box_id') == box_id:
            slip = event.get('slip_id')
            if state.get('open_transfer') != slip:
                entry.update(state, unmatched=True,
                             note='无对应移交单的单边点收，仅留证不改占有')
                chain.append(entry)
                continue
            # 落回盒的本库架位即回到在架；进入扫描室等业务房间为在室。
            settled = 'at_shelf' if event['location'] == home else 'at_room'
            state = {'state': settled, 'location': event['location'],
                     'responsible_party': event.get('receiver'),
                     'open_transfer': None, 'slip_id': slip,
                     'bilateral': True}
            entry.update(state)
        elif kind == 'rfid_read' and event.get('box_id') == box_id:
            fingerprint = (box_id, event['occurred_at'], event['location'])
            duplicate = fingerprint in seen_rfid
            seen_rfid.add(fingerprint)
            credible = (not duplicate
                        and state['state'] == 'at_shelf'
                        and state['location'] == event['location'])
            entry.update(state)
            entry['credible'] = credible
            entry['misread'] = not credible
            entry['duplicate'] = duplicate
            # 无论可信与否，读头事件本身不改变保管状态。
        elif kind == 'loan_issue' and box_id in event.get('box_ids', []):
            state = {'state': 'on_loan', 'location': 'out',
                     'responsible_party': event.get('borrower'),
                     'open_transfer': None, 'loan_id': event['loan_id']}
            entry.update(state)
        elif kind == 'return_receipt' and box_id in event.get('box_ids', []):
            state = {'state': 'at_shelf', 'location': event['location'],
                     'responsible_party': event.get('checker'),
                     'open_transfer': None, 'loan_id': event.get('loan_id')}
            entry.update(state)
        else:
            # loan_request / digital_delivery / event_cancel 不改变实体保管。
            continue
        chain.append(entry)
    return chain


def custody(events, box_id, as_of=None):
    """该盒在指定时刻（按上传可见性）可核验的当前保管状态与误读凭据。"""
    chain = custody_chain(events, box_id, as_of)
    misreads, duplicates, unmatched = [], [], []
    for entry in chain:
        if entry.get('misread'):
            misreads.append(entry['event_id'])
        if entry.get('duplicate'):
            duplicates.append(entry['event_id'])
        if entry.get('unmatched'):
            unmatched.append(entry['event_id'])
    state_changing = [entry for entry in chain if entry['kind'] != 'rfid_read']
    latest = state_changing[-1] if state_changing else None
    result = {'box_id': box_id,
              'state': latest['state'] if latest else 'unknown',
              'location': latest.get('location') if latest else None,
              'responsible_party': latest.get('responsible_party') if latest else None,
              'open_transfer': (latest.get('open_transfer') if latest else None),
              'misreads': misreads, 'duplicate_reads': duplicates,
              'unmatched_receipts': unmatched, 'as_of': parse_ts(as_of) if as_of is not None else None}
    if latest and latest['kind'] == 'handover_dispatch':
        result['from_location'] = latest['from_location']
        result['to_location'] = latest['to_location']
    return result


def box_position(events, box_id, as_of=None):
    """对外位置：架位/房间号、in_transit 或 out；绝不把在途盒报成原库可借。"""
    state = custody(events, box_id, as_of)
    if state['state'] == 'on_loan':
        return 'out'
    if state['state'] == 'in_transit':
        return 'in_transit'
    return state['location']


def lendable(events, box_id, as_of=None):
    """盒是否可立即实体出借：只有在架且无开放交接。"""
    state = custody(events, box_id, as_of)
    return state['state'] == 'at_shelf'
