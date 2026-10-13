## What & why

<!-- One or two sentences: the problem and the change. Link the issue if any. -->

## How this was tested

<!-- Be specific — commands, browsers, camera model/firmware. -->

- [ ] Mock mode (`HIK_MOCK=1`)
- [ ] Real camera — model / firmware: ``
- [ ] Browser(s) checked: ``
- [ ] Streaming verified (`curl` playlist + `ffprobe` shows H.264, no stray ffmpeg)

## Required gate checklist

- [ ] I installed the pre-commit hooks (`venv/bin/pre-commit install`)
- [ ] I ran the full gates (`venv/bin/pre-commit run -a`) and they pass
- [ ] Commit subjects follow Conventional Commits
- [ ] No credentials or secrets added (`config.env` stays untracked)

## Architecture checklist (when the change touches these areas)

- [ ] ffmpeg sessions keep the watchdog wrapper, session registry, group-kill,
      and stop-when-no-viewer paths
- [ ] New camera calls use the error model so the UI keeps distinguishing
      unreachable / service-down / no-data
- [ ] Codec handling stays automatic (H.264 copied, everything else transcoded)
- [ ] Tests added or updated for behaviour changes
- [ ] `README.md` / `AGENTS.md` updated if configuration or behaviour changed

## Screenshots (UI changes)

<!-- Before/after, ideally mobile and desktop. -->
