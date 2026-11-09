# contextqa-stack-bench

A public, re-runnable benchmark of how well ContextQA works on each tech stack (React, Node, Angular,
Spring Boot, .NET, Python, Postgres, MySQL, ...). It measures it on real bugs in real open-source apps.

**No results published yet.** This repo holds the method, the answer keys and the scorer. Scores will
be added under `results/` once runs are done, with the evidence for every item.

## What is measured

| Capability | Question | Correct only if |
|---|---|---|
| Bug reproduction (`repro`) | Given a bug ticket, does ContextQA reproduce the bug? | It says **REPRODUCED on the bug release** and **NOT_REPRODUCED on the fixed release**, with the same ticket text. |
| Verify-fix (`verify_fix`) | Does the regression test ContextQA saved tell the two releases apart? | The test **fails on the bug release** (NOT_FIXED) and **passes on the fixed release** (FIXED). |

Only that exact verdict counts. On the fixed release, NEEDS_INFO or an error is wrong, not "close
enough". PR impact, generated tests and API/DB checks will get their own answer keys and result
formats later. Until then they count as 0 in the headline (see below), shown rather than hidden.

## The rules

1. **The answer key comes from reality.** Every item is a real upstream bug with a release that has
   the bug and the next release that has the fix. We check with `git compare` that the fix commit is
   absent from the bug release and present in the fixed release (`compare_verified`). Whether the bug
   was actually *seen* on the bug release is a separate field (`bug_presence_verified`).
2. **Tickets don't leak the fix.** Each bug is run from up to three ticket texts (`ticket_tiers`):
   the upstream issue text as-is, a clear rewrite, and a vague one. Ticket writers never see the fix.
3. **Consistency is measured, not assumed.** The same input is run **N = 5 times** with nothing
   changed. We report the **stable share** (items whose verdicts were identical on all 5 runs) and
   **pass^5** (items correct on all 5 runs) next to the single-run score. N = 3 is the minimum, and is
   labelled as such.
4. **Error bars, never "100%".** Each score is k/n with a **Wilson 95% interval**. The headline is the
   lower bound, rounded down: 30 out of 30 prints as "≥ 88%".
5. **One weighted headline per group.** Headline = Σ weight × lower bound, with weights fixed before
   any run: repro 0.25, verify-fix 0.15, PR impact 0.20, generated tests 0.15, consistency (pass^5)
   0.15, API/DB checks 0.10. A capability that isn't measured counts as 0.
6. **Bands:** **Works** needs a score ≥ 80%, consistency measured with ≥ 80% stable, and (for a
   headline) every capability measured. **Partial** needs ≥ 50%. Anything lower is **Not yet**.
7. **Dev vs held-out.** Every answer key is split into a dev set (may be looked at while fixing
   ContextQA) and a held-out set (frozen, never used for fixing). **Only held-out scores are
   published.** A fix made after a held-out run means a new scorecard version and a full re-run; old
   versions stay published. `split` is empty in every key today: it is assigned once the key is frozen.
8. **Every item's evidence is published, failures included:** the ticket, the upstream links, the
   verdict on each release and a link to the run (video, trace, steps).
9. **Scores are dated.** Every result carries the date, the model and the ContextQA version. The
   scorer refuses to mix stamps. Compare versions only on the same items.
10. **"Measured on these apps"**, not "works for every app on this stack".

## Per-tech and per-feature scores

Scores are reported per app, per capability, per tech (rolled up across every app that shares a stack
tag, so Twenty and Bigcapital both count for `node`) and per feature tag. Real apps break more on
features than on frameworks, so every bug is tagged with what it touches:

`sign-in-<type>` (from the app's recipe: password, SSO, MFA, magic link), `email`, `upload`,
`websocket`, `iframe`, `shadow-dom`, `canvas` (charts, maps), `timezone`, `locale`, `roles`,
`payments`, `db-specific`.

## Apps

| App | Stack | Bugs |
|---|---|---|
| [GrowthBook](apps/growthbook) | React + Node + MongoDB | 11 |
| [Twenty](apps/twenty) | React + NestJS + Postgres | 11 |
| [Bigcapital](apps/bigcapital) | React + NestJS + MySQL | 14 |
| [Ghost](apps/ghost) | Ember/React + Node + MySQL | 11 |
| [Umami](apps/umami) | Next.js + Postgres or MySQL | 9 |
| [Artemis](apps/artemis) | Angular + Spring Boot + Postgres | 11 |
| [ThingsBoard CE](apps/thingsboard) | Angular + Spring Boot + Postgres | 10 |
| [Kavita](apps/kavita) | Angular + .NET + SQLite | 12 |
| [nopCommerce](apps/nopcommerce) | ASP.NET Core Razor + Postgres | 12 |
| [Reflex](apps/reflex) | Python (Reflex) + SQLite | 8 |

Each `apps/<app>/` has:
- `answer-key.yaml`: the bugs ([schema](schema/answer-key.schema.json)).
- `recipe.yaml`: how to run the bug and fixed releases: images, services, ports, RAM, admin
  bootstrap, seed data, sign-in and hosting notes ([schema](schema/recipe.schema.json)). These are
  skeletons from research; none is boot-tested yet unless a note says so.

**Hosting safety:** register the admin account (or change default passwords) before a URL is public,
use fake data only, and stop hosts when idle. Several apps have open sign-up on first start.

## Results format

One JSON object per line in `results/<date>-<label>.jsonl`, one line per run on one release of one
bug ([schema](schema/results.schema.json)): bug id, `bug` or `fixed`, capability, verdict, expected,
correct, run URL, start and end time, credits, model, ContextQA version, and `run_index` (0..N-1 for
the consistency runs). The scorer recomputes `correct` from the rule table and warns on a mismatch.

Re-running costs ContextQA credits on your own account.

## Scorer

Python 3, standard library only, no network.

```bash
python3 scorer/score.py --check                          # validate every answer key and recipe
python3 scorer/score.py results/<file>.jsonl             # score the held-out split
python3 scorer/score.py results/<file>.jsonl --split all --n 5
python3 -m unittest discover -s scorer/tests             # tests, including a broken-formula check
```

Not implemented yet: the cluster bootstrap over repos from the method. With three or more apps the
published interval should be the wider of Wilson and the cluster bootstrap.

## Licence

MIT, see [LICENSE](LICENSE). Each upstream app keeps its own licence (listed in its `recipe.yaml`).
