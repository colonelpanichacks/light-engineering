# Nexus Runner Reminders

Persistent reminders the agent should surface contextually. These are things the user has asked to be reminded about — the agent checks this file and brings them up when relevant.

## Active Reminders

- trigger: morning
  content: check Tindie orders
  created: 2026-03-19
  expires: never

## Format

Each reminder has:
- **trigger:** When to surface it (time-based, context-based, or keyword-based)
- **content:** What to say
- **created:** When the user set it
- **expires:** When it auto-removes (optional)

## Example

```
- trigger: morning
  content: Check the Tindie orders dashboard
  created: 2026-03-19
  expires: never
```
