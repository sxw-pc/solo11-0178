import unittest, tempfile, json
from pathlib import Path
from marrydesk import cli, storage
print('IMPORTED_MODULE', cli.__file__)
class BusinessPaths(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.con=storage.connect(Path(self.tmp.name)/'db.sqlite');self.n=0
 def tearDown(self):self.con.close();self.tmp.cleanup()
 def go(self,sub,action,subject='pair-a',**data):
  self.n+=1;return cli.run_event(self.con,sub,{'id':f'e{self.n}','subject':subject,'action':action,'data':data,'effective_at':self.n,'received_at':self.n})
 def qualify(self,subject='pair-a'):
  return self.go('applicant','applicant',subject,partner='pair-b' if subject=='pair-a' else subject+'-partner',valid_until=100,region='cross')
 def test_identity_and_region(self):
  self.assertEqual(self.qualify()['outcome'],'eligible');self.assertEqual(self.go('applicant','applicant','pair-z',partner='pair-z',valid_until=100,region='cross')['outcome'],'blocked')
 def test_material_version_frozen(self):
  self.qualify();self.assertEqual(self.go('applicant','replace',version=2)['outcome'],'pending');self.assertEqual(storage.latest(self.con,'subjects','pair-a')['state'],'eligible')
 def test_capacity_waitlist_and_promotion(self):
  self.qualify();self.qualify('pair-c');first=self.go('slot','book',slot='w1',capacity=1);second=self.go('slot','book','pair-c',slot='w1',capacity=1);self.assertEqual((first['outcome'],second['outcome']),('confirmed','waitlist'));self.go('slot','cancel',reservation=first['event']);self.assertEqual(storage.latest(self.con,'allocations',second['event'])['state'],'confirmed')
 def test_revoke_releases_booking(self):
  self.qualify();book=self.go('slot','book',slot='w1');self.go('applicant','revoke');self.assertEqual(storage.latest(self.con,'allocations',book['event'])['state'],'released');self.assertEqual(self.go('visit','checkin',reservation=book['event'])['outcome'],'blocked')
 def test_outage_excuses_arrival(self):
  self.qualify();book=self.go('slot','book',slot='w1');self.go('slot','outage','w1',slot='w1');self.assertEqual(self.go('visit','checkin',reservation=book['event'])['outcome'],'excused')
 def test_arrival_completion_and_duplicate(self):
  self.qualify();book=self.go('slot','book',slot='w1');arrival=self.go('visit','checkin',reservation=book['event'],deadline=100);self.assertEqual(arrival['outcome'],'checked');self.assertEqual(self.go('visit','complete',checkin=arrival['event'])['outcome'],'completed');self.assertEqual(self.go('visit','complete',checkin=arrival['event'])['outcome'],'idempotent')
 def test_late_and_return(self):
  self.qualify();book=self.go('slot','book',slot='w1');self.assertEqual(self.go('visit','checkin',reservation=book['event'],deadline=0)['outcome'],'late');self.assertEqual(self.go('visit','return',reservation=book['event'])['outcome'],'needs-correction');self.assertEqual(storage.latest(self.con,'allocations',book['event'])['state'],'released')
 def test_event_idempotency_and_conflict(self):
  event={'id':'fixed','subject':'x','action':'applicant','data':{'partner':'y','valid_until':30,'region':'local'},'effective_at':1};self.assertEqual(cli.run_event(self.con,'applicant',event)['outcome'],'eligible');self.assertEqual(cli.run_event(self.con,'applicant',event)['outcome'],'idempotent');event['data']['region']='bad';self.assertRaises(ValueError,cli.run_event,self.con,'applicant',event)
 def test_return_prioritizes_rebooking(self):
  self.qualify();self.qualify('pair-c');old=self.go('slot','book',slot='w1',capacity=1);self.go('visit','return',reservation=old['event']);second=self.go('slot','book','pair-c',slot='w1',capacity=1);priority=self.go('slot','book',slot='w1',capacity=1);self.assertEqual(priority['outcome'],'priority-waitlist');self.go('slot','cancel','pair-c',reservation=second['event']);self.assertEqual(storage.latest(self.con,'allocations',priority['event'])['state'],'confirmed')
 def test_material_regression_blocked(self):
  self.qualify();self.assertEqual(self.go('applicant','replace',version=1)['outcome'],'blocked')
 def test_party_conflict_and_cross_office(self):
  self.qualify();self.assertEqual(storage.latest(self.con,'documents','e1-route')['state'],'office-C');self.assertEqual(self.go('applicant','applicant','pair-z',partner='pair-b',valid_until=100,region='local')['outcome'],'blocked')

class EvidenceAudit(unittest.TestCase):
 def test_raw_replay_detects_mutated_decision(self):
  with tempfile.TemporaryDirectory() as tmp:
   con=storage.connect(Path(tmp)/'audit.db')
   event={'id':'audit-1','subject':'audit-subject','action':'unknown','effective_at':1,'received_at':1}
   cli.run_event(con,list(cli.HANDLERS)[0],event)
   self.assertTrue(cli.audit(con)['ok'])
   con.execute("UPDATE decisions SET outcome='tampered' WHERE event_id='audit-1'");con.commit()
   self.assertFalse(cli.audit(con)['ok'])
   con.close()
