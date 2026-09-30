"""«Сборки» (build projects): plan a PC / server from used parts, track the best offer for every
part, alert when a part beats its target or the whole build fits the budget.

knowledge  curated parts, templates, PSU sizing, LLM memory maths (no prices from any LLM)
planner    goal text → requirements → slots with alternatives → default choices → checks (+ optional LLM)
market     which ads fit a part; typical prices from the app's own price history, else rough «ориентир»
scoring    value per euro of an offer (€/GB VRAM, bandwidth, …) and warning flags from the ad text
tracker    best offers per slot, totals vs budget / buying new, price trends, alerts
store      SQLite tables (created in db.py: Database._migrate_projects)
"""
