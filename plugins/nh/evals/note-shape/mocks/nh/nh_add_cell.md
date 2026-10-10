---
expect:
  title: /^[^\n]{1,80}$/
  intent: /\S/
  code: string
---
Added "{{input.title}}" [2] at the bottom, below "Load raw data and check schema" [1]; ran ok in 0.9s; corr_matrix: new DataFrame 3×3 (from df 43×6); no nulls.
nh: cell=nh-a0c3e57b19 exec=2 turn=1/1 retries=0/2 waits=0/2 undos=0/3
--- output ---
[out]
          order_id  units  price
order_id      1.00   0.01  -0.10
units         0.01   1.00  -0.12
price        -0.10  -0.12   1.00
[image 1: 600x500 png]
--- self-check ---
corr_matrix: new DataFrame 3×3 (from df 43×6); no nulls
ax: new Axes
fig: new Figure
heatmap: new AxesImage
same shape and nulls: df
--- next ---
Reply to the user about "{{input.title}}" [2]: (1) what the cell does; (2) why this approach; (3) judgment calls they may want to change; (4) the real numbers from the output, surprises first (see 'check this'); (5) any failed attempts; (6) one proposed next cell, as a title they can approve with "go". Name cells by title and [n]. Do not write a second cell.
