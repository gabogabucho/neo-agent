---
name: scheduler
description: "Schedule reminders and recurring tasks"
min_capability: tier-2
requires:
  connectors: [task, memory]
---
# Scheduler

You can now schedule reminders and recurring tasks for users.

## How to handle reminder requests

When a user says things like:
- "Remind me to call Maria at 3pm"
- "Every Monday, remind me to check reports"
- "In 30 minutes, tell me to take a break"

Do the following:
1. Extract WHAT to remind (the action/task)
2. Extract WHEN (specific time, relative time, or recurring pattern)
3. Use task__create to save the reminder with due_date in the metadata
4. Confirm to the user with the exact time and action

## Formatting times

- Always confirm the time back to the user in their timezone
- For relative times ("in 30 minutes"), calculate the actual time
- For recurring ("every Monday"), note the pattern in metadata

## Examples

User: "Remind me to call Maria at 3pm"
→ task__create(description="Call Maria", due_date="today 15:00", priority="high")
→ "Done! I'll remind you to call Maria at 3:00 PM."

User: "Every Friday, remind me to submit the report"
→ task__create(description="Submit weekly report", due_date="recurring:friday", priority="medium")
→ "Noted! I'll remind you every Friday to submit the report."

## Limitations

Currently reminders are saved as tasks with due dates. Active time-based
triggering (push notification at exact time) requires the heartbeat module.
For now, users can ask "What are my pending reminders?" and Neo will list them.
