# Homey — example property-search agent

Templates for a daily rental-search agent, plus the scripts that apply and evaluate them.

| File | What it is |
|---|---|
| [system_prompt.md](system_prompt.md) | Agent prompt: strict criteria, evidence vs. unknowns, recovery when sources fail |
| [routine.md](routine.md) | Daily routine brief with `<PLACEHOLDER>` search criteria |
| [daily_updates.md](daily_updates.md) | Changes-only email policy to add to an existing prompt and routine (newer than the two templates; don't reapply those wholesale over it) |

The prompt relies on `rightmove_search`, `agency_search`, the browser tools, and the
`record_property_assessment` / `list_property_candidates` ledger, which keeps dated,
append-only assessments per agent. Do not add `memory_save` or `memory_recall`.

## Applying

`scripts/configure_homey.py` updates one agent's prompt/tools and one routine's description in
an existing storage root. It previews by default and refuses to run over an outstanding prompt
candidate.

```sh
# 1. Preview (read-only); note the state hash it prints
python -m scripts.configure_homey \
  --storage-root /root/.telos --user USER --routine-id ROUTINE_ID \
  --recipient EXISTING_RECIPIENT \
  --prompt docs/homey/system_prompt.md --routine docs/homey/routine.md

# 2. Stop telos and the nightly worker, then apply
python -m scripts.configure_homey ...same args... \
  --apply --service-stopped --expected-state HASH --backup /private/path/homey-before.json
```

The backup file must not already exist; keep it for rollback. Restart the services afterwards.
The scripts are not in the runtime image — run them from a checkout mounted against the same
storage volume. Check with a no-email test run before resuming delivery.

## Evaluating

Closed-book classification on frozen evidence (no tools, email or memory writes):

```sh
python -m scripts.evaluate_homey --backend codex --model MODEL \
  --prompt docs/homey/system_prompt.md --evidence PATH --output PATH
```

`--backend anthropic` needs an Anthropic API key; a subscription login is not enough.
