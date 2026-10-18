import os,tempfile,unittest
from unittest.mock import patch
os.environ.setdefault("DATA_DIR",tempfile.mkdtemp())
import run

class DirectTimeTest(unittest.TestCase):
 def test_change_preserves_booking_and_rejects_conflict_and_stale(self):
  with tempfile.TemporaryDirectory() as tmp, patch.object(run,"DB",tmp+"/db"):
   c=run.con()
   c.execute("INSERT INTO reservations(id,source,guest_name,visit_at,party_size,seating_area,amount,duration_minutes,status,created_at,updated_at,celebration_items) VALUES(1,'DIRECT','guest','2099-10-19T18:30',2,'PRIVATE2',90000,150,'CONFIRMED','now','now','花束')")
   c.commit()
   # Regular 18:00 is valid beyond the October 2026 custom-time exception.
   result=run.change_direct_time(c,1,{"expected_visit_at":"2099-10-19T18:30","time":"18:00"})
   self.assertEqual(result["visit_at"],"2099-10-19T18:00")
   row=c.execute("SELECT * FROM reservations WHERE id=1").fetchone()
   self.assertEqual((row["amount"],row["party_size"],row["seating_area"],row["celebration_items"],row["status"]),(90000,2,'PRIVATE2','花束','CONFIRMED'))
   with self.assertRaises(ValueError):run.change_direct_time(c,1,{"expected_visit_at":"2099-10-19T18:30","time":"20:30"})
   c.execute("INSERT INTO reservations(source,guest_name,visit_at,party_size,seating_area,amount,duration_minutes,status,created_at,updated_at) VALUES('DIRECT','other','2099-10-19T20:30',2,'PRIVATE2',90000,150,'CONFIRMED','now','now')")
   with self.assertRaises(ValueError):run.change_direct_time(c,1,{"expected_visit_at":"2099-10-19T18:00","time":"20:30"})
   with self.assertRaises(ValueError):run.change_direct_time(c,1,{"expected_visit_at":"2099-10-19T18:00","time":"19:00"})
   change={"party_size":3,"expected_party_size":2,"expected_visit_at":"2099-10-19T18:00","expected_amount":90000}
   result=run.change_direct_party(c,1,change)
   self.assertEqual((result["party_size"],result["amount"]),(3,135000))
   with self.assertRaises(ValueError):run.change_direct_party(c,1,change)
   change.update(party_size=5,expected_party_size=3,expected_amount=135000)
   with self.assertRaises(ValueError):run.change_direct_party(c,1,change)
   c.execute("UPDATE reservations SET status='CANCELLED' WHERE id=1")
   with self.assertRaises(ValueError):run.change_direct_time(c,1,{"expected_visit_at":"2099-10-19T18:00","time":"20:30"})
   c.close()
 def test_http_auth_and_update(self):
  import json,threading,urllib.request,urllib.error
  from http.server import ThreadingHTTPServer
  with tempfile.TemporaryDirectory() as tmp, patch.object(run,'DB',tmp+'/db'), patch.object(run,'ADMIN_TOKEN','test-only'):
   c=run.con()
   c.execute("INSERT INTO reservations(id,source,guest_name,visit_at,party_size,seating_area,amount,duration_minutes,status,created_at,updated_at) VALUES(1,'DIRECT','guest','2099-10-19T18:30',2,'PRIVATE2',90000,150,'CONFIRMED','now','now')")
   c.commit();c.close()
   server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler)
   threading.Thread(target=server.serve_forever,daemon=True).start()
   try:
    def post(kind,body,headers):
     req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/reservations/1/'+kind,data=json.dumps(body).encode(),headers={'Content-Type':'application/json',**headers})
     return json.load(urllib.request.urlopen(req))
    data={'time':'18:00','expected_visit_at':'2099-10-19T18:30'}
    with self.assertRaises(urllib.error.HTTPError) as error:post('time',data,{})
    self.assertEqual(error.exception.code,401)
    with self.assertRaises(urllib.error.HTTPError) as error:post('time',data,{'x-admin-token':'test-only'})
    self.assertEqual(error.exception.code,403)
    headers={'x-admin-token':'test-only','X-Tsukiya-Action':'change-time'}
    self.assertEqual(post('time',data,headers)['visit_at'],'2099-10-19T18:00')
    self.assertEqual(post('party',{'party_size':4,'expected_party_size':2,'expected_amount':90000,'expected_visit_at':'2099-10-19T18:00'},headers)['amount'],180000)
   finally:server.shutdown();server.server_close()
