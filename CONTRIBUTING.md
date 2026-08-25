# Contributing to Squirrel

Everything about working ON Squirrel. For using it, see the [README](README.md) and the
[full documentation](plugin/README.md).

## Layout

This repo is a marketplace whose one plugin is the `plugin/` subdirectory. Nothing outside
it is installed on a user's machine.

| Path | Ships to users | What it is |
|---|---|---|
| `.claude-plugin/marketplace.json` | no | the marketplace manifest; its `source` points at `./plugin` |
| `plugin/` | **yes** | everything a user gets: `scripts/statusline.py`, `scripts/squirrel.sh`, `defaults/`, `commands/`, `hooks/`, `skills/`, `README.md`, `LICENSE` |
| `dev/` | no | tests, the golden fixture, the smoke gate, the hostile-string corpus, the benchmark |
| `LICENSE` | no | byte-identical copy of `plugin/LICENSE`, so GitHub can see it (a test enforces that) |
| `CHANGELOG.md` | no | the release record, including the security fixes behind 1.0.0 |

Splitting the tree this way is deliberate: the plugin installer copies whatever is in the
plugin directory, so before this split every colleague's machine received our entire test
surface — including `dev/hostile_strings.json`, a corpus of 36 terminal-injection payloads.
Harmless as data, but not something to put on other people's disks and then explain to
their endpoint scanner.

## Development

Everything runs from the repo root.

```
python3 -m unittest discover -s dev -t dev -v   # the suite
bash dev/smoke_install.sh; echo $?              # the install gate — run it DIRECTLY, never piped
python3 dev/bench.py                            # render-path benchmark
python3 dev/write_golden.py                     # re-baseline the golden fixture (deliberate changes only)
python3 dev/docs_images.py                      # regenerate the README's screenshots
claude plugin validate --strict .               # the manifests
```

The README's images in `docs/img/` are **generated**, never drawn: `dev/docs_images.py`
renders real bars through `render()` and converts the escape sequences to SVG. A test fails
while they are out of date, so a screenshot on the project page can never show a bar the code
would not produce.

`dev/smoke_install.sh` must be invoked directly, not through a pipe: a pipeline reports the
exit status of the last command in it, which would hide a failure.

The suite and the smoke gate isolate themselves with `CLAUDE_CONFIG_DIR`, `XDG_CACHE_HOME`
and `SQUIRREL_SHARED_DIR`, and never touch a real profile.

**Three conventions, each learned the hard way three times.** They are different mistakes
with the same symptom — a green suite over a real defect:

1. **A subsystem can be fully written and tested while never being called.** Write at least
   one test that reaches it through `render()` or `main()`, not only through the function.
   The attribution walker was correct while the render still used a rolling window; the whole
   task queue was never called from anywhere for an entire commit.
2. **A test can pass for a reason other than the one in its name.** Assert the property, not
   a symptom of it — "acks are not *blocked*" is satisfied by a version that charges them to
   the budget anyway.
3. **A guard can be untested by a fixture that cannot exercise it.** The partial-line guard
   only bites when a complete JSON object sits exactly on the seek boundary; with any other
   input the scan behaves identically with the guard deleted.

4. **A fixture is tidier than a real bar, and a tidy fixture hides interaction bugs.** The
   first three are about how a test is written; this is about what it is given. Every defect
   that reached a user here was an interaction between individually-correct things — three
   producers returning the same yellow for different reasons, one stale observer among five
   agreeing ones. A fixture for anything that composes must contain the mess.

Every one of those was found by **mutation** or by looking at a real bar, never by review:
break the code a test names, and watch *that* test fail. A test you have not watched fail is
a test you have not tested.

`dev/bench.py` measures whole process invocations, so it tracks machine load and is not a
regression verdict — see the caution at the top of that file for what to measure instead.

## CI

`.github/workflows/tests.yml` runs the suite on **Linux, macOS and Windows** on every push,
plus the install smoke gate and a check that the README's screenshots still match the
renderer. macOS is in the matrix for a specific reason: the README used to say the mac paths
"should work, untested", and a runner turns that into a fact instead of a promise.

Tests that assert POSIX behaviour (permission bits, `os.getuid()`-keyed paths, the
select()-based stdin guard) are skipped on Windows with a reason, never deleted — the
platform genuinely differs there, and the reason is the documentation.

CI earned its keep on its first run: it found `os.kill(pid, 0)` sending **Ctrl-C to its own
console group** on Windows (there, signal 0 is CTRL_C_EVENT), and `read_text()` failing on
UTF-8 sources under the cp1252 default.

## Sending a change

Nobody has write access but the maintainer, so the route is the ordinary one: **fork, branch,
pull request.** CI runs the suite on Linux, macOS and Windows for every PR, and the same
commands are all available to you locally — please run them before opening it, because a red
PR is a slower conversation than a green one.

What gets a change merged quickly:

- **A test that fails without it, and that you have watched fail.** Every defect this project
  has recorded was found by mutation or by looking at a real bar — never by review. A test you
  have not watched fail is a test you have not tested.
- **No new dependency.** The plugin is standard library only, on purpose: it runs on every
  render, on machines whose Python nobody controls.
- **Nothing that blocks the render.** Anything that could wait — a fetch, a notifier, a
  command — is spawned detached, and its failure must be invisible to the bar.
- **The four conventions above.** They are not style preferences; each one is a defect family
  that reached a user.

Deliberately rendering something differently? Re-run `python3 dev/write_golden.py` and
`python3 dev/docs_images.py` and commit both — the golden fixture and the README's screenshots
are generated, and a guard fails while either is stale.

Security problems go through [SECURITY.md](SECURITY.md) instead — privately, not as an issue.

## Licence

MIT — see [`LICENSE`](LICENSE).
