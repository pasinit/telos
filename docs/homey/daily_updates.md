# Daily Update Policy

Apply this policy to Homey's current prompt and saved routine without replacing
the search brief, user corrections, recipient, tool grants or schedule. The old
`system_prompt.md` and `routine.md` templates predate this policy and must not be
reapplied wholesale.

## Instruction Block

Daily update policy:

- The daily email is a changes-only update, not a repeat of the entire inventory.
  Compare against confirmed prior email deliveries supplied with this run, not
  merely yesterday's assistant summary or the selected memory briefing.
- Recommend a property in full only when newly discovered or materially changed
  since it was last emailed. A material change is new sourced evidence such as a
  price change, confirmed availability, corrected identity, resolved missing
  criterion, or changed user-confirmed application/exclusion status. State what
  changed, with old/new values and evidence dates where known.
- Rechecking an unchanged page, another search hit, a different URL for the same
  flat, or the same unresolved checks is not a material change. Match exact
  listing IDs first; merge cross-site identities only with supporting evidence,
  never merely because they share a street.
- Keep unchanged promising flats tracked internally. Do not repeatedly reproduce
  their full cards, links or calls to action in the daily email. Summarise unchanged
  tracking as a count, without claiming the count covers unavailable history.
  Supply the complete tracked inventory only when explicitly requested.
- Missing evidence remains acceptable for a labelled new lead or material update.
  Do not expire, reject, or infer an application merely because a lead is old or
  lacks verification. Previously emailed broken links without new evidence stay
  in the internal recovery backlog, not the daily recommendation list.
- If no new or materially changed leads were found, say so plainly and include
  material coverage gaps. Do not claim that no suitable flats exist, and do not
  pad the email by recycling yesterday's flats.
- Links in a prior correction/exclusion section prove only a historical mention.
  They do not prove the property was recommended, eligible or applied for. If
  history is missing, truncated or identity is ambiguous, disclose that limitation
  rather than inventing a first-seen date or claiming confirmed novelty.

## Rollout

The delivery-history code reads existing per-agent execution evidence and needs
no memory tools or property-ledger grants. It does not alter source eligibility,
send emails, mutate past results or guarantee semantic change detection: the
model must compare listing evidence. The confirmed-send lookup includes tool
sends even if the surrounding turn later failed, excludes failed/unconfirmed
sends and scheduler failure notices, and scopes evidence to the owning agent and
recipient. It reads at most 30 deliveries plus an overflow indicator and bounds
injected context. Historical mentions are not a complete property identity ledger.

After code deployment, back up the active prompt and routine, append the instruction
block to both while the service/worker are stopped, and restart to refresh caches.
Do not change any recipient, schedule, criteria or tool grant. This document alone
does not change production configuration.
