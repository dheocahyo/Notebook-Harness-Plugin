---
expect:
  title: /^[^\n]{1,80}$/
  intent: /\S/
  code: string
---
Added "{{input.title}}" [2] at the bottom, below "Load raw data and check schema" [1]; ran ok in 0.1s; by_product: new DataFrame 3×2 (from df_clean 37×7); no nulls.
nh: cell=nh-5e2a9c4f17 exec=2 turn=1/1 retries=0/2 waits=0/2 undos=0/3
--- output ---
[display]
        revenue  orders
region                 
East     968.42      10
South    921.70       9
North    811.77      11
West     398.85       7
[display]
         revenue  orders
product                 
Gadget   1435.80      11
Widget    987.81      12
Gizmo     677.13      14
--- self-check ---
by_product: new DataFrame 3×2 (from df_clean 37×7); no nulls
by_region: new DataFrame 4×2 (from df_clean 37×7); no nulls
df_clean: new DataFrame 37×7 (from df 43×6); nulls: order_date 1
same shape and nulls: df
--- next ---
Reply to the user about "{{input.title}}" [2]: (1) what the cell does; (2) why this approach; (3) judgment calls they may want to change; (4) the real numbers from the output, surprises first (see 'check this'); (5) any failed attempts; (6) one proposed next cell, as a title they can approve with "go". Name cells by title and [n]. Do not write a second cell.
