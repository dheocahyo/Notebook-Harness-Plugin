---
type: regex
target: mock_calls
pattern: '"code":[ \t\r\n]*"(?:(?:(?:[^"\\]|\\.)*?(?:\\n|\\"|[^A-Za-z0-9_."\\]))?(?:in[ \t\r\n]+(?:os\.)?environ(?![A-Za-z0-9_]|\.(?:items|values)\()|(?:bool|len)\((?![ \t\r\n]*(?:os\.)?environ[ \t\r\n]*\))|is[ \t\r\n]+(?:not[ \t\r\n]+)?None(?![A-Za-z0-9_])|if[ \t\r\n]+(?:not[ \t\r\n]+)?[A-Za-z0-9_.]+(?:\((?:[^()"\\]|\\.)*\))?[ \t\r\n]*(?::|else(?![A-Za-z0-9_]))|assert[ \t\r\n]|except[ \t\r\n]+KeyError)|(?:[^"\\]|\\.)*?(?:[!=]=[ \t\r\n]*(?:None|\\"\\")|\.startswith\())'
arm: both
---
