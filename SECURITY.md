# Security policy

Squirrel runs on your machine, reads your Claude Code session state, can open a loopback
listener, and — if you switch it on — runs commands from your own config. That is a real
surface, and it has already been audited once: twenty-one findings, all closed before 1.0.0
(see [CHANGELOG.md](CHANGELOG.md)).

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Use GitHub's private reporting:
**Security → Report a vulnerability** on this repository. That reaches the maintainer without
telling everyone else first.

Include what you would want if you were fixing it: what an attacker controls, what they get,
and the smallest reproduction you have. A status-line plugin has a few plausible shapes of
attack, and any of them is worth reporting:

- text from a repository, branch, prompt or command output that escapes the sanitiser and
  reaches the terminal as an escape sequence
- anything that turns a config file into code execution without `/squirrel-custom-commands on`
- anything that lets one local process reach another user's click listener, token or state
- a path traversal through a session id, segment id or task id
- credentials or tokens ending up anywhere but the profile's own 0600 files

## What Squirrel already promises

- **No network except Anthropic's usage endpoint**, and only while `/squirrel-usage` is on.
  With it off, the credentials file is never read and no token is ever in the process.
- **No prompts, no code, no file contents** leave the machine, ever. The shared store holds
  session ids, repo names, timestamps, token counts and costs — nothing else.
- **Every file it writes is 0600**, and a test walks the whole profile to prove it.
- **Commands from config never run** unless you have explicitly enabled them.
- **The click listener** binds 127.0.0.1 only, carries a 128-bit token, refuses any request
  whose `Host` is not loopback, and exits by itself once nothing has clicked it.

If you find a place where one of those is not true, that is a security bug even if nothing
else is exploitable through it.
