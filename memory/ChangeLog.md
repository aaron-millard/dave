# Change Log — diet-macro-assistant

## Continuous Change Logging Rule
Whenever any change is made to programs, code, scripts, bots, configs, or rules in `diet-macro-assistant`, a row must be automatically recorded here.
Trigger command: `[8]` or `pull change log`.

| Timestamp | Project | Change | Files / Target | Impact / Reason |
| --- | --- | --- | --- | --- |
| 2026-11-25 09:21 | diet-macro-assistant | Codify Continuous Change Logging & `[8] pull change log` across workspace governance, MasterRules, ProjectMemory, ErrorLog, and Technical Blueprint; backfill verified program history | `.agents/rules/workspace.md`, `rules/MasterRules.md`, `memory/ChangeLog.md`, `memory/ProjectMemory.md`, `memory/ErrorLog.md`, `TECHNICAL_BLUEPRINT.md` | Automatically log all program, bot, config, and rule changes alongside ActionLog and ErrorLog |
| 2026-11-25 09:15 | diet-macro-assistant | Initialized `ChangeLog.md` | `memory/ChangeLog.md` | Track all program, script, bot, dashboard, and config changes |
| 2026-11-14 17:59 | diet-macro-assistant | Embedded Mandatory Internal "Why" Verification rule into MasterRules and bot system prompt | `rules/MasterRules.md`, `bot/telegram_agent.py` | Require verified decimal math and active inventory evidence before finalizing meal suggestions |
| 2026-11-14 17:56 | diet-macro-assistant | Upgraded HTTP server to `ThreadingHTTPServer` with zlib-compressed cloud state & inventory sync | `bot/telegram_agent.py`, `.gitignore` | Prevent blocking webhook requests and expand Telegram cloud persistence capacity |
| 2026-11-11 09:27 | diet-macro-assistant | Standardized workspace governance rules, codified numbered action menu, and archived redundant files | `.agents/rules/workspace.md`, `rules/MasterRules.md`, `memory/ProjectMemory.md`, `TECHNICAL_BLUEPRINT.md`, `archive/` | Eliminate doc/config sprawl and unify numbered action commands |
| 2026-11-11 09:02 | diet-macro-assistant | Created permanent `ErrorLog.md` mistake registry and continuous error logging protocol | `memory/ErrorLog.md` | Prevent recurring mistakes and track root causes |
| 2026-11-10 21:02 | diet-macro-assistant | Replaced Autonomous Rule Evolution with Mandatory Approval Rule, Backlog, and Staging lists | `.agents/rules/mandatory_approval.md`, `rules/MasterRules.md`, `memory/ProjectMemory.md` | Require explicit `"go"` or `"execute"` before modifying any files or configs |
| 2026-11-10 12:25 | diet-macro-assistant | Updated GitHub repository references to `aaron-millard/diet-macro-assistant` | `rules/MasterRules.md` | Keep deployment documentation aligned with updated GitHub username |
| 2026-11-10 09:52 | diet-macro-assistant | Replaced `"Okay"` acknowledgment protocol with `"Roger Roger"` across rules and bot | `.agents/rules/roger_roger.md`, `rules/MasterRules.md`, `bot/telegram_agent.py`, `TECHNICAL_BLUEPRINT.md` | Enforce consistent `"Roger Roger"` affirmations on idea dumps and confirmations |
| 2026-11-09 08:55 | diet-macro-assistant | Standardized service and bot naming to `diet-macro-assistant` | `bot/render.yaml`, `bot/telegram_agent.py` | Remove legacy `MacroDave` naming across Render config and prompts |
| 2026-11-09 08:03 | diet-macro-assistant | Required explicit `"Idea dump"` trigger phrase for silent note logging | `bot/telegram_agent.py`, `rules/MasterRules.md` | Prevent normal macro questions from being misrouted as silent idea dumps |
| 2026-11-09 07:56 | diet-macro-assistant | Pinned Strict Project Isolation rule and isolated `0` shorthand command | `.agents/rules/strict_project_isolation.md`, `rules/MasterRules.md`, `bot/telegram_agent.py` | Guarantee zero cross-project contamination in workspace and Telegram sync |
| 2026-11-06 21:40 | diet-macro-assistant | Added Rule 3e requiring daily super low-calorie veggie snacks | `rules/MasterRules.md` | Ensure celery, sugar snap peas, or carrots are included in daily snack plans |
| 2026-10-28 16:04 | diet-macro-assistant | Added multi-item inventory removal and persistent Telegram cloud overrides across container restarts | `bot/telegram_agent.py`, `inventory/persistent_overrides.json` | Survive ephemeral Render container restarts without losing mobile updates |
| 2026-10-27 20:23 | diet-macro-assistant | Added `"I need more context."` handler for captionless screenshots while retaining image in memory | `bot/telegram_agent.py`, `rules/MasterRules.md` | Stop misinterpreting uncaptioned screenshots as grocery receipts |
| 2026-10-27 11:25 | diet-macro-assistant | Added multi-model Gemini fallback chain (`thinkingBudget` + auto-retry) and 2s message batching | `bot/telegram_agent.py` | Eliminate 503 timeouts and combine rapid multi-screenshot uploads into one reply |
| 2026-10-24 16:20 | diet-macro-assistant | Organized categorized folder structure and added Render webhook server with 8-minute keep-alive | `bot/telegram_agent.py`, `bot/render.yaml`, `telegram_agent.py` | Enable 24/7 free-tier cloud hosting on Render without container sleep |
