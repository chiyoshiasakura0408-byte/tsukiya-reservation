"""Explicit approved customer fixtures; production never auto-approves a legacy link."""
from unittest.mock import patch
import run
import concierge

def approve(db,user):
    with patch.object(run,'DB',db):
        c=run.con();c.close()
    c=concierge.connect(db)
    with c:
        c.execute('INSERT OR IGNORE INTO customers(match_key,name,created_at,updated_at) VALUES(?,?,?,?)',('fixture:'+user,'テスト顧客','now','now'))
        cid=c.execute('SELECT id FROM customers WHERE match_key=?',('fixture:'+user,)).fetchone()[0]
        c.execute('INSERT INTO concierge_customers(user_id,customer_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET customer_id=excluded.customer_id',(user,cid))
        c.execute("INSERT OR REPLACE INTO vip_access(user_id,status,name,introducer,customer_id,updated,decided_by) VALUES(?,'approved','テスト顧客','紹介者',?,0,'owner-fixture')",(user,cid))
    c.close()
