Syntax example commands 
All commands can be shown with "commands" in the console 

Example how to show eco and force rounds only:
set MAP inferno
set SIDE T
show ecos
heatmap ecos
search smoke phase:early

Single Player Prep:
role
player xDiedrich
heatmap rotations xDiedrich
report player xDiedrich

Recent form only against a specific opponent:
set LAST_N 5
set OPPONENT Holy Smokes
vetos
anomalies
report html

Reusable info template 
team SusukaSprinters
download
parse
set ROSTER main
report both
export all csv
THEN RERUN WITH 
py csc_analyst.py -r scout.txt
