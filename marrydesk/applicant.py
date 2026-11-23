from . import storage as s
NAME='资格与材料核验'
def apply(c,e):
 a=e['action']; p=e.get('data',{}); sid=e['subject']; old=s.latest(c,'subjects',sid)
 if a=='applicant':
  if old and old['state']=='revoked': return s.decide(c,e,NAME,'blocked','revoked-applicant','blocked')
  partner=p.get('partner'); office='office-L' if p.get('region')=='local' else 'office-C' if p.get('region')=='cross' and p.get('cross_district_proof',True) else None
  if partner==sid or not partner:return s.decide(c,e,NAME,'blocked','invalid-pair','blocked')
  if c.execute("SELECT 1 FROM documents WHERE kind='party' AND state IN (?,?) AND subject<>?",(sid,partner,sid)).fetchone():return s.decide(c,e,NAME,'blocked','party-already-in-other-application','blocked')
  state='eligible' if p.get('valid_until',0)>=e['effective_at'] and office else 'blocked'
  c.execute('INSERT OR REPLACE INTO subjects VALUES(?,?,?,?)',(sid,'couple',state,(old['version']+1 if old else 1)))
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'identity',int(p.get('version',1)),state))
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id']+'-a',sid,'party',1,sid))
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id']+'-b',sid,'party',1,partner))
  if office:c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id']+'-route',sid,'office',1,office))
  return s.decide(c,e,NAME,state,f'identity-and-region-checked:{office}',state)
 if a=='revoke':
  if not old: return s.decide(c,e,NAME,'blocked','no-applicant','blocked')
  c.execute('UPDATE subjects SET state=?,version=version+1 WHERE id=?',('revoked',sid))
  c.execute("UPDATE allocations SET state='released' WHERE subject=? AND state IN ('confirmed','waitlist','priority-waitlist')",(sid,))
  return s.decide(c,e,NAME,'revoked','reservation-released','revoked')
 if a=='replace':
  if not old: return s.decide(c,e,NAME,'blocked','no-applicant','blocked')
  previous=c.execute("SELECT MAX(version) v FROM documents WHERE subject=? AND kind='identity'",(sid,)).fetchone()['v'] or 0
  if int(p['version'])<=previous:return s.decide(c,e,NAME,'blocked','material-version-regression','blocked')
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'identity',int(p['version']),'pending'))
  return s.decide(c,e,NAME,'pending','new-version-does-not-rewrite-old-booking',old['state'])
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
