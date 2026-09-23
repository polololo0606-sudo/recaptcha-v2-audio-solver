# reCAPTCHA v2 Audio Solver

An experimental **reCAPTCHA v2 checkbox/audio solver** for authorized testing. The audio solver
uses a Chrome/Chromium browser and Google speech recognition. The image solver
uses Playwright and a local vision model.

This project is not a reCAPTCHA v3 score solver, hCaptcha solver, or Cloudflare
Turnstile solver. Enterprise audio paths are allowlisted, but Enterprise solving
has not been verified.

**Start with the audio demo.** It has a runnable entry point and does not need
LM Studio. Neither implementation can guarantee acceptance by a live CAPTCHA
provider. A transcription test, a checkbox pass, and a solved audio challenge
are different results; this project reports them separately.

## Quick navigation

- [What works and what is unverified](#verification-status)
- [Install](#install)
- [Run the audio demo](#run-the-audio-demo)
- [Use the audio class in your own code](#use-the-audio-class-in-your-own-code)
- [Run the image experiment](#run-the-image-experiment)
- [How the code works](#how-the-code-works)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [Privacy and security](#privacy-and-security)
- [Guide for developers and AI coding assistants](#guide-for-developers-and-ai-coding-assistants)

## Verification status

A complete live audio solve has been demonstrated. Six new live attempts included
four widget/token successes, one transcription failure, and one rate limit.
Only one of those successes was confirmed to use audio. **Do not describe this
as a guaranteed or production-reliable solver.**

See [the dated validation record](docs/VALIDATION.md) for exact counts, environment,
limitations, and the distinction between mocked, synthetic, and live results.

| Capability | Evidence |
| --- | --- |
| Imports and dependency consistency | Checked in the recorded environment |
| Unsafe audio URL rejection and recognition timeout | Offline regression tests |
| Audio conversion and real Google transcription | Generated speech transcribed correctly |
| Live browser checkbox/token flow | Repeated live attempts; outcomes recorded separately |
| Real audio challenge accepted by the provider | See validation record; do not infer from checkbox-only success |
| Image coordinates, capture and JSON handling | Unit tests and local browser fixture |
| Real vision-model solving accuracy | Not established; local model service was unavailable |

This is experimental software, not a production service or a promise of a
particular success rate. The audio demo checks the widget and token presence;
it does not submit arbitrary website forms or perform your application's
server-side token verification.

## Choose an implementation

| | Audio | Image |
| --- | --- | --- |
| Entry point | `audio-solver/demo.py` | `standalone/captchasolver.py` |
| Core implementation | `audio-solver/RecaptchaSolver.py` | Same file as entry point |
| Browser library | DrissionPage | Playwright |
| Browser install | System Chrome or Chromium | Playwright Chromium |
| Recognition | Google speech recognition | Local OpenAI-compatible vision endpoint |
| Extra prerequisite | FFmpeg | Running vision model with image support |
| Challenge scope | v2 checkbox and audio flow | 4×4 image grids; skips/reloads 3×3 grids |
| Browser ownership | Demo creates one; class accepts caller's browser | Script creates its own browser |

## Install

Run these commands **from the repository root** after downloading or cloning it.
Do not run them inside `audio-solver/`.

```sh
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -c requirements-tested.txt
python -m pip check
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` instead.

`requirements.txt` lists the six direct dependencies.
`requirements-tested.txt` records exact versions from the tested environment,
including transitive dependencies. It is a constraints snapshot, not proof of
compatibility with every Python version or operating system. The recorded run
used Python 3.9.6 on macOS; other environments need their own validation.

Install the native prerequisites:

1. **Chrome or Chromium:** required for the audio demo. An existing system
   installation is sufficient; pass `--browser-path` if detection fails.
2. **FFmpeg:** install with your operating system's package manager, and ensure
   both commands below are available to the same shell as Python.
3. **Playwright Chromium:** install only if running the image script or its
   browser fixture.

```sh
ffmpeg -version
ffprobe -version
python -m playwright install chromium
```

The audio implementation does not record your microphone. It downloads the
challenge recording, converts it to WAV, then sends the audio to Google's speech
recognition service. No personal API key is configured by this repository.

## Run the audio demo

```sh
python audio-solver/demo.py
```

The default target is Google's reCAPTCHA demo. One invocation makes **one solve
attempt** and closes its browser. It uses a separate temporary browser profile,
not your everyday Chrome profile. It does not automatically retry failures.

If browser discovery fails, supply the executable path. For example, on macOS:

```sh
python audio-solver/demo.py \
  --browser-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

For a page you own or are authorized to test:

```sh
python audio-solver/demo.py --url "https://your-authorized-test-site.example/"
```

Optional flags:

| Flag | Meaning |
| --- | --- |
| `--url URL` | Target page; defaults to Google's demo |
| `--browser-path PATH` | Explicit Chrome/Chromium executable |
| `--headless` | No visible browser; provider behavior may differ |
| `--help` | Show arguments without opening a browser |

The script prints progress followed by a JSON result. This is an **illustrative
checkbox-only success**, not an audio-challenge success claim:

```json
{
  "status": "solved",
  "token_present": true,
  "audio_download_attempted": false,
  "audio_transcribed": false,
  "elapsed_seconds": 6.0
}
```

Interpret results precisely:

- `solved` plus `token_present: true`: widget solved and a token was found.
- `audio_transcribed: false` on a solved result: audio recognition was not proven
  by that attempt. Google can accept the checkbox without a challenge.
- `audio_transcribed: true` plus a solved result: this attempt passed through
  audio transcription and ended with a solved widget.
- `rate_limited`: provider declined the attempt. Stop and try later; this is not
  a successful solve. Do not treat rapid retries as a fix.
- `unrecognized_audio`: the recognizer could not understand the recording.
- `speech_service_error`: the external speech request failed.
- `error` or `unsolved`: inspect the troubleshooting table and configuration.

Exit status is **0 only for a solved widget with a token**, and **1 otherwise**.
The demo deliberately does not print the token, transcript, audio URL, or raw
browser exception. Token presence is not a substitute for verification by your
own backend when integrating a protected form.

## Use the audio class in your own code

The directory name contains a hyphen, so it is not a normal dotted Python package.
From a script placed in the repository root:

```python
from pathlib import Path
import sys

from DrissionPage import ChromiumOptions, ChromiumPage

sys.path.insert(0, str(Path(__file__).resolve().parent / "audio-solver"))
from RecaptchaSolver import RecaptchaSolver

options = ChromiumOptions(read_file=False).auto_port()
page = ChromiumPage(options)
try:
    page.get("https://www.google.com/recaptcha/api2/demo", timeout=30)
    solver = RecaptchaSolver(page)
    solver.solveCaptcha()
    if solver.is_solved():
        token = solver.get_token()  # Keep this in memory; do not log it.
        # Continue your authorized application's normal form flow here.
finally:
    page.quit()
```

API:

| Method | Behavior |
| --- | --- |
| `RecaptchaSolver(driver, modal=None)` | Uses an existing DrissionPage browser; optional modal narrows anchor discovery |
| `solveCaptcha()` | Clicks checkbox, switches to audio if necessary, downloads/transcribes/submits; returns `None` or raises on failure |
| `is_solved()` | Reads the anchor's checked state; returns a Boolean |
| `get_token()` | Returns a cached/discovered token string, or an empty string |

Use a new solver instance for each new page/widget attempt. The class caches
frames and a token. On pages with multiple widgets, supplying `modal` narrows
anchor selection, but challenge discovery and token lookup are page-wide.
Multi-widget association is not guaranteed; start with a page containing one
widget. The class does not own or close the browser you provide.

## Run the image experiment

First start a local vision service that implements the OpenAI-style chat
completions API **and accepts images**. The defaults in
`standalone/captchasolver.py` are:

| Setting | Default / purpose |
| --- | --- |
| `LM_STUDIO_URL` | `http://127.0.0.1:1234/v1/chat/completions` |
| `LM_MODEL` | `zai-org/glm-4.6v-flash`; must match the loaded model identifier |
| `USE_LM_STUDIO` | `True` |
| `TEST_URL` | Google's reCAPTCHA demo |
| `NUM_RUNS` | `10`; reduce to `1` for your first check |
| `MAX_SLOTS` | `5` successive grids per run |
| `MAX_3x3_RETRIES` | `8` attempts to receive a 4×4 grid |
| `HEADLESS` | `False` |
| `LM_TIMEOUT` | `120` seconds per request; requests can be retried |
| `RECHECK_THRESHOLD` | `0.55`; positive tiles below this are checked again |

Edit those source constants intentionally before running. There is no `.env`
loader and no command-line configuration interface for this script.

```sh
python -m playwright install chromium
python standalone/captchasolver.py
```

A vision-capable model must already be loaded and the API server running.
Installing Python dependencies does not install a model or start LM Studio.
Setting `USE_LM_STUDIO=False` disables model HTTP calls, **but does not disable
browser clicks**; it is not a passive screenshot-only mode.

The model sees a clean full grid followed by separate tiles. Numbered overlays
are debug artifacts and are not sent to the model. Only JSON Boolean `true`
authorizes a tile selection; strings such as `"false"` are not treated as true.
Invalid confidence values become zero so positive predictions are rechecked.

Generated files are relative to the directory where you launch the script:

```text
captcha_solver_output/
  full/            full-page screenshots
  grid/            clean grid screenshots
  overlay/         numbered debugging images
  tiles/           individual tiles for each grid
  reports/         per-run JSON reports
  solver_runs.csv  summary rows
```

Typical run statuses include `SOLVED`, `PASSED_NO_CHALLENGE`, `NO_CHECKBOX`,
`NO_4x4_AFTER_MAX_RETRIES`, `SLOT_FAILED`, `UNSOLVED`, `MAX_SLOTS`, and `CRASHED`.
Consult the status and checkbox fields, not merely whether the script exited.

## How the code works

### Audio path

```text
caller browser
  → visible approved-origin anchor
  → click checkbox
  → already solved? return token
  → visible approved-origin audio challenge
  → extract audio URL
  → validate exact HTTPS host/path and public DNS addresses
  → connect to the checked numeric address with TLS hostname verification
  → bounded audio response → temporary MP3 → FFmpeg WAV conversion
  → separate Python process → Google speech recognition
  → transcript → challenge input → Verify → check solved state
  → clean temporary files
```

Recognition has a 15-second subprocess deadline. On expiry the subprocess is
killed and reaped, temporary files are cleaned, and no answer is submitted.
This is not a 15-second deadline for the whole solve: browser waits, DNS, media
conversion, and download/header processing have separate behavior.

### Image path

```text
Playwright demo page → checkbox → obtain 4×4 grid
  → read target text → capture grid element → split into 16 tiles
  → local model describes full grid
  → local model classifies every tile
  → recheck uncertain positives → click tile centers → Verify
  → inspect checkbox/new grid → write CSV and JSON
```

Playwright element bounding boxes already use the main frame's viewport
coordinates. Do not add the iframe offset a second time. Grid element screenshots
handle frame offsets and scrolling directly. See the
[Playwright locator documentation](https://playwright.dev/python/docs/api/class-locator#locator-bounding-box).

## Tests

Offline unit and security regressions:

```sh
python -m unittest discover -s tests -v
python -m pip check
```

These tests do not contact Google or a model server. They cover URL rejection,
DNS pinning, redirects, frame origins, worker termination, cleanup, model JSON,
and image coordinates. Passing them does not prove a live CAPTCHA success rate.

A separate opt-in local browser fixture verifies real iframe coordinates and
image capture without any external page or model request:

```sh
python tests/browser_image_check.py
# Or use an installed Chrome executable:
python tests/browser_image_check.py --browser-path "/path/to/chrome"
```

For a live check, run the audio demo once and record the JSON outcome. Keep
headless and visible-browser results separate, as well as checkbox-only and
audio-challenge successes. Do not commit tokens, recordings or challenge URLs.

For deterministic tests of your own application's CAPTCHA integration, Google
provides [official test keys](https://developers.google.com/recaptcha/docs/faq).
Those can avoid real challenges; they do not validate this solver's transcription
or image-recognition ability.

## Troubleshooting

| Symptom | Check / action |
| --- | --- |
| `ModuleNotFoundError` | Activate the virtual environment; install with that environment's `python -m pip` |
| Browser cannot start | Install Chrome/Chromium and pass its executable using `--browser-path` |
| Browser address error after configuring options | `auto_port()` manages a temporary profile. Do not call `set_user_data_path()` after it; DrissionPage disables automatic port selection |
| Playwright executable missing | Run `python -m playwright install chromium` in the same environment |
| FFmpeg/FFprobe not found | Install both tools and check the shell's `PATH` |
| `NotOpenSSLWarning` | The recorded system Python emitted a LibreSSL warning. A Python build linked against supported OpenSSL avoids that environment issue; rerun tests after changing Python |
| `rate_limited`, “Try again later”, automated-query notice | Provider restriction. Stop; wait before a later manual test. Do not count it as a pass |
| `unrecognized_audio` | A valid recording could not be transcribed; this is a failed attempt, not a solved challenge |
| `speech_service_error` | Google speech recognition rejected or could not complete its request |
| `Speech recognition timed out` | External service did not finish inside the recognition deadline; the worker is stopped |
| `Untrusted reCAPTCHA URL` | A frame/audio URL is outside the allowlist. Inspect the origin/path without logging query tokens; do not disable validation |
| `Non-public reCAPTCHA address` | DNS resolved to an unsafe address. Check local DNS/network setup; do not bypass the restriction |
| Audio endpoint not HTTP 200 | Expired/invalid challenge or an unsupported response; redirects are intentionally rejected |
| Connection refused at port 1234 | Start the local vision API and load the configured model |
| Image model returns malformed JSON | Check model compatibility. Non-object JSON is rejected; no code is executed from responses |
| Green tick but application rejects form | A token must be associated with the correct widget and validated by the application backend; this demo does not do that integration |

## Privacy and security

- Audio leaves the machine for Google speech recognition. Image inference defaults
  to loopback; changing the configured endpoint changes who receives the images.
- The audio fetcher accepts only `www.google.com` and `www.recaptcha.net`, on HTTPS
  port 443, with `/recaptcha/api2/payload` or `/recaptcha/enterprise/payload`, each
  optionally ending in `/audio.mp3`. Other destinations and redirects are rejected.
- DNS answers are checked for public addresses and the connection is pinned to a
  checked address. TLS still verifies the original hostname. Proxy environment
  variables do not redirect the native audio download.
- Audio downloads are limited to 10 MiB. Temporary MP3/WAV files are removed on
  normal completion and exceptions. A forced process or machine shutdown can
  still leave temporary files.
- The demo omits tokens, transcripts and audio URLs from its result. The image
  experiment saves page screenshots and diagnostics, which can contain sensitive
  page content. Keep generated output out of Git and delete it when no longer needed.
- `.gitignore` does not remove already committed content. Review history and
  author metadata before publication. A public repository remains associated
  with its hosting account.
- The image experiment still launches Chromium with `--no-sandbox`; this is a
  remaining hardening limitation. Use only authorized, trusted test pages.

## Guide for developers and AI coding assistants

Read this section and [the validation record](docs/VALIDATION.md) before making
claims about reliability. Work from the repository root.

| Need to understand/change | Start here | Relevant checks |
| --- | --- | --- |
| One-shot audio CLI and safe result output | `audio-solver/demo.py: main`, `DemoSolver` | `--help`, controlled live attempt |
| Audio orchestration | `RecaptchaSolver.solveCaptcha`, `_solve_audio_challenge` | Audio regression suite; live flow separately |
| Trusted frame discovery | `_trusted_frame`, `_get_anchor_frame`, `_get_challenge_frame` | Imitation/hidden-frame regression |
| Audio URL and network boundary | `_trusted_url`, `_public_addresses`, `_download_audio` | Unsafe URLs, DNS, redirect, pinning and size tests |
| Recognition deadline | `_transcribe_with_timeout`, `_transcribe_audio_file` | Real sleeping-subprocess timeout test |
| Widget token retrieval | `get_token`, `_grab_token` | Review multi-widget limitations before integration |
| Image model prompts/response handling | `LOCATE_PROMPT`, `TILE_PROMPT`, `_parse_json`, `lm_classify_tile` | `tests/test_image_solver.py` |
| Image geometry/capture | `tile_page_xy`, `process_slot`, `detect_grid` | Image unit tests and local browser fixture |
| Run persistence | `append_csv`, `save_report`, `run_one` | Inspect output in an isolated directory |

Preserve these invariants:

1. Validate page-controlled audio URLs at the native download boundary, even if
   iframe validation also exists. Never re-resolve an unchecked destination or
   follow redirects around the allowlist.
2. Keep TLS hostname verification and public-address checks. Do not accept private
   addresses to make a test pass.
3. Keep recognition outside the browser process with an enforceable timeout and
   cleanup. A future timeout inside a waiting executor does not provide that bound.
4. Never print or commit token values, private recordings, browser profiles,
   credentials, absolute personal paths, or raw challenge query strings.
5. Keep synthetic/mocked tests separate from real recognition and live acceptance.
   A checkbox-only pass does not establish audio-solving accuracy.
6. Preserve the public audio API unless deliberately documenting a breaking change.
   The solver caches state; use fresh instances for new attempts.
7. Update the validation record after behavioral changes. Describe exact evidence
   and remaining limits; do not turn a small sample into a universal success claim.

There is no web server, database, hosted API, account system, or deployment step
in this repository. Both implementations run locally and rely on external browser
pages; the audio recognizer also relies on an external speech service.
