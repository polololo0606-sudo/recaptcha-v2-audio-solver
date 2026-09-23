# RecaptchaSolver.py — Audio-based reCAPTCHA v2 solver using DrissionPage
#
# Supports pages with multiple reCAPTCHA widgets.
# Uses the modal context to find the RIGHT captcha, then:
#   1. Click the reCAPTCHA checkbox (via JS inside the iframe)
#   2. Switch to audio challenge
#   3. Download the audio MP3
#   4. Convert to WAV with pydub
#   5. Transcribe with Google Speech Recognition
#   6. Type the answer and verify
#
# Works with DrissionPage's ChromiumPage + ChromiumFrame.

import os
import time
import tempfile
import http.client
import ipaddress
import json
import socket
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import speech_recognition as sr
from pydub import AudioSegment


# Exact origins and paths keep page-controlled URLs out of native resources.
_RECAPTCHA_HOSTS = frozenset({"www.google.com", "www.recaptcha.net"})
_AUDIO_PATHS = ("/recaptcha/api2/payload", "/recaptcha/api2/payload/audio.mp3",
                "/recaptcha/enterprise/payload", "/recaptcha/enterprise/payload/audio.mp3")
_DOWNLOAD_TIMEOUT = 15
_RECOGNITION_TIMEOUT = 15
_MAX_AUDIO_BYTES = 10 * 1024 * 1024


def _trusted_url(value, paths):
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid reCAPTCHA URL")
    if "\\" in value:
        raise ValueError("Invalid reCAPTCHA URL")
    url = urlsplit(value)
    if (url.scheme != "https" or url.hostname not in _RECAPTCHA_HOSTS
            or url.username is not None or url.password is not None
            or url.port not in (None, 443) or url.fragment or url.path not in paths):
        raise ValueError("Untrusted reCAPTCHA URL")
    return url


def _public_addresses(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("No address for reCAPTCHA host")
    for family, socktype, proto, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified
                or getattr(ip, "ipv4_mapped", None) is not None
                or getattr(ip, "sixtofour", None) is not None
                or getattr(ip, "teredo", None) is not None):
            raise ValueError("Non-public reCAPTCHA address")
    return addresses


def _download_audio(value):
    url = _trusted_url(value, _AUDIO_PATHS)
    addresses = _public_addresses(url.hostname)
    context = ssl.create_default_context()
    deadline = time.monotonic() + _DOWNLOAD_TIMEOUT
    connection = http.client.HTTPSConnection(url.hostname, timeout=_DOWNLOAD_TIMEOUT,
                                             context=context)
    # Connect directly to a validated numeric address. No proxies or second DNS lookup.
    for family, socktype, proto, _, address in addresses:
        raw = socket.socket(family, socktype, proto)
        try:
            raw.settimeout(max(0.001, deadline - time.monotonic()))
            raw.connect(address)
            connection.sock = context.wrap_socket(raw, server_hostname=url.hostname)
            break
        except OSError:
            raw.close()
            if time.monotonic() >= deadline:
                raise TimeoutError("Audio download timed out")
    else:
        raise ConnectionError("Could not connect to reCAPTCHA audio host")
    try:
        path = url.path + ("?" + url.query if url.query else "")
        connection.request("GET", path, headers={"User-Agent": "Mozilla/5.0"})
        response = connection.getresponse()
        if response.status != 200:
            # Includes redirects: never fetch a second, page-selected destination.
            raise ValueError("Audio endpoint did not return HTTP 200")
        length = response.getheader("Content-Length")
        if length is not None and int(length) > _MAX_AUDIO_BYTES:
            raise ValueError("Audio response too large")
        data = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Audio download timed out")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, _MAX_AUDIO_BYTES + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > _MAX_AUDIO_BYTES:
                raise ValueError("Audio response too large")
    finally:
        connection.close()


def _transcribe_with_timeout(wav_path):
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--transcribe-audio", str(wav_path)],
            capture_output=True, text=True, timeout=_RECOGNITION_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Speech recognition timed out") from exc
    if result.returncode:
        try:
            error = json.loads(result.stdout).get("error")
        except (ValueError, AttributeError):
            error = None
        if error == "UnknownValueError":
            raise sr.UnknownValueError()
        if error == "RequestError":
            raise sr.RequestError("Speech service request failed")
        raise RuntimeError("Speech recognition failed")
    return json.loads(result.stdout).strip().lower()


def _transcribe_audio_file(wav_path):
    recognizer = sr.Recognizer()
    recognizer.operation_timeout = _RECOGNITION_TIMEOUT
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
    return recognizer.recognize_google(audio_data)


