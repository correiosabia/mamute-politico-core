# Mamute Politico Context

## Domain Terms

### Mamute Politico

The product that lets authenticated Ghost members monitor legislative activity from selected Brazilian parliamentarians.

### Project

A customer workspace represented by a `projetos` row. A project is identified from the authenticated Ghost member e-mail and owns the list of monitored parliamentarians.

### Monitored Parliamentarian

A parliamentarian favorited by a project through `projetos_parliamentarian`. Dashboard metrics are scoped to the monitored parliamentarians of the authenticated project.

### Parliamentarian

An elected official from either the Chamber of Deputies or the Senate. Parliamentarians are stored in `parliamentarian` and are keyed to source-system codes through `parliamentarian_code`.

### Proposition

A legislative matter stored in `proposition`. Propositions can be associated with parliamentarian authors through `authors_proposition`.

### Proposition Theme

An official thematic area (*área temática*) that the Chamber or the Senate
assigns to a proposition, stored as a list of names in `proposition.themes`,
in the order the source returns them. A proposition often has several and the
source marks none as the main one, so none is singled out. `NULL` means not
collected yet; `[]` means collected and the House did not classify it — the
Chamber never classifies requests (REQ), amendments (EMC) or committee
opinions (PRL, PAR). The two must not look the same on screen. Not to be
confused with the indexing keywords (`keywords`, `indexacao`) that the House
documentation staff add later, nor with an **Editorial Agenda**, which is
derived from a parliamentarian's speeches.

### Nominal Vote

A recorded vote by a parliamentarian on a proposition, stored in `roll_call_votes`.

### Speech Transcript

A speech or stenographic note associated with a parliamentarian, stored in `speeches_transcripts`.

### Committee

A legislative committee or collegiate body stored in `committee`.

### Plenary Attendance

A parliamentarian attendance record for plenary activity, stored in
`plenary_attendance` when collected from a source or derived according to an
ADR. Only the Chamber has rows: it publishes attendance per deliberative
session and `camara_crawler/plenary_attendance.py` collects it.

### Inferred Presence

The Senate's side of the presence indicator. The Senate publishes no
attendance per plenary session — only each senator's attendance code in each
nominal vote — so the number is derived from `roll_call_votes`: the share of
**session days** on which the senator appears present in at least one nominal
vote. Aggregated by day, never by vote, so a day with ten votes does not weigh
ten times. A day on which the senator has no record at all is a day out of
exercise, not an absence, and stays out of the denominator; a justified
absence (licence, mission, parliamentary activity) is an absence, the same as
in the Chamber. Because it is not the same measurement as the Chamber's, the
card says on the tile that the number is inferred. See
`docs/adr/0001-dashboard-presence-metric-source.md`.

### Committee Attendance

A parliamentarian attendance record for committee activity, stored in `committee_attendance`.

### Parliamentary Amendment

A budget amendment (*emenda parlamentar orçamentária*) through which a
parliamentarian directs federal funds, stored in `parliamentary_amendment`.
Collected from the Portal da Transparência, which identifies the author only by
free-text name — so `parliamentarian_id` is nullable and `match_status` records
whether the name resolved to a parliamentarian, resolved ambiguously, or not at
all. Amendments that resolve to nobody are kept, not discarded, and surface in
the admin audit panel. Not to be confused with an amendment to a proposition,
which alters the text of a bill.

### Dashboard Stats

Aggregated activity counts shown in the authenticated project dashboard. These counts must make their source period and data freshness clear enough that a zero can be distinguished from missing data.

### Data Freshness

The recency of collected legislative data per source table. Freshness is part of operational correctness for dashboard metrics and crawler acceptance.

### Scraper

A command under `mamute_scrappers` that collects source data and persists it into the PostgreSQL legislative database. Scrapers must be idempotent.

### Personal Mark

Anything a subscriber records about a parliamentarian for their own use: the
personal order of monitored parliamentarians, free tags, and the mamutômetro.
Personal marks are layers on top of monitoring, never a second kind of favorite,
and none of them consumes the plan's `qtd_termos`.

### Mamutômetro

A scale of 1 to N mammoth icons that a subscriber assigns to a parliamentarian,
stored in `project_mamutometro`. **The meaning of each level is chosen privately
by each subscriber and never recorded** — one person may use 3 for "I voted for
them" and another for "I follow them closely". The product never asks, never
suggests and never aggregates, which is what keeps it from holding a declared
vote. The column is called `level` and nothing else, on purpose. See
`docs/adr/0002-privacidade-do-mamutometro.md`.

### Project Tag

A free-text label a project creates and applies to parliamentarians, stored in
`project_tag` and `parliamentarian_tag`. Tags are private to the project: there
is no shared, suggested or public tag.

### Personal Order

The subscriber-defined ordering of monitored parliamentarians, stored in
`projetos_parliamentarian.position`. `NULL` means never ordered, and the reading
order is `position NULLS LAST, created_at DESC` — so before anyone reorders, the
result is identical to the previous behaviour.

### Marks Configuration

Admin-owned settings for personal marks, in `marcacoes_config` (a single row):
mamutômetro scale size, the neutral first-use notice, and whether the
mamutômetro and tags apply to monitored parliamentarians only or to the whole
visible catalog. Which plans get the mamutômetro lives in `feature_flag_tier`;
how many parliamentarians a plan may mark lives in `tiers.detalhes`
(`qtd_mamutometro`). **Changing configuration never deletes a subscriber's
marks** — they go dormant and return if the configuration returns.

### Elected Candidate

A 2026 candidacy whose official TSE result, in a results file with the
totalization closed, has `eleito = true` **and** a status starting with
"Eleito" ("Eleito", "Eleito por QP", "Eleito por média"), stored in
`candidacy_result`. The flag alone is not enough: the TSE also sets `e = "s"`
for candidates who went to the runoff. Gender and race come from the candidacy
(`candidacy.gender`/`race`), because the results file does not carry them. A
partial (still open) result never counts as elected anywhere in the product.

### Collection

A curated, admin-edited page that groups people around a subject, stored in
`collection`. Each person is a **Collection Member** (`collection_member`):
a parliamentarian, a candidate, or someone outside the database, with an
optional `tier` (1 to 5) whose meaning is set per collection in
`tier_labels`, a context text and sources. **Collection Blocks**
(`collection_block`) interleave content: free text, links, documents, or a
pointer to a speech, nominal vote, proposition or quota expense in the
database. A member's link to the rest of the schema is resolved on every read
through `parliamentarian_id`, `candidacy_id` and the CPF, so a candidate who
takes office later, or a person who moves to another house or level, points to
the right profile without rewriting the collection. Only published collections
are readable without login.
