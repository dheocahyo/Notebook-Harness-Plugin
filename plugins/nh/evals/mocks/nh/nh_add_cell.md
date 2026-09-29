---
expect:
  title: /^[^\n]{1,80}$/
  intent: /\S/
  code: string
---
Added "{{input.title}}" [2] at the bottom, below "Load raw data and check schema" [1]; ran ok in 0.4s; df_clean: new DataFrame 37×6 (from df 43×6); no nulls.
nh: cell=nh-7d41c9e2a5 exec=2 turn=1/1 retries=0/2 waits=0/2 undos=0/3
--- output ---
[stdout] rows: 43 -> 37
[out]
   order_id  order_date region product  units  price
0      1001  2024-01-01  North  Widget      4  11.25
1      1002  2024-01-08   West   Gizmo     11   7.25
2      1003  2024-01-15   East  Gadget      6  26.40
3      1004  2024-01-22  South  Gadget      1  23.40
5      1006  2024-01-08  North   Gizmo      3   6.89
--- self-check ---
df_clean: new DataFrame 37×6 (from df 43×6); no nulls
same shape and nulls: df
--- next ---
Reply to the user about "{{input.title}}" [2]: (1) what the cell does; (2) why this approach; (3) judgment calls they may want to change; (4) the real numbers from the output, surprises first (see 'check this'); (5) any failed attempts; (6) one proposed next cell, as a title they can approve with "go". Name cells by title and [n]. Do not write a second cell.
