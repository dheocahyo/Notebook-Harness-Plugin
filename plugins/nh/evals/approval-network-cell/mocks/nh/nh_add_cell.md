---
expect:
  title: /^[^\n]{1,80}$/
  intent: /\S/
  code: string
error: true
---
Not written: this needs the user's yes first.
nh: E122
- L012: The cell connects to `data.example.org` over the network (`pd.read_csv`).
Next: Ask the user, then stop: 'This cell connects to `data.example.org` over the network. Run it as it is?'. After a yes, send the same call again.
