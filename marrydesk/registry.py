from . import certificate

def entry(record):
    return {'case_id': record['case_id'], 'serial': record['serial'],
            'state': 'issued' if certificate.usable(record) else 'pending'}
