A digest that only shows up when something happens is just a siren, and you'll learn to read it like one. So alerts should be rare and tiny, and the digest should come daily whether or not anything happened.

**Alert** (push, under 60 words, readable on a lock screen). It fires only when the gate passes: active class, at least 2 independent origins, p ≥ 0.6. All numbers below are made up.

```
CALL · lab.incident · p=0.65  (class base rate 0.22, 38 resolved)
Claim: a second frontier lab discloses an agent security incident by 17 Nov.
Evidence: 2 independent origins (HF postmortem, lab blog). 3 reposts discarded.
Falsifier: no disclosure by 17 Nov means noise.
Class status: ACTIVE, skill +0.31 vs base rate.
Details: triage/4412
```

The base rate sits right next to the probability so you read 0.65 as nearly 3x the usual, not as "probably". The falsifier is on the lock screen so you can't quietly forget what would prove it wrong. Add a cap of 2 alerts a day and a per-class cooldown, or you'll mute the channel within a week.

**Daily digest** (email or markdown, fixed order, roughly one screen):

```
DAILY DIGEST · Sun 18 Oct 2026 · 1 call · 0 alerts
Pipeline OK: 11/11 collectors fresh (oldest 4 min). Placebo check: noise, as intended.

CALLS (1)
· Acme Corp files ≥2 more WARN notices outside CA. p=0.45 (base 0.22),
  2 independent origins. Resolves 17 Nov. Falsifier: zero by then.
  Digest-only: below the 0.6 alert bar. [triage/4412]

UNEXPLAINED (1)   <- human attention goes here
· BGP update rate, region B, z=5.8. 3 mundane checks ruled out, none explains it,
  no testable claim yet. [triage/4419]

RESOLVED SINCE LAST DIGEST (1)
· MISS. "Region A traffic drop persists 24h." Said 0.60, outcome no.

TRACK RECORD
class        n   hit   skill   status
layoffs.us   64  0.22  +0.31   active
bgp.routing  47  0.15  -0.04   demoted to shadow today

DISMISSED (41): 29 collector backfill/coverage, 8 calendar, 3 no mechanism, 1 duplicate.
SHADOW: 7 predictions logged, not shown.
NEEDS YOU: 1 prediction can't be resolved by rule [pred 118].
```

Some of these choices are deliberate:

- **Pipeline health goes first.** Otherwise a dead collector looks exactly like a calm day. Add a heartbeat monitor outside the box too, so a missing digest means something broke.
- **Misses get the same space as hits.** A digest that only celebrates wins will quietly inflate your trust.
- **Dismissed anomalies are counted, not listed.** You'll see "29 backfills" without having to read 29 explanations, and the full list is linked.
- **Shadow predictions are a count only.** If you read them in the digest, you're promoting that class by hand, and the point of probation is that nothing gets to skip it.
- **"Needs you" is the human-resolution queue.** It's where the `resolver = 'human'` rows in your ledger come from.

The numbers should come from templates filled by SQL. Let the LLM supply only the 80-word `summary` the triage agent already wrote. If an LLM writes the digest narrative, it will drift toward "alarming developments" by day three.
