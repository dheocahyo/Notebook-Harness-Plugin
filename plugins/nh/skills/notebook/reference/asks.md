# When nh asks first (E122)

Some cells need the user's yes before nh writes them:
- a cell that installs packages (`!pip install`, `%pip install`,
  `%conda install`, `!uv add`: rule L009) or removes them with
  `%pip uninstall` or `%conda remove`;
- a cell that reaches a host the project hasn't approved (rule L012): a URL
  given to a reader or any other call (`pd.read_csv("https://…")`, a name
  holding one), `requests`, `httpx`, `urllib`, `socket`, `!curl`, `!wget`,
  `!git clone`, `!scp`. The question names the hosts, never the URL.
- a cell that writes, creates or removes a file or folder outside the project
  (rule L013): `df.to_csv("~/…")`, `plt.savefig("/…")`,
  `open("../../x.txt", "w")`, `shutil.copy`, `!cp`, `>`, `%%writefile`. The
  question names the paths. A relative path starts from the notebook's
  folder: from `notebooks/`, `../data/processed/` is inside. Keep files inside
  the project (for example `data/processed/` or `reports/`) unless the user
  named the place.

Unless `harness.toml` sets the rule to another level, nh refuses such a call
with `E122`, writes nothing, and puts the question in its `Next:` line.

Don't ask before the call: write the cell and send it, and let nh ask. nh
grants only a yes to its own question, so asking first means asking twice.

The better way to install stays `uv add <pkg>` with Bash (in a conda project:
add it to environment.yml, then `nhctl env sync`), after the user's yes,
then the cell without the install. A cell that installs is the fallback, for
when the user wants the cell itself; nh asks them about it.

## Approved hosts

A cell whose every host is in `.nh/state/approved_hosts.json` is written with
no question. `/nh:init` puts the host of the data URL it set up there, so the
first cell reads the data straight away. A yes to `E122` approves that one
cell, once, never the host: the next cell that reads from it asks again. To
approve a host for good, the user adds it to that file themselves (a JSON
list of host names: `["data.example.org"]`); a subdomain needs its own entry.
You can't write the file, and nh has no command for it.

## In the message that gets E122

1. Ask the user that question in chat, as nh gives it. One question, then
   stop. Write nothing else in this message: no other cell, and no way around
   it (don't drop the install, the download or the write into Bash on your
   own, don't split the cell).
2. Keep the call exactly as it was. Never rephrase its code between the
   question and the retry: changed code is a new question.
3. One question per message. A second cell nh asks about in the same message
   gets `E122` "already waiting for the user's answer": ask only the first
   question.

## In the user's next message

- **A yes** ("yes", "ok", "sure", "approved", or "go" on its own): send the
  exact same call again, before any other cell: the same tool, the same cell
  (`cell_id` for an edit) and the same `code`. The title and notes may change.
  nh accepts it once, as this message's one cell. A "go" here answers the
  question; it doesn't mean "do the next step". Another cell nh would ask
  about gets `E122` "the user's yes … is for the other cell": send the
  approved call, and propose the other cell in your reply.
- **Anything else** ("no", "use uv add", "what does it change?", "go on",
  "yes, but …"): the cell is not approved. Drop the call and do what the
  message asks. For a package that is usually `uv add <pkg>` with Bash (in a
  conda project: environment.yml, then `nhctl env sync`), after the user
  agreed, then the cell without the install. For the network: the user
  downloads the file into the project (for example `data/raw/`), then a cell
  reads it there. Never fetch it yourself: no `curl` or `wget` with Bash, no
  WebFetch, no other cell. For a write outside the project: write the file
  inside the project instead (for example `data/processed/` or `reports/`)
  and tell the user where it is.
- The yes only counts in the very next message. Later, nh asks again.

## Other cases

- A cell you write in the asking message replaces its question: the user's
  yes then approves nothing.
- A retry that keeps the install (`nh_edit_cell` after the approved cell
  failed) asks again: a yes covers one call.
- Re-running a cell that installs (`nh_run`) runs the install again without a
  question from nh, as does re-running one that downloads or writes outside
  the project: ask the user first, as for any re-run.
- Headless runs (`NH_HEADLESS=1`, set for runs like `claude -p` where nobody
  answers): the refusal stands. Tell the user the cell needs their yes in an
  interactive session, and write nothing.
- nh:cell-writer inside nh:qa-cell can't ask the user: its `E122` goes back
  to the workflow with the question in it. The report's outcome is then
  `needs_approval`, with nh's question and the exact call: if the report is
  for this message, ask the question; after the user's yes, send that call
  yourself ([qa-workflow.md](qa-workflow.md)).
