from . import storage as s
NAME='到场办理与回执'
def apply(c,e):
 a=e['action']; p=e.get('data',{}); sid=e['subject']; app=s.latest(c,'subjects',sid); booking=s.latest(c,'allocations',p.get('reservation',''))
 if a=='checkin':
  if not booking or booking['subject']!=sid: return s.decide(c,e,NAME,'blocked','no-booking','blocked')
  if booking['state']=='reschedule': return s.decide(c,e,NAME,'excused','outage-preserves-rebook-right','excused')
  if not app or app['state']!='eligible' or booking['state']!='confirmed': return s.decide(c,e,NAME,'blocked','invalid-qualification-or-seat','blocked')
  if e['effective_at']>int(p.get('deadline',999999)): return s.decide(c,e,NAME,'late','arrival-after-deadline','late')
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'checkin','checked',0))
  return s.decide(c,e,NAME,'checked','identity-and-window-matched','checked')
 if a=='complete':
  check=s.latest(c,'receipts',p.get('checkin',''))
  if not check or check['subject']!=sid or not app or app['state']!='eligible': return s.decide(c,e,NAME,'blocked','no-valid-checkin','blocked')
  if c.execute("SELECT 1 FROM receipts WHERE subject=? AND kind='completed'",(sid,)).fetchone():return s.decide(c,e,NAME,'idempotent','already-completed','completed')
  c.execute('INSERT INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'completed','completed',0))
  return s.decide(c,e,NAME,'completed','receipt-issued','completed')
 if a=='return':
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'return','needs-correction',0))
  if booking: c.execute("UPDATE allocations SET state='released' WHERE id=?",(booking['id'],))
  return s.decide(c,e,NAME,'needs-correction','rebook-right-retained','needs-correction')
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
