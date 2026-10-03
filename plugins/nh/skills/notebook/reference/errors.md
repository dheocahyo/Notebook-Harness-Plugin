# Errors and refusals

## When your cell fails

1. Read the `--- output ---` traceback tail and the `--- next ---` line.
2. If the cause is clear, fix the SAME cell with `nh_edit_cell` (its
   `cell_id` is in the `nh:` line). Never add a new cell to fix it.
3. At most 2 retries per message. Each retry re-runs the cell; it doesn't use
   another cell.
4. Once it runs, the reply adds what failed first and what you changed.
5. If it still fails after 2 retries, or the cause is unclear, stop and explain
   with the template below. Offer **undo**, then wait.

Template (plain words, no stack dump):
> "Parse signup dates" [4] still fails. The failing code is
> `pd.to_datetime(df["signup_date"])`. Python said: "day is out of range for
> month, at position 7". The likely cause is an impossible date in the data
> (row 7 is 2023-11-31). One fix: parse with `errors="coerce"`, which turns bad
> dates into missing values, then count them. Say **undo** to remove the cell,
> or tell me how you'd like to handle bad dates.

Quote at most the last line of the traceback, and point at the code by quoting
it, never by line number.

## Common data errors

| Python says | Usual cause | First move |
|---|---|---|
| `KeyError: 'Price'` | the column is named differently | `nh_inspect(view="var", name="df")`, then use the real name |
| `ValueError` / `DateParseError` in `to_datetime` | an impossible or mixed-format date | `errors="coerce"`, then show the rows that became missing |
| `could not convert string to float` | numbers stored as text (`"1,200"`, `"n/a"`) | `pd.to_numeric(..., errors="coerce")`, then count the failures |
| `TypeError: agg function failed … dtype->object` | a numeric-looking column is text | convert it first, as above |
| `ModuleNotFoundError` | the package is not in the project env | ask the user; after a yes, `uv add <pkg>` with Bash (in a conda project: add it to environment.yml, then `nhctl env sync`), then re-run. Or use an installed package |
| `MemoryError`, very slow cell | the data is large | work on a sample first (`nrows=` or `.sample(…, random_state=0)`); say so |
| `SettingWithCopyWarning` | assigning into a filtered frame | build the new column with `.assign(...)` on a named result |
| `UnicodeDecodeError` | not UTF-8 | `encoding="latin-1"` in the reader |
| `ParserError: Expected N fields` | wrong delimiter | `sep=None, engine="python"` in `read_csv` |

## Hard-rule rejections (`E120`): nothing written, your cell is not used

Fix every listed problem and call again, except for L009, L012 and L013 (see their rows).

