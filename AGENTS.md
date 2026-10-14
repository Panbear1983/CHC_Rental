# CHC_Rental - agent rules

**Memory wing: `chc_rental`.** All agents (Claude Code, Hermes, Codex, Antigravity) share ONE long-term
memory, the mempalace, reached through the `mempalace_*` tools. Full rules and wing table:
`~/.mempalace/MEMORY_PROTOCOL.md`.

File-based rental scraper + TUI, no database.

## Every task in this repo
1. Start: `mempalace_search(<what you are about to do>, wing="chc_rental")` before planning.
2. Finish: one `mempalace_add_drawer(wing="chc_rental", room=<decisions|backend|bugs|reference>)`:
   what was done, what was verified, what is still open, Peter's words verbatim where they matter.
   Dated facts (bug fixed, decision made, thing deployed) also go to `mempalace_kg_add`.
3. Never file secrets, tokens or financial figures.
4. The Hermes kanban board is live work state, not memory; do not copy cards into the palace.

## Standing rules for this repo
- Blocked on source vetting - do not add new scrape sources without Peter approving them.
