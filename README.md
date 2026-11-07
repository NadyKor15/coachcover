# CoachCover

Shift coverage engine for teams with certifications: when a coach or lifeguard calls out at dawn, CoachCover finds an eligible replacement and keeps the schedule covered.

Work in progress (Vultr Agent Rush, November 2026).

## Private lessons: the family says yes first

A private lesson is one child with one coach. When the coach calls out, CoachCover finds another cleared coach by the
same rules, holds them (they get no other offer for that time) and asks the child's parent: "Marina can't make it today,
so Chloe can teach Lily's private lesson at 17:00 instead. Is that OK?" Only the parent's yes books the coach, after the
rules run again. A no lets the coach go and offers the family's own coach at the nearest free time at the same hour;
if there is none, or the family says no again, they get a credit for one lesson. If the family does not answer within
30 minutes (and at the latest an hour before the lesson), the manager decides: move the lesson or a credit. Nothing is
moved or credited without a yes from the family or the manager. The parent answers on their own page (`/client?id=…`);
an assistant connected over MCP can read the status but cannot answer for the family.

Price of a private lesson: $50 for 30 minutes, the price from the founder's coaching practice (a group lesson is $30 a
child). A credit for a private lesson is worth that $50.

In the 50-morning replay (`docs/sim/SIM-50-MORNINGS.md`), how families answer is assumed, not measured: yes 80%, no
10%, no answer 10%, 2-20 minutes after the question.

License: MIT.
