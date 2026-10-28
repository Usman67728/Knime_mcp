# KNIME headless execution — root cause analysis

## Summary
Headless execution hung for 600s on every run. Root cause: **node-settings
templates carried stale execution state from the instance they were harvested
from**, which made KNIME's loader fail the node, so the workflow never executed
and NodePit's JVM never exited.

## What was NOT the cause (ruled out by experiment)
- **Arg form.** NodePit's picocli parser rejects `-data=DIR`; it only accepts
  `-data DIR`. With the space form the JVM boots and loads the workflow. (Fixed
  in `executor.py`; this alone did not fix the hang.)
- **Pipe back-pressure.** A draining stdout reader revealed more output but did
  not change the outcome.
- **JVM exit / teardown deadlock.** Real, KNIME-authored workflows execute and
  exit 0 through the same NodePit path (e.g. `KNIME_project2` exits 0 in ~100s).
  So the executor and app exit fine. The hang was upstream: nodes never executed.
- **`.knwf` vs `.knime`.** `.knwf` is a workflow *archive* used for export/import;
  `workflow.knime` is the plain XML *inside* a workflow folder and is what the
  executor loads. Both are correct for their roles.

## The actual mechanism (proven)
1. A workflow containing one **verbatim, KNIME-authored** node + our
   hand-written `workflow.knime` **executes and exits 0 in ~23s**.
   => our `workflow.knime` writer is correct.
2. The **raw harvested template**, put in that same workflow, **executes**
   (reaches `KNIME-Worker-0`; only fails on data-type mapping at runtime).
   => the template content is valid enough to run.
3. Our **rendered** template **never loads**: *"Errors loading flow variables
   into node: Config for key `specs` not found"* -> node never configures ->
   workflow never executes -> JVM idles until timeout.
   => `templates.render()` (or the template definition) is what breaks it.

The break traced to the `csv_reader` template's `clear` list:

    "clear": ["model.transformationParameters"]

`model/transformationParameters` contains THREE things:
  - `specs`                 -> REQUIRED by KNIME for flow-variable/spec load
  - `enforceTypes`          -> forces the stale type map on
  - `columnTransformation`  -> 13 stale per-column rules for the old CSV

Blanking the whole config wiped `specs`. During `WorkflowManager.postLoad`,
`Node.loadDataAndInternals` -> `restoreWarningMessage` logs the flow-variable
load error, the node ends in an error state, and the workflow never runs.

## Fix applied
`src/knime_mcp/templates/csv_reader/template.json`:
- `clear: ["model.transformationParameters"]`  ->  `clear: []`   (keep `specs`)
- added `post_set` `model.transformationParameters.enforceTypes = false`
  so KNIME re-infers column types from the actual input file instead of
  enforcing the harvested instance's types.

With this, the workflow now **loads, configures, and executes** (confirmed by
`KNIME-Worker-0-CSV Reader` appearing in the log).

## Remaining issue (NOT a hang)
With the fix the CSV Reader executes but can still fail at *runtime* with
`MappingRuntimeException: Row ... can't be converted to the configured data
types` when the input CSV's columns differ from the harvested template's
columns (the template was harvested against a specific CSV). The correct next
step is to build the CSV Reader's `spec`/type-map from the actual input file's
header at render time (or clear `enforceTypes` per-column), so column types
match whatever CSV is supplied.

Note: `table_creator`/`table_writer`/`sorter`/`column_filter`/... already had
`clear: []`, so `csv_reader` was the only template with the workflow-breaking
clear. The table_creator->table_writer isolation also timed out, but its error
("dataType is null") points at its own stale `spec`, the same class of bug.