"""Offline scan transcription for the appointment desk."""
from . import cli


def import_scan(con, scan):
    """Choose a deterministic OCR transcription and submit the application."""
    candidates = scan['partner_candidates']
    if not candidates:
        raise ValueError('no-partner-candidate')
    partner = min(candidates)
    event = {
        'id': scan['id'],
        'subject': scan['subject'],
        'action': 'applicant',
        'effective_at': scan['effective_at'],
        'received_at': scan.get('received_at', scan['effective_at']),
        'data': {
            'partner': partner,
            'partner_candidates': candidates,
            'photo_digest': scan['photo_digest'],
            'valid_until': scan['valid_until'],
            'region': scan['region'],
        },
    }
    return cli.run_event(con, 'applicant', event)
