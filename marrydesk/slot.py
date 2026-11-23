from . import storage as s
NAME='时段与窗口调度'
def apply(c,e):
 a=e['action']; p=e.get('data',{}); sid=e['subject']; app=s.latest(c,'subjects',sid)
 if a=='book':
  if not app or app['state']!='eligible': return s.decide(c,e,NAME,'blocked','ineligible','blocked')
  resource=p['slot']; cap=int(p.get('capacity',1)); used=c.execute("SELECT COUNT(*) n FROM allocations WHERE resource=? AND state='confirmed'",(resource,)).fetchone()['n']
  has_return=c.execute("SELECT 1 FROM receipts WHERE subject=? AND kind='return'",(sid,)).fetchone()
  status='confirmed' if used<cap else 'priority-waitlist' if has_return else 'waitlist'; c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)',(e['id'],sid,resource,status,app['version']))
  return s.decide(c,e,NAME,status,'capacity-arbitrated',status)
 if a=='cancel':
  alloc=s.latest(c,'allocations',p['reservation'])
  if not alloc or alloc['subject']!=sid: return s.decide(c,e,NAME,'blocked','reservation-mismatch','blocked')
  c.execute("UPDATE allocations SET state='released' WHERE id=?",(p['reservation'],))
  wait=c.execute("SELECT id,subject FROM allocations WHERE resource=? AND state IN ('priority-waitlist','waitlist') ORDER BY CASE state WHEN 'priority-waitlist' THEN 0 ELSE 1 END,id LIMIT 1",(alloc['resource'],)).fetchone()
  if wait:
   other=s.latest(c,'subjects',wait['subject'])
   if other and other['state']=='eligible': c.execute("UPDATE allocations SET state='confirmed' WHERE id=?",(wait['id'],))
  return s.decide(c,e,NAME,'released','waitlist-reconsidered','released')
 if a=='outage':
  resource=p['slot']; c.execute("UPDATE allocations SET state='reschedule' WHERE resource=? AND state='confirmed'",(resource,))
  return s.decide(c,e,NAME,'reschedule','window-outage-excuses-arrival','reschedule')
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
