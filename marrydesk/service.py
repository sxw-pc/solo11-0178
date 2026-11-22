from . import delivery

def served(record):
    return {'case_id':record['case_id'],'state':'served' if delivery.latest_status(record)=='delivered' else 'pending'}
