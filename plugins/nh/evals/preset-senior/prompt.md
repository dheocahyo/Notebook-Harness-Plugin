---
description: In a senior project (harness.toml's [preset] level = "senior"), SessionStart tells Claude to answer the reply contract in one short paragraph without headings or lists, and not to explain common pandas methods. Asked for a cleaning and summary cell, Claude writes it with at most one comment line (senior's budget) and replies in short plain prose with the real numbers; a junior project's reply, in headed parts, fails the same rubric (design §6.9). The mock doesn't lint, so a grader reads the code as L105 would at senior's ratio; L105 itself is tested by the unit and gateway tests.
tags: [ci]
plugins: ["../.."]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
expected_outcome: The SessionStart context holds the senior line; one nh_add_cell call whose code has at most one line with a comment; a reply of one or two short paragraphs of plain prose, with no headings, labels or lists, saying what the cell does and what to check, with real numbers from the output (East leads with 968.42, Gadget with 1435.80), ending with one proposed next cell and defining no common pandas method.
---

Add a cell that makes df_clean, a clean copy of df for analysis: parse order_date as dates (impossible dates become missing), drop the rows with no price, add a revenue column (units times price). Then display only two tables, by_region and by_product, with total revenue and order count (columns revenue and orders), each sorted by revenue; print nothing else.
