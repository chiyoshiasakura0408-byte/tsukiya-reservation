import test_workflow as workflow
run=workflow.run
import unittest

class RoomMoveTest(unittest.TestCase):
    def setUp(self):
        self.fixture=workflow.WorkflowTest();self.fixture.setUp()
        self.rid=self.fixture.reservation()
        c=run.con();c.execute("UPDATE reservations SET source='DIRECT',square_invoice_id=NULL,seating_area='PRIVATE2',party_size=4,visit_at='2026-10-24T18:00',status='CONFIRMED' WHERE id=?",(self.rid,));c.commit();c.close()
        self.data={'seating_area':'PRIVATE3','expected_seating_area':'PRIVATE2','expected_visit_at':'2026-10-24T18:00','expected_party_size':4}
    def tearDown(self):self.fixture.tearDown()
    def move(self):
        c=run.con()
        try:
            c.execute('BEGIN IMMEDIATE');result=run.change_direct_room(c,self.rid,self.data);c.commit();return result
        except Exception:c.rollback();raise
        finally:c.close()
    def test_preserves_all_other_booking_details(self):
        before=self.fixture.get(self.rid);self.move();after=self.fixture.get(self.rid)
        self.assertEqual(after['seating_area'],'PRIVATE3')
        for key in before:
            if key not in ('seating_area','updated_at'):self.assertEqual(before[key],after[key],key)
    def test_conflicting_room_rejected(self):
        other=self.fixture.reservation();c=run.con();c.execute("UPDATE reservations SET seating_area='PRIVATE3',visit_at='2026-10-24T18:00' WHERE id=?",(other,));c.commit();c.close()
        with self.assertRaises(ValueError):self.move()
        self.assertEqual(self.fixture.get(self.rid)['seating_area'],'PRIVATE2')
    def test_stale_room_rejected(self):
        self.data['expected_seating_area']='PRIVATE1'
        with self.assertRaises(ValueError):self.move()
    def test_two_people_not_moved_into_room_three(self):
        c=run.con();c.execute('UPDATE reservations SET party_size=2 WHERE id=?',(self.rid,));c.commit();c.close();self.data['expected_party_size']=2
        with self.assertRaises(ValueError):self.move()
    def test_cancelled_booking_rejected(self):
        c=run.con();c.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?",(self.rid,));c.commit();c.close()
        with self.assertRaises(ValueError):self.move()
