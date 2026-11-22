import unittest

from marrydesk import cli, ocr, storage


class ScanBaseline(unittest.TestCase):
    def test_tied_transcription_uses_first_identifier(self):
        con = storage.connect(':memory:')
        existing = {'id': 'old', 'subject': 'couple-3', 'action': 'applicant',
                    'effective_at': 1, 'received_at': 1,
                    'data': {'partner': 'couple-2', 'valid_until': 20, 'region': 'local'}}
        self.assertEqual(cli.run_event(con, 'applicant', existing)['outcome'], 'eligible')
        scan = {'id': 'scan-1', 'subject': 'couple-1', 'effective_at': 2,
                'valid_until': 20, 'region': 'cross', 'photo_digest': 'same-photo',
                'partner_candidates': ['couple-Z', 'couple-2']}
        self.assertEqual(ocr.import_scan(con, scan)['reason'],
                         'party-already-in-other-application')
        self.assertEqual(storage.report(con)['events'][-1]['id'], 'scan-1')
        con.close()
