# Big asks: plan, don't build

## When to plan

Plan instead of writing a cell when the ask names an end result that needs 5
or more cells: "build a churn model", "do a full EDA", "clean this and train
something", "write the whole notebook".

Not a big ask:
- one clear step ("drop rows with missing price"): do it;
- a short list of concrete steps ("drop the nulls, then plot price by
  region"): do the first step as this message's cell, and propose the rest as
  numbered next steps in the reply.

When unsure, do the first step and propose the rest.

Look before you plan: `nh_inspect(view="outline")` for the cells the notebook
holds, and `nh_inspect(view="vars")` for the data and what is installed.

## Format

- 5-12 numbered steps. Each is one cell with one visible output the user can
  check (a table, a shape, one plot).
- Start each step with its title in bold (a verb, at most 8 words), then a
  colon and the one result it shows, in a few words: "**Drop customers
  without a signup date**: rows before and after." One result, not two joined
  by "and": "the dates that fail to convert", not "the date type, and the
  dates that fail to convert". A decision the user will face is a second short
  sentence: "**Handle the missing ages**: the rows without an age. You choose
  whether to drop, fill or keep them." One action per step: a step that would
  do two things ("list the names, then merge them") is two steps.
- Plain words only: no code, commands, constants, method or variable names,
  and no backticks. Say "the first rows", not the method. No code fences and
  no write tool calls in the planning reply.
- Start from what the user asked: the reply opens with the plan and says
  nothing about what the notebook holds, before the list or after it
  ("cell [1] already loads the data" and "I left out the loading step" are
  recaps: leave them out). A step the notebook already holds (the loader, a
  check that ran) is not listed.
- A package that isn't installed yet (scikit-learn for a model): say so in
  words at the step that needs it (when that step comes, ask the user before
  installing it). A package that `nh_inspect` lists under "installed:",
  "(not imported)" or not, gets no word: "**Plot signups per week**: one line
  chart.", not "… one line chart. This uses matplotlib, which is installed."
- End with one question, in these words: "Where should I start? Say "go" for
  step 1, or "run the next 3" to do several in one reply." (or another number,
  or "run steps a-b"; you then ask once before writing them).

After that, every step is one normal turn: one cell, the reply contract, then
wait (an approved batch is the one exception: below). Keep the plan in the
chat, not the notebook. If the user reorders, skips or changes steps, follow
them and re-print the whole updated list; a later "what's left?" gets the
updated list too.

When a tool refuses a second cell (`E110`), reply the same way with the steps
that remain.

## The batch path

"run the next 3" or "run steps 2-4" asks for several plan steps in one reply.

1. **The ask message.** Write nothing (nh refuses it: `E109`). Ask one
   question in chat, "Run steps a-b in one reply?", with the plan's numbers
   of the steps the user asked for, then stop. A yes writes them one cell per
   step, not as one cell. nh runs at most `max_batch` steps at once (5 by
   default): for more, ask about the first ones.
2. **The yes message** (a whole-message yes, or "go" alone). Write the steps
   in order, one `nh_add_cell` each, with a short report after each (what it
   did, the real numbers). nh counts the batch's own steps, 1 to N: its "step
   1 of 3" is the first step the user approved (plan step 2 after "run steps
   2-4"). To the user, name each step by its plan number and title. Each
   result's `--- next ---` block says whether to go on. Stop at the first
   error, "check this" section or `E12x` refusal (an nh question, `E122`,
   included) and reply as that result says (its `--- next ---` block, or the
   refusal's `Next:` line): what the steps did, where and why the batch stopped, which
   planned steps did not run, and after an `E122` nh's question word for
   word; then wait. No retry or fix of the failing step in that reply.
   `E123` means the batch has stopped: report and wait. Under ultracode, "In
   an approved batch" in [qa-workflow.md](qa-workflow.md) applies.
3. **At the end of that reply** (after the batch's last step, or where it
   stopped): list the remaining steps as a numbered list, then "go" for the
   next one, or a new "run the next N".

Any other answer to the question grants no batch: "no" means write nothing
and ask what to do instead; "go on" or "yes, but …" is a normal message (at
most one cell); a new "run …" is a new ask.

## Example: "Build me a churn model and tell me the AUC"

The notebook's loader, cell [1], already reads the customers.

> 1. **Check the churn label**: the share of customers who churned.
> 2. **Profile missing values**: the missing count per column. You choose
>    which columns to drop, fill or keep.
> 3. **Compare churn by contract type**: one bar chart of churn rates.
> 4. **Encode the text columns as numbers**: the list of new columns.
> 5. **Split train and test**: the churn rate in each part.
> 6. **Fit a logistic regression baseline**: test AUC. This needs
>    scikit-learn, which isn't installed yet.
> 7. **Fit a gradient boosting model**: test AUC next to the baseline.
> 8. **Plot ROC curves for both models**: one figure.
> 9. **Show the top features**: which inputs drive the prediction.
>
> Where should I start? Say "go" for step 1, or "run the next 3" to do
> several in one reply.

## Example: "Forecast next quarter's sales"

The notebook's loader, cell [1], already reads the sales.

> 1. **Parse the order dates**: the dates that fail to parse.
> 2. **Total the revenue per week**: the first weekly totals.
> 3. **Plot weekly revenue**: one line chart.
> 4. **Hold out the last 13 weeks**: the size of each part.
> 5. **Fit a seasonal naive baseline**: test error (MAE).
> 6. **Fit an exponential smoothing model**: test error next to the baseline.
> 7. **Forecast the next 13 weeks**: one plot with its interval.
>
> Where should I start? Say "go" for step 1, or "run steps 1-3" to do those
> in one reply.
