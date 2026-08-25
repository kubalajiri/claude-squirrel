## What and why

<!-- The behaviour that changes, and the reason it should. -->

## Checklist

- [ ] `python3 -m unittest discover -s dev -t dev` passes
- [ ] `bash dev/smoke_install.sh` passes (run it directly, never piped)
- [ ] a test that fails without this change, and that I have watched fail
- [ ] no new dependency — the plugin is standard library only
- [ ] if the rendering changed on purpose: `python3 dev/write_golden.py` and
      `python3 dev/docs_images.py`, both re-run and committed
