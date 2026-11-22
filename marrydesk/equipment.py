from . import scanner

def state(record):
    return {'device_id':record['device_id'],
            'state':'可用' if scanner.drift_mm(record)<=record['limit_mm'] else '待检修'}
