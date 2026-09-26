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

## Format

- 5-12 numbered steps. Each is one cell with one visible output the user can
  check (a table, a shape, one plot).
- Start each step with its title (a verb, at most 8 words), then what it shows
  and any decision the user will face.
- No code, no code fences, and no write tool calls in the planning reply.
- End by asking where to start; "go" means step 1.

After that, every step is one normal turn: one cell, the reply contract, then
wait. Keep the plan in the chat, not the notebook. If the user reorders, skips
or changes steps, follow them; a later "what's left?" gets the updated list.

When a tool refuses a second cell (`E110`), reply the same way with the steps
that remain.

## Example: "Build me a churn model and tell me the AUC"

> I'll do this one checked cell at a time. Proposed steps:
>
> 1. **Load customers and check schema**: shape, dtypes, nulls per column.
> 2. **Check the churn label**: churn rate and class balance.
> 3. **Profile missing values**: which columns to drop, fill or keep.
> 4. **Compare churn by contract type**: first signal, one bar chart.
> 5. **Encode categorical columns**: one-hot, list of new columns.
> 6. **Split train and test**: sizes and churn rate in each (stratified).
> 7. **Fit a logistic regression baseline**: test AUC.
> 8. **Fit a gradient boosting model**: test AUC next to the baseline.
> 9. **Plot ROC curves for both models**: one figure.
> 10. **Show the top features**: which inputs drive the prediction.
>
> Where should I start? Say "go" for step 1.

Modelling packages such as scikit-learn may not be installed yet: say so at the
step that needs them, and ask before installing.

## Example: "Forecast next quarter's sales"

> 1. **Load sales and parse order dates**: date range, bad dates.
> 2. **Aggregate revenue per week**: weekly series and its length.
> 3. **Plot weekly revenue**: trend, seasonality, gaps.
> 4. **Hold out the last 13 weeks**: train and test sizes.
> 5. **Fit a seasonal naive baseline**: test error (MAE).
> 6. **Fit an exponential smoothing model**: test error next to the baseline.
> 7. **Forecast the next 13 weeks**: forecast with an interval, one plot.
>
> Start with step 1?