| Rule | Problem | Fix |
|---|---|---|
| L001 | empty code | write the cell's code |
| L002 | `# %%`, `# In[ ]`, `# <codecell>` or `# COMMAND` separators | Keep only the first step in this cell (no separators) and propose the rest in your reply |
| L003 | title over 8 words, multi-line, or with markdown/HTML | a short plain title: what the cell does |
| L004 | notes: not 2-5 bullets, a bullet over 40 words, or markdown in a bullet | 2-5 plain one-sentence bullets |
| L005 | no intent | the user's ask in one line |
| L007 | syntax error (for the kernel's Python version) | fix the syntax |
| L008 | the cell writes an `.ipynb` | never; nh owns the notebook |
| L009 | only when `harness.toml` makes it an error (`package_install = "error"`, or `"hint"` under `[lint] mode = "strict"`; by default an install is no rejection: nh asks the user first, `E122` below): a package install: `!pip install`, `%pip install`, `%conda install`, `!uv add` and the like | don't call again yet. Ask the user whether to install it; after a yes, run `uv add <pkg>` with Bash (in a conda project: add it to environment.yml, then `nhctl env sync`), then write the cell without the install |
| L012 | only when `harness.toml` makes it an error (`network = "error"`, or `"hint"` under `[lint] mode = "strict"`; by default nh asks the user first, `E122` below): the cell reaches a host the project hasn't approved: a URL given to a reader or any other call (`pd.read_csv("https://…")`), `requests`/`httpx`/`urllib`/`socket`, `!curl`, `!wget`, `!git clone`, `!scp`. The refusal names the hosts | don't call again yet. Ask the user to download what the cell needs into the project (for example `data/raw/`), then write the cell to read it from there. Never fetch it yourself with Bash or another cell |
| L013 | only when `harness.toml` makes it an error (`outside_write = "error"`, or `"hint"` under `[lint] mode = "strict"`; by default nh asks the user first, `E122` below): the cell writes, creates or removes a file or folder outside the project: `df.to_csv("~/…")`, `plt.savefig("/…")`, `open("../../x.txt", "w")`, `shutil.copy`, `!cp`, `>`, `%%writefile`. A relative path starts from the notebook's folder. The refusal names the paths | don't call again yet. Write the files inside the project instead (for example `data/processed/` or `reports/`), or tell the user where the cell would write and let them change it themselves |
| L010 | `%%markdown`/`%%html`, or `Markdown()`/`HTML()`/`Latex()` with prose | put the text in the chat reply |
| L011 | code that would show an env var's value, secret or not: `print(os.environ["KEY"])`, `os.getenv("KEY")` as the last line, a variable holding one, `os.environ.keys()` shown, `%env`, `!env`, `!printenv`, `!echo $KEY`, `!cat .env`, `.env` read and shown | check it without showing the value: `print("KEY" in os.environ)` or `print(bool(os.getenv("KEY")))`; list names with `sorted(os.environ)`; pass it on (`create_engine(url)`) without printing it |
| L014 | only under `[lint] mode = "strict"`: a shown name that says it holds a secret (`print(api_key)`) | show `bool(api_key)` instead, or leave it out of the output |

After 3 rejections in one message: `E121`. Stop, tell the user what you are
trying to write, and ask how to proceed.

Hints come after the run and don't block, unless `harness.toml` sets
`[lint] mode = "strict"`: then every hint is refused as E120 like the rows
above. L014 (a shown name that says it holds a secret, such as `print(api_key)`)
matters most: don't show that name again; show `bool(api_key)` when the user
needs to know it is set.

## Refusals

Every refusal's first line is written for the user; `Next:` is for you. Do what
it says. Inside the nh:qa-cell workflow, nh's cell writer returns every refusal
but E120 to the workflow instead of asking the user. The most common:

| Code | Meaning | Do |
|---|---|---|
| E101 | nh's hooks didn't stamp this call, or two identical calls (one from a subagent) arrived together | tell the user; suggest `/nh:status`. Retry at most once (for two identical calls, just retry once) |
| E102 | the call belongs to an earlier message; or, with "nh missed this message", nh never recorded the user's latest message | stop and wait; for a missed message, first ask the user to send it again |
| E103 | only the main conversation, or nh's cell writer inside the nh:qa-cell workflow, may change the notebook; other subagents are read-only | return findings to the main conversation |
| E104 | plan mode | describe the cell instead |
| E105 | not an nh project | suggest `/nh:init` |
| E106 | Claude Code too old for turn tracking | suggest `claude update` |
| E107 | the nh:qa-cell run is done, the user sent a new message since it started, or it started over an hour ago | the writer returns the refusal to the workflow; it doesn't retry |
| E108 | the nh:qa-cell workflow is writing this message's cell | reply when its report arrives; write nothing. To change course, stop it first |
| E109 | the message is explain-only ("explain …" or `/nh:explain` naming no change verb: fix, change, add, update, rewrite, refactor, make), a `/nh:plan`, or a "run next 3" ask, so nh changes no cell in it; a mode typed mid-message holds for the rest of it | do what `Next:` says: a numbered walkthrough, the numbered plan or the one question, in chat. nh:cell-writer returns the refusal to the workflow |
| E110 | one new cell per message; this message's cell exists | reply with the remaining steps as a numbered list; ask which next |
| E111 | out of retries | explain with the template above; offer undo |
| E112 | the cell already ran OK this message | report and wait; changes wait for the next message |
| E113 | this message's cell is a different one | propose the change as the next step |
| E114 | re-running an older cell uses the message's cell, already used | ask before re-running next message |
| E115, E116 | undo or wait limit reached | tell the user; stop |
| E117 | the cell was stopped before it finished (interrupted), so nh asks before changing or re-running it | say what ran before it stopped; ask whether to re-run it, change it or leave it; wait |
| E118 | the user typed into nh's cell before it ran, so nothing ran and nh asks before running or changing it | show the user their change; ask whether to run it as it is, restore nh's version (`nh_undo`) or leave it; wait |
| E122 | the cell needs the user's yes first: by default, a cell that installs packages (or removes them with `%pip uninstall`/`%conda remove`; L009), reaches a host the project hasn't approved (a URL read, a download, `requests`, `!curl`; L012) or writes outside the project (`~/…`, an absolute path, `../..` above the project; L013), named in the refusal. `Next:` holds the question | ask that one question in chat, then stop; write nothing else. If the user's next message is a yes ("go" alone counts), send the exact same call again, first (same tool, cell and `code`; title and notes may change); anything else drops it. A second ask in the same message gets "already waiting for the user's answer": ask only the first. In the yes message, another asked-for cell gets "the user's yes … is for the other cell": send the approved call, propose the other. Headless (`NH_HEADLESS=1`): no one can answer; tell the user and write nothing. nh:cell-writer returns it to the workflow. See [asks.md](asks.md) |
| E125 | the code holds nh's `[redacted:…]` marker: nh shows you secrets that way, never their values | never retype the marker. For `[redacted:NAME]`, read NAME without printing it: `os.environ.get("NAME")`, falling back to the project's `.env` as the first cell reads `DATA_URL` (nh doesn't load `.env` into the kernel). A `[redacted:<kind>]` marker (`password`, `token`, `url-userinfo`, …) names no variable: ask the user to edit that line in JupyterLab. Code that must hold the marker's text itself builds it in pieces (`"[" + "redacted:"`). If the hidden value is no secret: nh hides a value (in `.env` or the environment) whose name looks secret (TOKEN, SECRET, PASSWORD, KEY…) and any other `.env` value of 16+ characters unless its name ends in a word for a place or label (`_PATH`, `_DIR`, `_URL`, `_HOST`, `_NAME`, `_SCHEMA`…); tell the user they can rename it to end in such a word (`MODEL_VARIANT` → `MODEL_VARIANT_NAME`) or unset it |
| E130, E131 | no project JupyterLab, or one without collaboration | ask the user to run `nhctl lab start`; `nh_inspect` still works |
| E132 | notebook not found | pass `notebook=` with a candidate from the message |
| E133 | kernel busy (a cell is running, or nh's cell is RUNNING or QUEUED) | wait, or `nh_run(mode="wait")` for nh's own cell; add nothing |
| E134 | no usable kernel | ask the user to select or restart the kernel in JupyterLab |
| E135 | couldn't sync with JupyterLab | retry once; then ask the user to reload the notebook tab |
| E136 | not a Python kernel or nbformat 4 notebook | tell the user nh can't use this notebook |
| E137 | Windows | Claude Code and JupyterLab must run under WSL |
| E138 | the notebook is open in another Jupyter server | ask the user to close it there |
| E140 | cell id not found | `nh_inspect(view="outline")`, use a current id |
| E141 | the user changed the cell since nh last saw it | show the change; ask; `base_sha` or undo's `force=true` only after they confirm |
| E142 | the cell was already deleted in JupyterLab | nothing undone; say what is still in the kernel and follow `Next:` |
| E144 | a cell the user wrote, edited without `base_sha` | `nh_inspect(view="cell")`, then call again with its `sha` as `base_sha` |
| E145 | a markdown cell; nh only writes code cells | suggest the wording; the user changes it in JupyterLab |
| E143 | nothing to undo recently | ask which cell (pass `cell_id`) |
| E199 | nh internal error; nothing more was written | tell the user; details are in `.nh/logs/gateway.log` |