class RecaptchaSolver:
    """
    Solve reCAPTCHA v2 (checkbox + audio challenge) on a DrissionPage browser.

    Usage:
        solver = RecaptchaSolver(driver, modal=modal_element)
        solver.solveCaptcha()
    """

    def __init__(self, driver, modal=None):
        """
        driver : DrissionPage ChromiumPage instance
        modal  : (optional) parent element — used to find the CORRECT captcha
                 on pages with multiple reCAPTCHA widgets
        """
        self.driver = driver
        self.modal = modal
        self._token = ""
        self._anchor_frame = None   # cached anchor iframe (ChromiumFrame)
        self._challenge_frame = None  # cached bframe (ChromiumFrame)

    # ==================================================================
    # Public API
    # ==================================================================

    def solveCaptcha(self):
        """Full solve flow: checkbox → audio challenge → transcribe → submit."""
        # Step 1: Click the reCAPTCHA checkbox
        self._click_checkbox()
        time.sleep(2.5)

        # Already solved after clicking? (low-risk score → no challenge)
        if self.is_solved():
            self._grab_token()
            return

        # Step 2: Switch to audio challenge
        self._click_audio_button()
        time.sleep(2)

        # Step 3-6: Download audio, transcribe, type answer, verify
        self._solve_audio_challenge()

    def is_solved(self) -> bool:
        """Return True if the reCAPTCHA checkbox shows a green tick."""
        try:
            anchor = self._get_anchor_frame()
            if not anchor:
                return False
            # Check aria-checked on the anchor span
            el = anchor.ele("css:#recaptcha-anchor", timeout=1)
            if el and el.attr("aria-checked") == "true":
                return True
            # Check for the checked class
            el = anchor.ele("css:.recaptcha-checkbox-checked", timeout=1)
            if el:
                return True
        except Exception:
            pass
        return False

    def get_token(self) -> str:
        """Return the solved captcha token (if available)."""
        if self._token:
            return self._token
        self._grab_token()
        return self._token

    # ==================================================================
    # Internal: find the CORRECT iframes
    # ==================================================================

    @staticmethod
    def _trusted_frame(frame, kind):
        try:
            url = _trusted_url(frame.url, (f"/recaptcha/api2/{kind}",
                                           f"/recaptcha/enterprise/{kind}"))
            return bool(url and frame.states.is_displayed)
        except (ValueError, AttributeError):
            return False

    def _get_anchor_frame(self):
        """Return a visible, genuine reCAPTCHA anchor in the requested context."""
        if self._anchor_frame and self._trusted_frame(self._anchor_frame, "anchor"):
            return self._anchor_frame
        self._anchor_frame = None
        ctx = self.modal if self.modal else self.driver
        for frame in ctx.eles("tag:iframe"):
            if self._trusted_frame(frame, "anchor"):
                self._anchor_frame = frame
                return frame
        return None

    def _get_challenge_frame(self):
        """Return a visible challenge frame from an approved reCAPTCHA origin."""
        if self._challenge_frame and self._trusted_frame(self._challenge_frame, "bframe"):
            return self._challenge_frame
        self._challenge_frame = None
        for frame in self.driver.eles("tag:iframe"):
            if self._trusted_frame(frame, "bframe"):
                self._challenge_frame = frame
                return frame
        return None

    # ==================================================================
    # Internal: click checkbox via JS (handles "no location" issue)
    # ==================================================================

    def _click_checkbox(self):
        """
        Click the reCAPTCHA checkbox.
        Uses JS to programmatically click the checkbox inside the correct
        anchor iframe — avoids DrissionPage's "no location or size" error.
        """
        anchor = self._get_anchor_frame()
        if not anchor:
            raise Exception("Could not find reCAPTCHA anchor iframe")

        # Method 1: Try clicking the #recaptcha-anchor element inside the frame
        try:
            cb = anchor.ele("css:#recaptcha-anchor", timeout=3)
            if cb:
                try:
                    cb.click()
                    print("    ✓ Clicked reCAPTCHA checkbox")
                    return
                except Exception:
                    # "no location" — try JS click inside the frame
                    pass
        except Exception:
            pass

        # Method 2: Run JS inside the anchor frame to click the checkbox
        try:
            anchor.run_js("document.getElementById('recaptcha-anchor').click();")
            print("    ✓ Clicked reCAPTCHA checkbox (frame JS)")
            return
        except Exception:
            pass

        # Method 3: Click the iframe element itself from the parent page
        try:
            anchor.click()
            print("    ✓ Clicked reCAPTCHA checkbox (iframe click)")
            return
        except Exception:
            pass

        # Method 4: Use top-level JS to find and click the visible anchor iframe
        try:
            self.driver.run_js("""
                var frames = document.querySelectorAll('iframe[src*="recaptcha"][src*="anchor"]');
                for (var i = frames.length - 1; i >= 0; i--) {
                    var rect = frames[i].getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        frames[i].contentWindow.postMessage({type: 'click'}, '*');
                        // Direct click on iframe triggers the captcha
                        var evt = new MouseEvent('click', {
                            bubbles: true, cancelable: true,
                            clientX: rect.left + rect.width/2,
                            clientY: rect.top + rect.height/2
                        });
                        frames[i].dispatchEvent(evt);
                        return true;
                    }
                }
                return false;
            """)
            print("    ✓ Clicked reCAPTCHA checkbox (dispatch event)")
            return
        except Exception as e:
            pass

        raise Exception("Failed to click reCAPTCHA checkbox — all methods exhausted")

    # ==================================================================
    # Internal: switch to audio
    # ==================================================================

    def _click_audio_button(self):
        """Click the 'Get an audio challenge' button in the challenge popup."""
        # Wait for the challenge iframe to become visible
        challenge = None
        for _ in range(5):
            self._challenge_frame = None  # force re-search
            challenge = self._get_challenge_frame()
            if challenge:
                break
            time.sleep(1)

        if not challenge:
            if self.is_solved():
                return
            raise Exception("Could not find reCAPTCHA challenge iframe")

        # Find and click the audio button
        try:
            audio_btn = challenge.ele("css:#recaptcha-audio-button", timeout=4)
            if audio_btn:
                try:
                    audio_btn.click()
                    print("    ✓ Switched to audio challenge")
                    return
                except Exception:
                    pass
                # JS fallback inside challenge frame
                try:
                    challenge.run_js("document.getElementById('recaptcha-audio-button').click();")
                    print("    ✓ Switched to audio challenge (frame JS)")
                    return
                except Exception:
                    pass
        except Exception:
            pass

        # Check for rate limit / bot detection messages
        try:
            body = challenge.ele("tag:body", timeout=1)
            if body:
                text = (body.text or "").lower()
                if "try again later" in text or "automated queries" in text:
                    raise Exception("Rate limited by reCAPTCHA — try again later")
        except Exception as e:
            if "rate limit" in str(e).lower():
                raise

        raise Exception("Could not find audio challenge button")

    # ==================================================================
    # Internal: solve audio challenge
    # ==================================================================

    def _solve_audio_challenge(self):
        """Download audio, transcribe, type answer, and verify."""
        self._challenge_frame = None  # re-find after switching to audio
        challenge = self._get_challenge_frame()
        if not challenge:
            raise Exception("Challenge iframe disappeared after audio switch")

        # Wait for audio to load
        time.sleep(2)

        # Check for rate-limit errors
        self._check_for_errors(challenge)

        # Get the audio download link
        audio_url = self._get_audio_url(challenge)
        if not audio_url:
            raise Exception("Could not find audio download URL")

        print(f"    📥 Downloading audio...")

        # Download, convert, transcribe
        transcript = self._download_and_transcribe(audio_url)
        if not transcript:
            raise Exception("Speech recognition returned empty result")

        print("    Audio transcribed")

        # Type the answer
        self._type_answer(challenge, transcript)
        time.sleep(0.5)

        # Click verify
        self._click_verify(challenge)
        time.sleep(3)

        # Check if solved
        if self.is_solved():
            self._grab_token()
            return

        self._check_for_errors(challenge)

        time.sleep(2)
        if self.is_solved():
            self._grab_token()
            return

        raise Exception("Audio challenge submitted but captcha not solved")

    def _get_audio_url(self, challenge_frame) -> str:
        """Extract the audio MP3 URL from the challenge iframe."""
        # Try multiple selectors
        selectors = [
            "css:.rc-audiochallenge-tdownload-link",
            "css:a[href*='.mp3']",
            "tag:a@text():Download",
        ]
        for sel in selectors:
            try:
                el = challenge_frame.ele(sel, timeout=2)
                if el:
                    href = el.attr("href") or ""
                    if href:
                        return href
            except Exception:
                continue

        # Check <audio> element with <source>
        try:
            audio_el = challenge_frame.ele("tag:audio", timeout=2)
            if audio_el:
                src_el = audio_el.ele("tag:source", timeout=1)
                if src_el:
                    url = src_el.attr("src") or ""
                    if url:
                        return url
        except Exception:
            pass

        # JS fallback inside frame
        try:
            url = challenge_frame.run_js("""
                var link = document.querySelector('.rc-audiochallenge-tdownload-link');
                if (link) return link.href;
                var audio = document.querySelector('#audio-source');
                if (audio) return audio.src;
                var source = document.querySelector('audio source');
                if (source) return source.src;
                return '';
            """)
            if url:
                return str(url)
        except Exception:
            pass

        return ""

    def _download_and_transcribe(self, audio_url: str) -> str:
        """Download MP3, convert to WAV, run Google Speech Recognition."""
        tmp_dir = tempfile.mkdtemp(prefix="captcha_audio_")
        mp3_path = os.path.join(tmp_dir, "challenge.mp3")
        wav_path = os.path.join(tmp_dir, "challenge.wav")

        try:
            data = _download_audio(audio_url)
            with open(mp3_path, "wb") as f:
                f.write(data)
            print(f"    Downloaded {len(data)} bytes")

            audio = AudioSegment.from_mp3(mp3_path)
            audio.export(wav_path, format="wav")
            return _transcribe_with_timeout(wav_path)

        finally:
            for p in (mp3_path, wav_path):
                try:
                    os.remove(p)
                except Exception:
                    pass
            try:
                os.rmdir(tmp_dir)
            except Exception:
                pass

    def _type_answer(self, challenge_frame, answer: str):
        """Type the transcribed answer into the response input."""
        # Try native element interaction first
        try:
            el = challenge_frame.ele("css:#audio-response", timeout=2)
            if el:
                try:
                    el.clear()
                    el.input(answer)
                    print("    ✓ Typed answer into response field")
                    return
                except Exception:
                    pass
        except Exception:
            pass

        # JS fallback inside challenge frame
        try:
            answer_js = answer.replace("\\", "\\\\").replace("'", "\\'")
            challenge_frame.run_js(f"""
                var el = document.getElementById('audio-response');
                if (el) {{
                    el.value = '{answer_js}';
                    el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
            """)
            print("    ✓ Typed answer (JS fallback)")
            return
        except Exception:
            pass

        raise Exception("Could not find audio response input field")

    def _click_verify(self, challenge_frame):
        """Click the Verify button in the challenge."""
        try:
            el = challenge_frame.ele("css:#recaptcha-verify-button", timeout=2)
            if el:
                try:
                    el.click()
                    print("    ✓ Clicked Verify button")
                    return
                except Exception:
                    pass
                # JS fallback
                try:
                    challenge_frame.run_js("document.getElementById('recaptcha-verify-button').click();")
                    print("    ✓ Clicked Verify (JS)")
                    return
                except Exception:
                    pass
        except Exception:
            pass

        raise Exception("Could not find Verify button")

    def _check_for_errors(self, challenge_frame):
        """Check for rate limit or error messages in the challenge."""
        try:
            error_sels = [
                "css:.rc-audiochallenge-error-message",
                "css:.rc-doscaptcha-header-text",
            ]
            for sel in error_sels:
                try:
                    el = challenge_frame.ele(sel, timeout=1)
                    if el:
                        text = (el.text or "").lower()
                        if "try again later" in text or "automated queries" in text:
                            raise Exception(f"Rate limited by reCAPTCHA: {text}")
                        if "multiple correct" in text:
                            raise Exception("reCAPTCHA bot detection triggered")
                except Exception as e:
                    if "rate limit" in str(e).lower() or "bot" in str(e).lower():
                        raise
        except Exception as e:
            if "rate limit" in str(e).lower() or "bot" in str(e).lower():
                raise

    # ==================================================================
    # Internal: grab token
    # ==================================================================

    def _grab_token(self):
        """Read the g-recaptcha-response token from the page."""
        try:
            val = self.driver.run_js("""
                var tas = document.querySelectorAll('textarea[name="g-recaptcha-response"]');
                for (var i = 0; i < tas.length; i++) {
                    if (tas[i].value && tas[i].value.length > 20) return tas[i].value;
                }
                return '';
            """)
            if val:
                self._token = str(val)
        except Exception:
            pass

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--transcribe-audio":
        raise SystemExit("Import RecaptchaSolver with a configured browser driver.")
    try:
        print(json.dumps(_transcribe_audio_file(sys.argv[2])))
    except (sr.UnknownValueError, sr.RequestError) as exc:
        # Keep provider details and audio content out of subprocess diagnostics.
        print(json.dumps({"error": type(exc).__name__}))
        raise SystemExit(1)
