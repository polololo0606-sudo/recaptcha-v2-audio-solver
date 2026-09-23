"""Run one authorized audio-CAPTCHA test without logging the response token."""
import argparse
import json
import time

import speech_recognition as sr
from DrissionPage import ChromiumOptions, ChromiumPage
from RecaptchaSolver import RecaptchaSolver


class DemoSolver(RecaptchaSolver):
    audio_attempted = False
    audio_transcribed = False

    def _download_and_transcribe(self, audio_url):
        self.audio_attempted = True
        transcript = super()._download_and_transcribe(audio_url)
        self.audio_transcribed = True
        return transcript


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='https://www.google.com/recaptcha/api2/demo',
                        help='Demo URL or a page you are authorized to test')
    parser.add_argument('--browser-path', help='Chrome/Chromium executable, if not detected')
    parser.add_argument('--headless', action='store_true', help='Run without a visible browser')
    args = parser.parse_args()
    options = ChromiumOptions(read_file=False).auto_port()
    if args.browser_path:
        options.set_browser_path(args.browser_path)
    if args.headless:
        options.headless()

    page = None
    solver = None
    started = time.monotonic()
    result = {'status': 'error', 'token_present': False}
    try:
        page = ChromiumPage(options)
        page.get(args.url, timeout=30)
        solver = DemoSolver(page)
        solver.solveCaptcha()
        solved = solver.is_solved()
        result = {'status': 'solved' if solved else 'unsolved',
                  'token_present': bool(solver.get_token()) if solved else False}
    except Exception as exc:
        message = str(exc)
        limited = 'rate limit' in message.lower() or 'bot detection' in message.lower()
        result = {'status': 'rate_limited' if limited else 'error',
                  'error_type': type(exc).__name__, 'token_present': False}
        # Browser exceptions can contain URL parameters and local paths.
        # Keep the output safe to share; no tokens, audio URLs or raw traceback.
        if limited:
            result['message'] = 'Provider refused this attempt. Stop and try later.'
        elif isinstance(exc, sr.UnknownValueError):
            result['status'] = 'unrecognized_audio'
            result['message'] = 'Speech recognition could not understand the recording.'
        elif isinstance(exc, sr.RequestError):
            result['status'] = 'speech_service_error'
            result['message'] = 'The speech service request failed.'
        elif isinstance(exc, TimeoutError):
            result['message'] = 'An operation timed out.'
        else:
            result['message'] = 'Solve did not complete; see README troubleshooting.'
    finally:
        if page is not None:
            page.quit()
    result['audio_download_attempted'] = bool(solver and solver.audio_attempted)
    result['audio_transcribed'] = bool(solver and solver.audio_transcribed)
    result['elapsed_seconds'] = round(time.monotonic() - started, 2)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'solved' and result['token_present'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
