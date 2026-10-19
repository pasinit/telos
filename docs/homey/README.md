# Homey Search Reliability

## Changes

- Rightmove uses an area page instead of the generic landing page. Search results
  include bedroom counts, descriptions, pagination and explicit coverage limits.
  Unrecognised responses are errors, not empty inventory. Featured cards are deduplicated.
- Browser text removes navigation, form options and repeated image markup before
  truncation. Structured records precede text and remain valid JSON. Agency adapters
  now run in the stateful browser too. Observed hrefs are exposed for recovery.
- HTTP failures and failed navigations are not successful property reads. Crashed
  sessions are rebuilt on the next navigation. This does not bypass site restrictions.
- Agency searches distinguish unusable responses from filtered current-page results.
  Optional postcode filters reject known wrong districts and retain unknowns for
  verification. No claim is made that all agency integrations are exhaustive.
- The per-agent history DB stores append-only property assessments. Dedicated
  record/list tools preserve criterion evidence, observation dates, first-seen dates,
  original unresolved deadlines and exclusions. Historical assessments are injected
  into routine context, separately from the short email. Automatic Recordatio memory
  remains unchanged. Assessments are agent assertions, not independent verification.

## Prompt and Routine

`system_prompt.md` replaces the accumulated procedural prompt with a shorter
workflow using the tools actually available. `routine.md` retains the mandatory
brief, named sources and permanent exclusions, but removes the obsolete hardcoded
tracking list. It does not relax match criteria or put unresolved leads into emails.

Applying these files requires the new code and the two property tools. Do not deploy
the prompt alone against an old image. Do not add memory_save or memory_recall.

Use `scripts/configure_homey.py` against the service's actual storage volume, not
an empty host directory. It defaults to a read-only preview, verifies ownership and
the existing authorised recipient, preserves schedule/last_run/notification settings
and existing grants, and refuses an outstanding prompt candidate.

```sh
python -m scripts.configure_homey \
  --storage-root /root/.telos --user USER --routine-id ROUTINE_ID \
  --recipient EXISTING_RECIPIENT \
  --prompt docs/homey/system_prompt.md --routine docs/homey/routine.md
```

After deploying the reviewed code, stop the service and nightly worker before
applying configuration. Repeat the command with `--apply --service-stopped`,
`--expected-state HASH_FROM_PREVIEW`, and `--backup /private/path/homey-before.json`.
The backup must not exist; it is created with mode 0600 before mutation. A concurrent
configuration change invalidates the preview. The script updates only Homey's
prompt/tools and the selected routine description. Restart the service and worker
to refresh cached agents/schedules; normal agent loading records the new prompt version.

The maintenance script and docs are not bundled in the runtime image. Mount this
checkout read-only into a one-off container using the same storage volume, or run
from an appropriately provisioned maintenance environment. Do not run the apply
command while either writer is active. Keep the backup: SQLite databases and the
prompt file do not share a crash-atomic transaction. It contains the original
current prompt, registry prompt/tools and routine description for rollback.

Verify with a no-email test before resuming normal delivery: correct scoped tool
grants, usable Rightmove results, persisted assessments visible after a fresh run,
unchanged routine schedule, and no unverified candidates in the email. This rollout
has not been applied to production as part of preparing the patch.

## Model Comparison

Closed-book comparison on frozen evidence: no browsing/tools, no email, no memory
writes. A small set of clipped/unusable detail pages plus synthetic controls (full
match, wrong postcode, let-agreed, missing size, over budget), labels withheld.
Both models classified every case correctly with both prompts. This only shows no
classification regression on these cases; it does not compare browser strategy,
visual inspection, multi-day recall or search coverage.

Replay with `python -m scripts.evaluate_homey --backend codex --model gpt-6-astra
--prompt docs/homey/system_prompt.md --evidence PATH --output PATH`. Use backend
`anthropic` and model `claude-sonnet-4-6` for the other cell. An authorised Anthropic
API credential is required; a subscription login alone is not sufficient.
