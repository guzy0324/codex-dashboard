# Codex Dashboard

This context defines the terms used for Codex quota keepalive requests and their status in the local dashboard.

## Language

**Five-hour reset time**:
The timestamp at which Codex's five-hour usage window is scheduled to reset.

**Reset check**:
An evaluation of the five-hour reset time against the current time, recording whether the reset time has passed.

**Codex quota keepalive**:
A service that reads the five-hour quota reset time and issues a Codex request when that time has passed, keeping the quota active.

**Reset keepalive**:
A Codex request issued after the tracked five-hour reset time has passed, followed by a fresh reset check.

**Check execution result**:
Whether reading the rate limits and evaluating the five-hour reset time completed successfully. A successful check can report either that the reset time has passed or that it is still in the future.

**Keepalive execution result**:
Whether the reset keepalive request and its follow-up reset check both succeeded.

**Keepalive status**:
Whether the service and its rate-limit poller are running, plus the latest check and keepalive results observed by the local dashboard.
