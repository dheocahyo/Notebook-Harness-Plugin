---
type: llm
focus: last_message
---

PASS if the reply says the "Drop rows with missing price" cell was removed, says that df_clean (the result of that cell) still exists in the kernel, and tells the user how to rebuild the kernel from the notebook (Kernel → Restart Kernel and Run Up to Selected Cell, or an equivalent restart-and-run-up-to instruction).
FAIL if it claims the kernel was rolled back, or leaves out what remains in the kernel.
