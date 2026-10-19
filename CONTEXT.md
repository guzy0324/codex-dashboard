# Codex Dashboard

The dashboard displays Codex conversation state, completion and permission alerts, and quota.

## Language

**Account quota**:
The latest rate-limit snapshot read from the local Codex app-server.

**Conversation quota**:
Rate limits received from task hooks or read from conversation transcripts.

**Reset time**:
The timestamp at which a Codex usage window is scheduled to reset. The dashboard displays this time and the remaining countdown without issuing Codex requests to keep the window active.
