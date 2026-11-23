"""整案调阅计划与实体盒出借责任。

整案请求必须同时覆盖所需全部卷：局部盒可先预约，但不得伪造整案已交付；
已在扫描室占有或在途的盒进入待召回，不能同时被宣称“原库可借”。
迟到扫描只修订交接链，不改动任何已生成的借阅凭据。
"""
from . import box
from .box_capture import parse_ts

_RECALL_REASON = {'in_transit': '交接在途，待双边点收',
                  'at_room': '扫描室已占有，待召回',
                  'on_loan': '已外借，待催还'}


def available(record):
    """兼容旧样例：单盒 shelf/checked_out 回答。"""
    return {'box_id': record['box_id'],
            'result': '可调阅' if box.location(record) != 'out' else '外借中'}


def _volume_boxes(registry):
    mapping = {}
    for entry in registry.values():
        for volume in entry['volumes']:
            mapping.setdefault(volume, []).append(entry['box_id'])
    return mapping


def fulfillment_plan(events, registry, required_volumes, as_of=None):
    """对多卷请求给出可交付盒集合、待召回盒与可预约的局部盒。"""
    volume_map = _volume_boxes(registry)
    missing_volumes = [v for v in required_volumes if v not in volume_map]
    deliverable, recall, reserve, details = [], [], [], {}
    seen_boxes = set()
    for volume in required_volumes:
        for box_id in volume_map.get(volume, []):
            if box_id in seen_boxes:
                continue
            seen_boxes.add(box_id)
            state = box.custody(events, box_id, as_of)
            detail = {'box_id': box_id, 'state': state['state'],
                      'location': state['location'],
                      'responsible_party': state['responsible_party'],
                      'misreads': state['misreads']}
            if state['state'] == 'at_shelf':
                deliverable.append(box_id)
                reserve.append(box_id)
                detail['action'] = '可交付'
            else:
                recall.append(box_id)
                detail['action'] = '待召回'
                detail['recall_reason'] = _RECALL_REASON.get(
                    state['state'], state['state'])
            details[box_id] = detail
    fulfilled = not missing_volumes and not recall
    return {
        'required_volumes': list(required_volumes),
        'deliverable_boxes': deliverable,
        'recall_boxes': recall,
        'reservable_boxes': reserve,
        'missing_volumes': missing_volumes,
        'misread_evidence': {bid: detail['misreads']
                             for bid, detail in details.items()
                             if detail['misreads']},
        'whole_case_fulfillable': fulfilled,
        'partial_only': bool(reserve) and not fulfilled,
        'as_of': parse_ts(as_of) if as_of is not None else None,
        'box_details': details,
    }


def loans(events, as_of=None):
    """从事件流派生借阅凭据；迟到事件不会改写旧凭据，只新增凭据。"""
    result = []
    for event in box._visible(events, as_of):
        if event['kind'] == 'loan_issue':
            result.append({
                'loan_id': event['loan_id'], 'case_id': event['case_id'],
                'box_ids': list(event['box_ids']),
                'borrower': event['borrower'], 'due_at': event['due_at'],
                'issued_at': event['occurred_at'],
                'state': 'active', 'evidence': event['id'],
            })
        elif event['kind'] == 'return_receipt':
            returned = set(event['box_ids'])
            for item in result:
                if item['loan_id'] == event['loan_id']:
                    item['state'] = 'returned'
                    item['return_evidence'] = event['id']
                    item['returned_boxes'] = sorted(
                        set(item['box_ids']) & returned)
    return result


def service_outcome(events, registry, case_id, required_volumes, as_of=None):
    """整案服务结论：实体两盒同借，或单盒实体借 + 双方确认的数字替代件。

    两种路线的保管责任与借阅清单不同，但都不得用 location=out 布尔混同。
    """
    plan = fulfillment_plan(events, registry, required_volumes, as_of)
    case_loans = [item for item in loans(events, as_of)
                  if item['case_id'] == case_id]
    digital = [event for event in box._visible(events, as_of)
               if event['kind'] == 'digital_delivery'
               and event.get('case_id') == case_id]
    digital_volumes = sorted({volume for event in digital
                              for volume in event.get('volumes', [])})
    # loaned_boxes：该整案借阅凭据实际覆盖的全部实体盒（含已点收回库者）。
    issued_boxes = sorted({box_id for item in case_loans
                           for box_id in item['box_ids']})
    loaned_boxes = issued_boxes
    returned_boxes = sorted({box_id for item in case_loans
                             for box_id in item.get('returned_boxes', [])})
    volume_map = _volume_boxes(registry)
    loaned_volumes = sorted({volume for box_id in issued_boxes
                             for volume in registry[box_id]['volumes']})
    covered = set(loaned_volumes) | set(digital_volumes)
    complete = all(volume in covered for volume in required_volumes)
    if digital and case_loans:
        route = 'digital_substitute'
    elif case_loans and complete:
        route = 'physical_full_case'
    else:
        route = 'pending'
    box_responsibility = {
        box_id: box.custody(events, box_id, as_of)
        for volume in required_volumes
        for box_id in volume_map.get(volume, [])
    }
    return {
        'case_id': case_id,
        'route': route,
        'whole_case_served': complete,
        'physical_loans': case_loans,
        'loaned_boxes': loaned_boxes,
        'returned_boxes': returned_boxes,
        'digital_substitute_volumes': digital_volumes,
        'digital_evidence': [event['id'] for event in digital],
        'custody': box_responsibility,
        'plan': plan,
        'as_of': parse_ts(as_of) if as_of is not None else None,
    }
