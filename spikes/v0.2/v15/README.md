# Spike V15: does the desktop app show nh's "ask" dialog?

**What it checks** (plan v0.2, V15). C8 makes nh's hooks *ask* you, instead of blocking, before
Claude installs packages, fetches a web page or edits `harness.toml`. That only works if the
Claude desktop app shows its native permission dialog when a hook asks, ideally with the hook's
reason. This spike checks that with a small stand-in hook. C8 waits on it: if no dialog appears,
the build stops and asks you how to go on.

No nh plugin is involved: the folder has only the hook (`v15_hook.py`, Python 3 stdlib), its
`settings.json` and a stand-in `harness.toml`. Allow about 10 minutes.

## Setup (a scratch folder)

```sh
cd <your clone> && git fetch origin && git checkout release/0.2.0 && git pull
rm -rf ~/v15-spike && mkdir -p ~/v15-spike/.claude
cp spikes/v0.2/v15/settings.json spikes/v0.2/v15/v15_hook.py ~/v15-spike/.claude/
cp spikes/v0.2/v15/harness.toml ~/v15-spike/
```

`python3` must be on your PATH (macOS has `/usr/bin/python3`). Open `~/v15-spike` as a local
folder in the Claude desktop app's Code tab. If it asks whether to trust the folder, say yes:
the hook lives in the folder's settings.

## What to type

When a dialog offers "don't ask again" (or similar), pick the plain allow or deny instead, so
each prompt starts from the same state.

| # | You type | You should see |
|---|---|---|
| 1 | `Run pip install --dry-run six in the shell.` | A permission dialog for the Bash command. Does it show **"[V15] nh asks: this command installs packages into an environment. Allow it?"**? Allow it: pip runs a dry run and changes nothing |
| 2 | `Fetch https://example.com and tell me the page title.` | A dialog for the fetch, with "[V15] nh asks: this fetches a page from outside the project. Allow it?". **Deny** it: Claude should say it was refused |
| 3 | Switch the permission mode to **accept edits** (the mode picker, or Shift+Tab), then `Add the line # v15 at the end of harness.toml.` | Still a dialog, with "[V15] nh asks: this changes harness.toml, nh's guardrail settings. Allow it?". Allow it |
| 4 | Still in accept edits: `Write notes.txt with the word hi.` | **No** dialog: the hook says nothing for other files, and accept edits writes it |

Optional, for comparison: the same four in the terminal (`cd ~/v15-spike && claude`).

## What to send back

For prompts 1-3:
1. Did a dialog appear (yes/no)?
2. Did it show the "[V15] …" reason: word for word, cut short, or not at all?
3. Which buttons did it offer?
4. What happened after your answer?

For prompt 4: did a dialog appear? A screenshot of one dialog helps. Also send
`~/v15-spike/.spike/v15.jsonl`: it holds only times, tool names and which ask fired, with no
prompts or commands.

When you're done, delete `~/v15-spike`.
