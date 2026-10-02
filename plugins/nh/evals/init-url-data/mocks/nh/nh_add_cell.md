---
type: agent
expect:
  title: /^[^\n]{1,80}$/
  intent: /\S/
  code: string
---
This call is nh_add_cell. Answer it as the nh server described below.

{{file:fixtures/nh-server.md}}
