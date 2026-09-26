---
type: fixed
---
Kernel ≠ notebook: `df_clean` still holds results from the undone "Drop rows with missing price" [2]. To rebuild: select the last cell that should count, then Kernel → Restart Kernel and Run Up to Selected Cell.
Removed "Drop rows with missing price" [2] and its note. The code is kept in .nh/history.
nh: cell=nh-7d41c9e2a5 exec=- turn=0/1 retries=0/2 waits=0/2 undos=1/3
--- next ---
Tell the user, in plain words: what was undone, which variables still hold the old results, and which cells are now outdated. Offer to redo the step differently. Wait.
