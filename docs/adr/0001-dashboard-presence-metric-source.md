# ADR 0001: Dashboard Presence Metric Source

## Status

Accepted (revisado em 11/09/2026 — CS-79)

## Context

The dashboard exposes `attendance_avg_percent` for the monitored
parliamentarians of an authenticated project. The schema includes
`plenary_attendance` and `committee_attendance`.

The Chamber publishes attendance for deliberative plenary sessions and
`camara_crawler/plenary_attendance.py` collects it into `plenary_attendance`.

The Senate does not. Its open-data OpenAPI spec (checked 10/09/2026) has no
per-session attendance endpoint: the only attendance it publishes is
`/plenario/lista/tiposComparecimento`, "Tipos de Comparecimento **em
Votações** do Plenário" — the attendance of each senator in each nominal
vote. So `plenary_attendance` has no senator rows and the card showed "--"
in both windows, which is exactly the gap a subscriber reported (05/09/2026).

The original data model notes already said plenary attendance should be taken
from nominal votes, because presence is only mandatory in deliberative
sessions.

## Decision

One source per house, and the screen always says which one it is showing.

- **Chamber**: `plenary_attendance` / `committee_attendance`, collected from
  the official attendance pages. Unchanged.
- **Senate**: inferred from `roll_call_votes`, which already stores the
  senator's attendance code for every nominal vote.

The Senate inference is defined as:

1. **Aggregate by session day, not by vote.** 29/04/2026 had 10 nominal votes
   and 27/05/2026 had one; per vote the first day would weigh 10x.
2. **A day counts as present if the senator shows up in any vote that day.**
   Leaving early is not a full absence.
3. **The denominator is the set of days on which the senator has a record of
   his own.** The Senate records a code for a senator in exercise even when he
   misses the session — 62 of the 81 senators have a record on all 121 session
   days of the 2023-2027 legislature, including one who attended only 54% of
   them. So "no record at all on a session day" means "not in exercise"
   (a suplente who had not taken the seat yet, a licensed minister), not an
   absence. Counting those days would drop a suplente sworn in last month to
   2.5%.
4. **Justified absence counts as absence**, which is what the Chamber does
   today. Licence and mission are the whole signal: across the legislature the
   median senator is at 10.7% justified absence and 0% unjustified (`NCom`).
   Discounting the justified ones would put almost every senator at ~100% and
   the indicator would stop telling anyone apart.
5. **A code that is not about attendance leaves the calculation.** `FAL`
   (falecimento), `TER` (término do mandato) and `NH` (não houve votação) are
   nobody's absence, and an unknown code the API may start emitting must not
   become an absence before someone looks at it.

Because the two numbers are not the same measurement, the tile itself declares
the Senate one as inferred — next to the number, not only when it is missing.

## Consequences

- Senate presence works from data already collected by `roll_call_votes`, with
  no new crawler and no new cron entry: `senado-roll-call-votes` (3h),
  `backfill-votes-speeches` (2018→today, per senator) and
  `backfill-vote-dates` already fill it.
- The metric depends on `roll_call_votes.vote_date`. Without that column —
  the deploy window before migrations run — the Senate indicator goes back to
  "sem dado" instead of returning a wrong number.
- The Chamber number counts attendance events and the Senate number counts
  session days. A project dashboard that mixes both houses averages the two.
- If `plenary_attendance` ever gets senator rows from a real attendance
  source, the meaning of the Senate number changes and the screen copy has to
  change with it. That needs another ADR, not a silent switch.

## Validation

Checked against the whole 2023-2027 legislature: 81 senators, 423 nominal
votes, 121 session days.

- The universe of votes and session days was recomputed from a second,
  independent endpoint (`/plenario/lista/votacao/{ini}/{fim}`, which lists all
  81 senators per vote). Both paths agree on all 81 senators, to the decimal.
- Six senators covering every edge case (Alan Rick 89%, Beto Faro 99%,
  Eliziane Gama 73% with a licence gap, Jader Barbalho 54%, Renan Filho 47% as
  a minister in and out of the seat, Roberta Acioly 100% as a suplente with 17
  days) were run end to end — Senate API → crawler → Postgres →
  `_calculate_attendance_avg_percent` — and match the reference to the point.
