from . import archive

def disposition(record):
    return {'case_id':record['case_id'],'state':'ready' if archive.complete(record) else 'missing'}
