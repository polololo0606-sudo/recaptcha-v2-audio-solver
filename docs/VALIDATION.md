# Validation record

Date: 2026-09-23. This record describes a small development sample, not a universal
success rate or a guarantee about future provider behavior.

## Environment

- macOS; Python 3.9.6.
- System Google Chrome; separate temporary browser profiles.
- DrissionPage 4.1.1.4; SpeechRecognition 3.17.0; pydub 0.25.1.
- FFmpeg and FFprobe available on PATH.
- Full dependency version snapshot: `requirements-tested.txt`.
- System Python emitted urllib3's LibreSSL warning. Tests still ran; this is not
  evidence of compatibility with other Python/TLS installations.

## Offline checks

| Check | Result | What it establishes |
| --- | --- | --- |
| `python -m unittest discover -s tests -v` | 11 passed | URL/DNS/redirect protections, frame filtering, timeout/cleanup and speech error handling |
| `python -m pip check` | Passed | Installed dependency metadata consistent |
| Python syntax compilation | Passed | Audio solver, demo and test files compile |
| `git diff --check` | Passed | No whitespace errors in the reviewed diff |

The recognition deadline test launches a real worker that sleeps for 30 seconds,
sets a short test deadline, checks that the call returns promptly, and verifies
that the child has exited. Separate tests verify file cleanup and that a timeout
never causes an answer submission.

## Real speech recognition

The patched conversion and subprocess-recognition path correctly transcribed a
generated spoken sentence through the actual Google recognition service. The
MP3 download was replaced with locally generated audio for that isolated test.
It validates conversion and speech recognition independently of browser selection
and genuine challenge acceptance.

## Live Google demo

Target: `https://www.google.com/recaptcha/api2/demo`.

Earlier headless attempts encountered “Try again later” after switching to audio.
Those were failures, not discarded successes. A later visible-browser batch used
three fresh browser profiles, with 90 seconds between completed attempts.

| Attempt | Mode | Widget solved + token | Audio path | Seconds |
| --- | --- | --- | --- | --- |
| Visible 1 | Headed | Yes | Audio-path instrumentation was not yet present; no audio success claim | 6.32 |
| Visible 2 | Headed | Yes | Audio-path instrumentation was not yet present; no audio success claim | 5.38 |
| Visible 3 | Headed | Yes | Confirmed checkbox-only; no audio download/transcription | 5.18 |
| Audio 1 | Headless | Yes | Real audio downloaded, transcribed, entered, and accepted by widget | 17.35 |
| Audio 2 | Headless | No | Audio downloaded; recognition subprocess failed before returning text | 12.88 |
| Audio 3 | Headless | No | Provider rate-limited before audio download | 11.54 |

**Six new live attempts: four widget/token successes, one recognition failure,
and one provider rate limit.** Only `Audio 1` establishes full live audio solving;
the visible-browser results do not establish audio accuracy. Earlier rate-limited
attempts are additional failures and are not included in this six-attempt batch.

Tests ran while result instrumentation and safe error categories were being
improved. The final offline suite was rerun after those changes; the final code
was not shown to succeed on every live attempt.

`Audio 1` exercised the complete browser/audio flow with real services. It used
neither mocked recognition nor official always-pass integration test keys.
A subsequent recognition failure is retained as `Audio 2`; its exact provider
error was not preserved by that version of the wrapper. The wrapper now retains
safe error categories (`unrecognized_audio` / `speech_service_error`) without
exposing provider responses or audio content.

Successful results mean the widget reported solved and a response token was
present. The demo form was not submitted for a separate server-side verification.
No token value, audio URL query, personal recording, or browser profile is included
in this record.

## Scope and remaining limitations

- Live acceptance depends on the provider; earlier rate limits show it is not
  reliable on every attempt. The small sample cannot establish a general rate.
- Headless and headed outcomes must not be combined into a claimed reliability
  estimate without preserving their conditions and failures.
- Multiple widgets, Enterprise-specific flows, and other sites/operating systems
  were not verified.
- The native audio destination boundary rejects files/private addresses/proxies/
  redirects. DNS and HTTP-header handling do not have a guaranteed total deadline;
  the hard subprocess deadline covers recognition specifically.
