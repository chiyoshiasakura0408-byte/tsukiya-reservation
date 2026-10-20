# Beverage stock

`/drinks` is an iPhone-friendly, on-demand inventory for 本店 and 別邸. It contains the user's exact 23 products, including シャンパン. Unknown quantities start blank. Count individual bottles/cans/cartons; convert cases to contained units and opened containers to a fractional remainder. There is no purchasing recommendation or ordering input. The owner decides orders from the reported stock.

Staff enter quantities and their name, save individual items or all changed items, then preview and explicitly send the selected branch's complete inventory to the already-paired owner LINE. Each report includes missing quantities and each entered quantity's confirmation timestamp. Zero is distinct from missing. Saved stock remains across week boundaries. Concurrency revisions prevent overwriting another staff member, and changes and report snapshots are retained. Identical reports are deduplicated within the day.

There is **no scheduled beverage request, summary, onboarding or change announcement**. Existing supplies reminders and reports do not include beverages. Only explicit staff report submission or an allowed LINE command queues beverage messages. In the verified accounting group or paired owner DM, `酒類`, `酒類在庫`, `酒類在庫表`, `酒類チェック`, or `飲料在庫` returns an eight-day inventory link. Both inventory pages use the existing scoped inventory access and staff authentication; no access to reservation/finance APIs is granted. Navigation links connect the supplies and beverage pages.

Validation: `python3 -m unittest tests.test_drinks tests.test_supplies tests.test_line_delivery tests.test_line_bot tests.test_security_boundary -q`. All test notifications use isolated databases and fake LINE credentials/destinations.
