#!/usr/bin/env python3
"""Check citation & figure-number consistency in the paper HTML.

Usage:
    python3 scripts/check_paper_refs.py [paper/Machine_Economy_Lab_Research_Paper.html]

Exits non-zero if any citation referenced in the body is missing from the
References list, or if figure numbers are duplicated. Run after every
chapter edit to prevent the citation-number collisions that happened with
Chapter 2 (body [22]/[24]/[25] vs existing References).
"""
import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "paper/Machine_Economy_Lab_Research_Paper.html"
html = open(path, encoding="utf-8").read()

# --- split body vs References section ---
refs_marker = html.find("<h2>References</h2>")
body, refs_section = html[:refs_marker], html[refs_marker:]

# --- citations used in body (paragraph text only, not TOC/refs) ---
body_cites = set()
for m in re.finditer(r"\[(\d+)\]", body):
    body_cites.add(int(m.group(1)))

# --- references declared in the list ---
declared = set()
for m in re.finditer(r'class="ref-entry">\[(\d+)\]', refs_section):
    declared.add(int(m.group(1)))

# --- figure numbers in body text + captions ---
fig_in_text = set()
for m in re.finditer(r"Figure (\d+)", body):
    fig_in_text.add(int(m.group(1)))

problems = 0
missing = sorted(body_cites - declared)
if missing:
    problems += 1
    print(f"MISSING references (cited in body, not in References): {missing}")
else:
    print("OK: every body citation [n] has a References entry.")

unused = sorted(declared - body_cites)
print(f"Info: References entries never cited in body: {unused}")

# duplicate figure captions in List of Figures section only
lof_start = html.find("<h2>List of Figures</h2>")
lof_end = html.find("<h2>Appendix", lof_start)
lof = html[lof_start:lof_end if lof_end > 0 else len(html)]
figs = re.findall(r"Figure (\d+):", lof)
dups = sorted({f for f in figs if figs.count(f) > 1})
if dups:
    problems += 1
    print(f"DUPLICATE figure captions: {dups}")
else:
    print("OK: no duplicate figure captions.")

sys.exit(1 if problems else 0)
