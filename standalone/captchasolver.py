"""
=============================================================================
  reCAPTCHA Auto-Solver  v4.1
  Strategy:
    1. Navigate + click checkbox
    2. If a 3x3 grid appears, reload the page and try again (up to
       MAX_3x3_RETRIES times) until a 4x4 grid is served.
    3. Read prompt text DIRECTLY from Playwright (no OCR).
       Parse the target keyword from it.
    4. Capture the grid, draw numbered overlay.
    5. Send the clean full-grid image for a scene description, then
       classify each tile with a separate JSON response.
    6. Re-check low-confidence positive tiles individually.
    7. Click every confirmed tile (>=1 s apart + random jitter).
    8. Click Verify.
    9. Detect success / new challenge / failure.  Repeat up to MAX_SLOTS.
   10. Write CSV log + per-run JSON report.
=============================================================================
"""

from __future__ import annotations

import base64
import csv
import io
import json
import math
import os
import random
import re
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFont
from playwright.sync_api import Frame, Locator, Page, sync_playwright

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG  —  change these only
# ─────────────────────────────────────────────────────────────────────────────

# Primary URL: Google's own reCAPTCHA demo — reliable, not bot-blocked, always
# serves image challenges when browser fingerprint looks automated.
# Fallback options (uncomment one if primary stops working):
#   TEST_URL = "https://2captcha.com/demo/recaptcha-v2"
#   TEST_URL = "https://recaptcha-demo.appspot.com/recaptcha-v2-checkbox-explicit.php"
TEST_URL   = "https://www.google.com/recaptcha/api2/demo"
NUM_RUNS   = 10
MAX_SLOTS  = 5           # max successive grids per run

# LM Studio
USE_LM_STUDIO     = True
LM_STUDIO_URL     = "http://127.0.0.1:1234/v1/chat/completions"  # base: http://127.0.0.1:1234
LM_MODEL          = "zai-org/glm-4.6v-flash"
LM_TEMPERATURE    = 0.0
LM_MAX_TOKENS     = 600
LM_TIMEOUT        = 120

# Confidence threshold below which we do a per-tile re-check
RECHECK_THRESHOLD = 0.55

# Timing (seconds)
INITIAL_WAIT        = 5.0
AFTER_CHECKBOX_WAIT = 3.5
AFTER_VERIFY_WAIT   = 3.8
CLICK_MIN           = 1.05
CLICK_MAX           = 2.10
CLICK_JITTER        = 0.18   # random pre-click pause
CHANGE_POLL         = 0.80
CHANGE_TIMEOUT      = 22.0
BETWEEN_RUNS        = (4.0, 8.0)

# If a 3x3 grid appears, reload the page and try again (up to this many times)
MAX_3x3_RETRIES     = 8
RETRY_WAIT          = 3.0   # seconds to wait after reload before checking again

# Browser
HEADLESS   = False
SLOW_MO_MS = 0

# Output directories
OUTPUT_DIR  = Path("captcha_solver_output")
FULL_DIR    = OUTPUT_DIR / "full"
GRID_DIR    = OUTPUT_DIR / "grid"
OVERLAY_DIR = OUTPUT_DIR / "overlay"
TILES_DIR   = OUTPUT_DIR / "tiles"
REPORTS_DIR = OUTPUT_DIR / "reports"
CSV_LOG     = OUTPUT_DIR / "solver_runs.csv"

CSV_FIELDS = [
    "run_id", "timestamp", "status", "notes",
    "prompt_text", "target_label", "grid_size",
    "anchor_index", "slots_processed",
    "tiles_positive", "tiles_clicked", "verify_outcome",
    "final_checkbox_state",
    "image_1", "image_2", "image_3", "image_4", "image_5",
    "overlay_1", "overlay_2", "overlay_3", "overlay_4", "overlay_5",
    "lm_positives_1", "lm_positives_2", "lm_positives_3",
    "lm_positives_4", "lm_positives_5",
    "lm_json_1", "lm_json_2", "lm_json_3", "lm_json_4", "lm_json_5",
]


# ─────────────────────────────────────────────────────────────────────────────
#  SETUP
# ─────────────────────────────────────────────────────────────────────────────

def ensure_dirs() -> None:
    for d in [OUTPUT_DIR, FULL_DIR, GRID_DIR, OVERLAY_DIR, TILES_DIR, REPORTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

def make_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{random.randint(100, 999)}"

def blank_row(run_id: str) -> dict:
    row = {f: "" for f in CSV_FIELDS}
    row.update(run_id=run_id,
               timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               slots_processed=0, tiles_positive=0, tiles_clicked=0)
    return row

def append_csv(row: dict) -> None:
    exists = CSV_LOG.exists()
    with CSV_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not exists:
            w.writeheader()
        for k in CSV_FIELDS:
            row.setdefault(k, "")
        w.writerow({k: row[k] for k in CSV_FIELDS})

def save_report(run_id: str, data: dict) -> None:
    p = REPORTS_DIR / f"{run_id}.json"
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
#  TARGET KEYWORD  —  parsed directly from Playwright text string, no OCR
# ─────────────────────────────────────────────────────────────────────────────

_STRIP_PHRASES = [
    "select all squares with",
    "select all images with",
    "select all images that contain",
    "click verify once there are none left",
    "if there are none, click skip",
    "once there are none left click skip",
    "click skip",
]

def extract_target(prompt_text: str) -> str:
    """
    Parse the target object directly from the Playwright prompt string.
    Example:
      'Select all squares with\\nmotorcycles\\nIf there are none, click skip'
      -> 'motorcycles'
    """
    text = re.sub(r"\s+", " ", (prompt_text or "")).lower().strip()

    for phrase in _STRIP_PHRASES:
        text = text.replace(phrase, "")

    text = re.sub(r"[^a-z0-9 /\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Strip leading article ("a bus" → "bus", "an umbrella" → "umbrella")
    text = re.sub(r"^(a |an )", "", text).strip()

    return text if text else "unknown"


# ─────────────────────────────────────────────────────────────────────────────
#  IMAGE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def path_to_b64(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode("utf-8")

def images_differ(a: Optional[bytes], b: Optional[bytes]) -> bool:
    if a is None or b is None:
        return True
    try:
        ia = Image.open(io.BytesIO(a)).convert("RGB")
        ib = Image.open(io.BytesIO(b)).convert("RGB")
        if ia.size != ib.size:
            return True
        return ImageChops.difference(ia, ib).getbbox() is not None
    except Exception:
        return True

def clamp(box: dict, W: int, H: int) -> tuple:
    x1 = max(0, int(box["x"]))
    y1 = max(0, int(box["y"]))
    x2 = min(W, int(box["x"] + box["width"]))
    y2 = min(H, int(box["y"] + box["height"]))
    return x1, y1, x2, y2

def crop_save(src: Path, box: tuple, dst: Path) -> Path:
    Image.open(src).convert("RGB").crop(box).save(dst)
    return dst

def make_overlay(grid_path: Path, grid_size: int, out: Path) -> Path:
    """Draw white grid lines and yellow cell-number labels onto the grid image."""
    img = Image.open(grid_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    W, H = img.size
    cw, ch = W / grid_size, H / grid_size

    # grid lines
    for i in range(1, grid_size):
        draw.line([(int(i * cw), 0), (int(i * cw), H)], fill=(255, 255, 255), width=3)
        draw.line([(0, int(i * ch)), (W, int(i * ch))], fill=(255, 255, 255), width=3)

    # cell labels
    font = _best_font(30 if grid_size == 4 else 36)
    n = 1
    for r in range(grid_size):
        for c in range(grid_size):
            lx = int(c * cw)
            ty = int(r * ch)
            draw.rectangle([(lx + 4, ty + 4), (lx + 54, ty + 42)], fill=(0, 0, 0))
            draw.text((lx + 10, ty + 7), str(n), fill=(255, 255, 0), font=font)
            n += 1

    img.save(out)
    return out

def _best_font(size: int):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for fp in candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()

def split_tiles(grid_path: Path, grid_size: int, slot_dir: Path
                ) -> list[tuple[int, int, int, Path]]:
    """Return [(tile_num, row, col, path), ...] for every cell."""
    slot_dir.mkdir(parents=True, exist_ok=True)
    img = Image.open(grid_path).convert("RGB")
    W, H = img.size
    cw, ch = W / grid_size, H / grid_size
    tiles = []
    for r in range(grid_size):
        for c in range(grid_size):
            box = (
                int(round(c * cw)), int(round(r * ch)),
                int(round((c + 1) * cw)), int(round((r + 1) * ch)),
            )
            n = r * grid_size + c + 1
            tp = slot_dir / f"tile_{n:02d}.png"
            img.crop(box).save(tp)
            tiles.append((n, r, c, tp))
    return tiles


# ─────────────────────────────────────────────────────────────────────────────
#  LM STUDIO  —  primary (full-grid) and secondary (per-tile)
# ─────────────────────────────────────────────────────────────────────────────

def _lm(messages: list, max_tokens: int = LM_MAX_TOKENS) -> str:
    """
    POST to LM Studio and return the assistant text.
    Handles multiple response shapes (OpenAI-style, some GLM variants differ).
    Retries once on transient failure.
    """
    if not USE_LM_STUDIO:
        return ""

    for attempt in range(1, 3):          # up to 2 tries
        try:
            payload = {
                "model": LM_MODEL,
                "messages": messages,
                "temperature": LM_TEMPERATURE,
                "max_tokens": max_tokens,
            }
            r = requests.post(LM_STUDIO_URL, json=payload, timeout=LM_TIMEOUT)
            r.raise_for_status()
            data = r.json()

            # ── Standard OpenAI / LM Studio shape ────────────────────────────
            if "choices" in data:
                choice = data["choices"][0]
                # Most models: choice["message"]["content"]
                if isinstance(choice.get("message"), dict):
                    return choice["message"].get("content", "").strip()
                # Some models put text directly in choice["text"]
                if "text" in choice:
                    return choice["text"].strip()

            # ── GLM / non-standard shape fallbacks ────────────────────────────
            # Some GLM builds return {"response": "..."} or {"content": "..."}
            for key in ("response", "content", "text", "output", "result"):
                if key in data and isinstance(data[key], str):
                    return data[key].strip()

            # Last resort: dump the whole response so we can see what the
            # model actually returned and adapt.
            raw_dump = json.dumps(data)[:500]
            print(f"      [LM WARNING] Unrecognised response shape: {raw_dump}")
            return f"LM_UNKNOWN_SHAPE:{raw_dump}"

        except requests.exceptions.Timeout:
            print(f"      [LM] Timeout on attempt {attempt} — retrying ...")
            time.sleep(2)
        except Exception as e:
            # Print the raw response body when available so we can debug
            raw_body = ""
            try:
                raw_body = r.text[:300]
            except Exception:
                pass
            print(f"      [LM ERROR] attempt {attempt}: {e}")
            if raw_body:
                print(f"      [LM RAW BODY] {raw_body}")
            if attempt == 2:
                return f"LM_ERROR:{e}"
            time.sleep(1)

    return "LM_ERROR:max_retries"

def _image_msg(img_path: Path, text: str) -> list:
    """
    Build a vision message that works with OpenAI-compatible endpoints.
    GLM-4V and similar models accept the standard image_url format.
    Image is resized to max 1024px on the long side before encoding —
    smaller payload = faster response and fewer timeout errors.
    """
    # Resize to keep payload manageable
    img = Image.open(img_path).convert("RGB")
    max_side = 1024
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)   # JPEG is smaller than PNG
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
    }]

def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            value = json.loads(m.group(0))
            return value if isinstance(value, dict) else None
        except Exception:
            pass
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  LM PIPELINE  —  Step 1: full clean grid → Step 2: each tile individually
#
#  The key insight: never draw grid lines over the image.  They destroy content.
#  Instead, send the CLEAN grid for spatial understanding, then send each tile
#  by itself at full resolution for a precise binary yes/no.
# ─────────────────────────────────────────────────────────────────────────────

# ── Step 1 prompt — CLEAN grid, no labels, no lines ──────────────────────────
LOCATE_PROMPT = """You are solving a reCAPTCHA challenge. Look at this photo grid carefully.

I need to find: {target}

Scan the ENTIRE image top-to-bottom, left-to-right.

Answer in plain English:
1. YES or NO — is a {target} visible anywhere in this image?
2. If YES: where exactly? (e.g. "top-left corner", "centre of the image",
   "right side spanning two rows", "multiple locations across the top row")
3. How many {target} objects do you count?

Be specific and concrete. Short answers only. No JSON."""


# ── Step 2 prompt — single tile, full resolution ──────────────────────────────
TILE_PROMPT = """Look at this photo carefully.

I need to know: does this photo contain a {target}?

Context (from analysing the full grid): {context}

Answer with JSON only — no other text:
{{"yes": false, "confidence": 0.9, "what_i_see": "describe what is in this photo in one sentence"}}

IMPORTANT:
- "yes" = true only if a {target} is actually visible in this photo.
- "yes" = false if there is no {target} here at all.
- Even a small portion of a {target} counts as true.
- Do not confuse {target} with other similar-looking objects."""


def lm_locate(clean_grid_path: Path, target: str, grid_size: int) -> str:
    """Step 1: get a plain-English location description from the clean grid."""
    prompt = LOCATE_PROMPT.format(target=target, grid_size=grid_size)
    print(f"      [LM-1] Locating '{target}' ...")
    response = _lm(_image_msg(clean_grid_path, prompt), max_tokens=250)
    print(f"      Location desc: {response[:220].strip()}")
    return response


def lm_classify_tile(tile_path: Path, target: str,
                     context: str = "") -> tuple[bool, float, str]:
    """Step 2: binary classification of a single tile."""
    ctx = context[:250].strip() if context else f"Looking for {target}."
    prompt = TILE_PROMPT.format(target=target, context=ctx)
    raw    = _lm(_image_msg(tile_path, prompt), max_tokens=150)
    parsed = _parse_json(raw) or {}

    # Only a JSON boolean can authorize a tile click: the string "false" is truthy.
    contains = parsed.get("yes", parsed.get("contains", False)) is True
    try:
        confidence = float(parsed.get("confidence", 0.8 if contains else 0.2))
        if not math.isfinite(confidence):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.0
    what_i_see  = str(parsed.get("what_i_see",
                                  parsed.get("explanation",
                                             parsed.get("reason", raw[:80]))))
    return contains, confidence, what_i_see


def lm_full_grid(clean_path: Path, target: str, grid_size: int,
                 tiles: list = None) -> tuple[list, dict, dict]:
    """
    Full pipeline.
    Args:
        clean_path : path to the CLEAN grid image (no overlay, no lines).
        target     : object label e.g. "motorcycles".
        grid_size  : 4 for a 4x4 grid.
        tiles      : list of (tile_num, row, col, path) from split_tiles().
    Returns:
        (positive_tile_numbers, confidence_dict, info_dict)
    """
    total = grid_size * grid_size

    # ── Step 1 ────────────────────────────────────────────────────────────────
    location_text = lm_locate(clean_path, target, grid_size)

    # Early exit if model clearly says no target present
    loc_lower = location_text.lower()
    hard_no  = any(p in loc_lower for p in [
        "no ", "none", "not visible", "cannot see", "can't see",
        "i don't see", "do not see", "there is no", "there are no",
        "0 ", "zero", "not present", "absent", "no motorcycles",
        "no traffic", "no bus", "no car", "no fire",
    ])
    hard_yes = any(p in loc_lower for p in [
        "yes", "visible", "i see", "there is", "there are",
        "can see", "located", "found", "present", "appears",
    ])
    if hard_no and not hard_yes:
        print(f"      Step 1: no {target} detected — skipping tile calls.")
        return [], {}, {"location": location_text, "scene_description": location_text,
                        "tile_results": []}

    if tiles is None or len(tiles) == 0:
        print("      WARNING: no tiles available for Step 2")
        return [], {}, {"location": location_text, "scene_description": location_text,
                        "tile_results": []}

    # ── Step 2: classify every tile ────────────────────────────────────────────
    print(f"      [LM-2] Classifying all {len(tiles)} tiles ...")
    positives:    list[int]        = []
    confidence:   dict[int, float] = {}
    tile_results: list[dict]       = []

    for tile_num, row, col, tile_path in sorted(tiles, key=lambda x: x[0]):
        contains, conf, what = lm_classify_tile(tile_path, target, location_text)
        marker = "✓" if contains else "·"
        print(f"        [{marker}] Tile {tile_num:02d}  conf={conf:.2f}  {what[:55]}")
        tile_results.append({"tile": tile_num, "contains": contains,
                              "confidence": conf, "what": what})
        if contains:
            positives.append(tile_num)
            confidence[tile_num] = conf

    info = {
        "location": location_text,
        "scene_description": location_text,
        "tile_results": tile_results,
    }
    return sorted(positives), confidence, info


def lm_single_tile(tile_path: Path, target: str,
                   scene_description: str = "") -> tuple[bool, float]:
    """Standalone recheck for a single tile (used in the recheck loop)."""
    ctx = scene_description[:250] if scene_description else f"Looking for {target}."
    contains, conf, _ = lm_classify_tile(tile_path, target, ctx)
    return contains, conf

# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
#  PLAYWRIGHT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def pw_wait(page: Page, sec: float, label: str = "") -> None:
    if label:
        print(f"    [{label}] waiting {sec:.1f}s ...")
    page.wait_for_timeout(int(sec * 1000))

def page_wh(page: Page) -> tuple[int, int]:
    vp = page.viewport_size
    return (vp["width"], vp["height"]) if vp else (1440, 960)

def find_checkbox_frame(page: Page) -> tuple[Optional[Frame], Optional[int]]:
    iframes = page.locator('iframe[src*="api2/anchor"]')
    count = iframes.count()
    print(f"    Found {count} anchor iframe(s).")
    for i in range(count):
        try:
            frame = iframes.nth(i).content_frame
            if not frame:
                continue
            cb = frame.locator("#recaptcha-anchor")
            if cb.count() == 0:
                continue
            cb.wait_for(state="visible", timeout=5000)
            print(f"    Using anchor iframe #{i}.")
            return frame, i
        except Exception:
            continue
    return None, None

def checkbox_state(page: Page, idx: int) -> str:
    try:
        frame = page.locator('iframe[src*="api2/anchor"]').nth(idx).content_frame
        if not frame:
            return "missing"
        cb = frame.locator("#recaptcha-anchor")
        if cb.count() == 0:
            return "missing"
        val = cb.get_attribute("aria-checked")
        return val if val is not None else "unknown"
    except Exception:
        return "error"

def get_challenge_frame(page: Page) -> tuple[Optional[Frame], Optional[dict]]:
    loc = page.locator('iframe[src*="api2/bframe"]')
    if loc.count() == 0:
        return None, None
    return loc.nth(0).content_frame, loc.nth(0).bounding_box()

def read_prompt_text(frame: Frame) -> str:
    for sel in [
        ".rc-imageselect-desc-wrapper",
        ".rc-imageselect-desc-no-canonical",
        ".rc-imageselect-desc",
    ]:
        try:
            loc = frame.locator(sel)
            if loc.count() > 0:
                txt = loc.first.inner_text(timeout=3000).strip()
                if txt:
                    return txt
        except Exception:
            pass
    return ""

def detect_grid(frame: Frame) -> tuple[Optional[int], Optional[Locator], Optional[dict]]:
    # Prefer 4x4
    for size, selectors in [
        (4, [".rc-imageselect-table-44"]),
        (3, [".rc-imageselect-table-33", ".rc-imageselect-table"]),
    ]:
        for sel in selectors:
            try:
                loc = frame.locator(sel)
                if loc.count() > 0:
                    box = loc.first.bounding_box()
                    if box:
                        return size, loc.first, box
            except Exception:
                pass
    return None, None, None

def snapshot_grid_bytes(page: Page) -> Optional[bytes]:
    frame, _ = get_challenge_frame(page)
    if not frame:
        return None
    _, el, _ = detect_grid(frame)
    if not el:
        return None
    try:
        return el.screenshot()
    except Exception:
        return None

def wait_for_new_grid(page: Page, prev: Optional[bytes]) -> tuple[bool, Optional[bytes]]:
    t0 = time.time()
    while time.time() - t0 < CHANGE_TIMEOUT:
        cur = snapshot_grid_bytes(page)
        if cur and images_differ(prev, cur):
            return True, cur
        time.sleep(CHANGE_POLL)
    return False, prev

def click_verify(frame: Frame) -> bool:
    for sel in ["#recaptcha-verify-button", "button.rc-button-default",
                "button[class*='verify']"]:
        try:
            btn = frame.locator(sel)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=6000)
                print("    Clicked Verify")
                return True
        except Exception:
            pass
    print("    WARNING: Verify button not found.")
    return False

def click_skip(frame: Frame) -> bool:
    for sel in ["#recaptcha-reload-button", "button.rc-button-reload",
                "button[title='Get a new challenge']"]:
        try:
            btn = frame.locator(sel)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                print("    Clicked Skip / New Challenge.")
                return True
        except Exception:
            pass
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  CLICK ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def tile_page_xy(
    tile_num: int,
    grid_size: int,
    grid_box: dict,
    challenge_box: dict,
) -> tuple[float, float]:
    """Convert tile number to (x, y) in page coordinates."""
    n   = tile_num - 1
    row = n // grid_size
    col = n %  grid_size
    cw  = grid_box["width"]  / grid_size
    ch  = grid_box["height"] / grid_size

    tx = grid_box["x"] + col * cw + cw / 2
    ty = grid_box["y"] + row * ch + ch / 2

    # Playwright bounding boxes are already relative to the main frame.
    return tx, ty

def click_tiles(
    page: Page,
    tile_numbers: list[int],
    grid_size: int,
    grid_box: dict,
    challenge_box: dict,
) -> int:
    cw = grid_box["width"]  / grid_size
    ch = grid_box["height"] / grid_size
    clicked = 0

    for tile_num in tile_numbers:
        px, py = tile_page_xy(tile_num, grid_size, grid_box, challenge_box)

        # Slight random offset within tile
        px += random.uniform(-cw * 0.22, cw * 0.22)
        py += random.uniform(-ch * 0.22, ch * 0.22)

        # Pre-click jitter
        time.sleep(CLICK_JITTER + random.uniform(0, CLICK_JITTER))

        print(f"    -> Click tile {tile_num:02d}  (x={px:.0f}, y={py:.0f})")
        page.mouse.click(px, py)
        clicked += 1

        # Enforced >= 1 second gap
        time.sleep(random.uniform(CLICK_MIN, CLICK_MAX))

    return clicked


# ─────────────────────────────────────────────────────────────────────────────
#  SLOT PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def process_slot(page: Page, run_id: str, slot_idx: int) -> Optional[dict]:
    """Handle one challenge grid. Returns a result dict or None on failure."""
    frame, challenge_box = get_challenge_frame(page)
    if not frame or not challenge_box:
        print("    No challenge frame visible.")
        return None

    # ── 1. Read prompt text as a string (NOT via OCR) ─────────────────────────
    prompt_text  = read_prompt_text(frame)
    target_label = extract_target(prompt_text)

    print(f"    Prompt : {prompt_text!r}")
    print(f"    Target : {target_label!r}")

    if target_label in ("", "unknown"):
        print("    WARNING: Could not parse target from prompt.")

    # ── 2. Detect grid — only process 4×4, skip 3×3 ──────────────────────────
    grid_size, grid_el, grid_box = detect_grid(frame)
    if not grid_size or not grid_el or not grid_box:
        print("    Cannot detect grid element.")
        return None

    print(f"    Grid   : {grid_size}x{grid_size}  ({grid_size*grid_size} tiles)")

    if grid_size != 4:
        print(f"    {grid_size}x{grid_size} grid — not 4x4, clicking Skip to get a new one ...")
        click_skip(frame)
        pw_wait(page, 2.0, "skip non-4x4")
        return {
            "prompt_text": prompt_text, "target_label": target_label,
            "grid_size": grid_size, "grid_path": "", "overlay_path": "",
            "positives": [], "clicked": 0, "verified": False,
            "skipped": True, "skip_reason": "not_4x4",
            "raw_grid_response": {},
        }

    # ── 3. Capture images ─────────────────────────────────────────────────────
    tag    = f"{run_id}_s{slot_idx}"

    full_p  = FULL_DIR    / f"{tag}_full.png"
    grid_p  = GRID_DIR    / f"{tag}_grid.png"
    over_p  = OVERLAY_DIR / f"{tag}_overlay.png"  # kept for archive, not sent to model

    page.screenshot(path=str(full_p), full_page=True)

    # Element screenshots handle iframe offsets and page scrolling directly.
    grid_el.screenshot(path=str(grid_p))
    # Numbered overlay is a debug artifact; model inputs use the clean image.
    make_overlay(grid_p, grid_size, over_p)

    # ── 4. Split grid into individual tiles ──────────────────────────────────
    slot_dir = TILES_DIR / tag
    tiles    = split_tiles(grid_p, grid_size, slot_dir)
    print(f"    Split into {len(tiles)} tiles → {slot_dir}")

    # ── 5. PRIMARY: locate on clean grid → classify every tile individually ───
    print(f"    Running LM pipeline (target={target_label!r}) ...")
    positives, confidence, raw_grid = lm_full_grid(
        grid_p,           # CLEAN grid — no lines, no labels
        target_label,
        grid_size,
        tiles=tiles,      # individual tile images for Step 2
    )
    scene_description = raw_grid.get("scene_description", "")
    print(f"    Positives  : {positives}")
    print(f"    Confidence : { {k: round(v,2) for k,v in confidence.items()} }")

    # ── 6. SECONDARY: re-check low-confidence tiles ───────────────────────────
    uncertain = [n for n in positives if confidence.get(n, 1.0) < RECHECK_THRESHOLD]

    if uncertain:
        print(f"    Re-checking {len(uncertain)} uncertain tile(s): {uncertain}")
        confirmed = []
        for tile_num in positives:
            if tile_num not in uncertain:
                confirmed.append(tile_num)
                continue
            tile_entry = next((t for t in tiles if t[0] == tile_num), None)
            if tile_entry:
                contains, conf = lm_single_tile(tile_entry[3], target_label, scene_description)
                verdict = "keep" if contains else "drop"
                print(f"      Tile {tile_num:02d}: {contains} conf={conf:.2f} -> {verdict}")
                if contains:
                    confirmed.append(tile_num)
        positives = sorted(set(confirmed))
        print(f"    After recheck: {positives}")

    # ── 6. Click positive tiles ───────────────────────────────────────────────
    clicked = 0
    if positives:
        print(f"    Clicking {len(positives)} tile(s): {positives}")
        clicked = click_tiles(page, positives, grid_size, grid_box, challenge_box)
    else:
        print("    No positives — clicking Skip.")
        frame2, _ = get_challenge_frame(page)
        if frame2:
            click_skip(frame2)
        pw_wait(page, 2.0, "after skip")
        return {
            "prompt_text": prompt_text, "target_label": target_label,
            "grid_size": grid_size, "grid_path": str(grid_p),
            "overlay_path": str(over_p), "positives": [],
            "clicked": 0, "verified": False, "skipped": True,
            "raw_grid_response": raw_grid,
        }

    # ── 7. Short pause then click Verify ─────────────────────────────────────
    time.sleep(random.uniform(0.9, 1.5))
    frame3, _ = get_challenge_frame(page)
    verified = click_verify(frame3) if frame3 else False

    return {
        "prompt_text":  prompt_text,
        "target_label": target_label,
        "grid_size":    grid_size,
        "grid_path":    str(grid_p),
        "overlay_path": str(over_p),
        "positives":    positives,
        "confidence":   {str(k): v for k, v in confidence.items()},
        "clicked":      clicked,
        "verified":     verified,
        "skipped":      False,
        "raw_grid_response": raw_grid,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  RUN ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

def peek_grid_size(page: Page) -> Optional[int]:
    """Return the current challenge grid size (3 or 4), or None if no grid visible."""
    frame, _ = get_challenge_frame(page)
    if not frame:
        return None
    grid_size, _, _ = detect_grid(frame)
    return grid_size

def _load_and_click_checkbox(page: Page) -> tuple[Optional[Frame], Optional[int]]:
    """
    Navigate to TEST_URL, wait for it to settle, find the anchor iframe,
    click the checkbox, and wait for the challenge to potentially appear.
    Returns (cb_frame, anchor_idx) or (None, None) on failure.
    """
    page.goto(TEST_URL, wait_until="domcontentloaded", timeout=60_000)
    pw_wait(page, INITIAL_WAIT, "page load")

    cb_frame, anchor_idx = find_checkbox_frame(page)
    if cb_frame is None:
        print("    No anchor iframe found after load.")
        return None, None

    try:
        print("    Clicking reCAPTCHA checkbox ...")
        cb_frame.locator("#recaptcha-anchor").click(timeout=10_000)
    except Exception as e:
        print(f"    Checkbox click failed: {e}")
        return None, None

    # Wait for the challenge to appear (Google can take 2-4s)
    pw_wait(page, AFTER_CHECKBOX_WAIT, "after checkbox")
    return cb_frame, anchor_idx

def run_one(page: Page, run_id: str) -> dict:
    row    = blank_row(run_id)
    report = {"run_id": run_id, "slots": []}

    print(f"\n{'='*68}")
    print(f"  RUN  {run_id}")
    print(f"{'='*68}")

    anchor_idx: Optional[int] = None

    # ── Keep reloading until we get a 4×4 grid ───────────────────────────────
    # Each attempt = fresh page load + checkbox click.
    # A 3×3 means reload. No grid visible means wait a bit longer then reload.
    got_4x4 = False

    for attempt in range(1, MAX_3x3_RETRIES + 1):
        print(f"    [grid hunt] Attempt {attempt}/{MAX_3x3_RETRIES} ...")

        cb_frame, anchor_idx = _load_and_click_checkbox(page)
        if cb_frame is None:
            row["status"] = "NO_CHECKBOX"
            row["notes"]  = f"No usable checkbox on attempt {attempt}."
            return row

        row["anchor_index"] = anchor_idx

        # Quick pass — solved without an image challenge
        state = checkbox_state(page, anchor_idx)
        if state == "true":
            row.update(status="PASSED_NO_CHALLENGE", final_checkbox_state=state)
            return row

        # Give the challenge iframe up to RETRY_WAIT extra seconds to appear
        grid_size_peek = peek_grid_size(page)
        if grid_size_peek is None:
            print(f"    No challenge grid yet — waiting {RETRY_WAIT}s more ...")
            pw_wait(page, RETRY_WAIT, "waiting for grid")
            grid_size_peek = peek_grid_size(page)

        if grid_size_peek == 4:
            print(f"    Got 4x4 grid on attempt {attempt}!")
            got_4x4 = True
            break

        if grid_size_peek == 3:
            print(f"    3x3 grid on attempt {attempt} — reloading for a new one ...")
            # loop continues → next attempt reloads the page
            continue

        # Still None after the extra wait
        print(f"    Challenge did not appear on attempt {attempt} — reloading ...")
        # loop continues

    if not got_4x4:
        row["status"] = "NO_4x4_AFTER_MAX_RETRIES"
        row["notes"]  = f"Never received a 4x4 grid after {MAX_3x3_RETRIES} attempt(s)."
        return row

    prev_bytes     = snapshot_grid_bytes(page)
    total_positive = 0
    total_clicked  = 0

    for slot_idx in range(1, MAX_SLOTS + 1):
        ch_frame, _ = get_challenge_frame(page)
        if not ch_frame:
            print("    Challenge frame gone.")
            break

        print(f"\n  -- SLOT {slot_idx} --------------------------------------------------")
        slot = process_slot(page, run_id, slot_idx)

        if slot is None:
            row["status"] = "SLOT_FAILED"
            row["notes"]  = f"Slot {slot_idx} capture failed."
            break

        row[f"image_{slot_idx}"]        = slot.get("grid_path", "")
        row[f"overlay_{slot_idx}"]      = slot.get("overlay_path", "")
        row[f"lm_positives_{slot_idx}"] = ",".join(str(x) for x in slot.get("positives", []))
        row[f"lm_json_{slot_idx}"]      = json.dumps(slot.get("raw_grid_response", {}), ensure_ascii=False)
        row["prompt_text"]     = slot["prompt_text"]
        row["target_label"]    = slot["target_label"]
        row["grid_size"]       = slot["grid_size"]
        row["slots_processed"] = slot_idx

        total_positive += len(slot.get("positives", []))
        total_clicked  += slot.get("clicked", 0)
        report["slots"].append(slot)

        if slot.get("verified"):
            pw_wait(page, AFTER_VERIFY_WAIT, "after verify")

        state = checkbox_state(page, anchor_idx)
        print(f"    Checkbox state: {state}")

        if state == "true":
            row.update(status="SOLVED", final_checkbox_state=state,
                       verify_outcome="passed",
                       tiles_positive=total_positive,
                       tiles_clicked=total_clicked)
            break

        changed, new_bytes = wait_for_new_grid(page, prev_bytes)
        if changed:
            print("    New grid detected -- continuing ...")
            prev_bytes = new_bytes
            continue

        state = checkbox_state(page, anchor_idx)
        row.update(
            status="SOLVED" if state == "true" else "UNSOLVED",
            final_checkbox_state=state,
            verify_outcome="passed" if state == "true" else "failed",
            tiles_positive=total_positive,
            tiles_clicked=total_clicked,
        )
        break

    else:
        state = checkbox_state(page, anchor_idx)
        row.update(status="MAX_SLOTS", final_checkbox_state=state,
                   verify_outcome="max_slots",
                   tiles_positive=total_positive,
                   tiles_clicked=total_clicked)

    if not row["status"]:
        row["status"] = "UNKNOWN"

    save_report(run_id, report)
    return row


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def _print_summary(row: dict) -> None:
    print(f"\n  +-- RUN RESULT {'─'*40}+")
    print(f"  | Status:          {str(row.get('status','')):<40}|")
    print(f"  | Target:          {str(row.get('target_label','')):<40}|")
    print(f"  | Grid size:       {str(row.get('grid_size','')):<40}|")
    print(f"  | Slots processed: {str(row.get('slots_processed',0)):<40}|")
    print(f"  | Tiles clicked:   {str(row.get('tiles_clicked',0)):<40}|")
    print(f"  | Verify outcome:  {str(row.get('verify_outcome','')):<40}|")
    print(f"  | Final checkbox:  {str(row.get('final_checkbox_state','')):<40}|")
    print(f"  +{'─'*55}+")

def main() -> None:
    ensure_dirs()
    print("=" * 68)
    print("  reCAPTCHA Auto-Solver  v4.0")
    print(f"  URL   : {TEST_URL}")
    print(f"  Runs  : {NUM_RUNS}")
    print(f"  Model : {LM_MODEL}")
    print(f"  Output: {OUTPUT_DIR.resolve()}")
    print("=" * 68)

    if not USE_LM_STUDIO:
        print("  WARNING: USE_LM_STUDIO=False -- capture-only mode.")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO_MS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
            ],
        )
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 960},
            locale="en-GB",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = ctx.new_page()

        try:
            for i in range(1, NUM_RUNS + 1):
                print(f"\n{'#'*68}")
                print(f"  RUN {i}/{NUM_RUNS}")
                print(f"{'#'*68}")
                run_id = make_run_id()
                try:
                    row = run_one(page, run_id)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    row = blank_row(run_id)
                    row["status"] = "CRASHED"
                    row["notes"]  = traceback.format_exc()[-400:]

                append_csv(row)
                _print_summary(row)

                if i < NUM_RUNS:
                    gap = random.uniform(*BETWEEN_RUNS)
                    print(f"\n  Waiting {gap:.1f}s before next run ...")
                    time.sleep(gap)

        except KeyboardInterrupt:
            print("\n  Stopped by user.  CSV already saved.")
        finally:
            ctx.close()
            browser.close()

    print(f"\n  CSV : {CSV_LOG.resolve()}")
    print("  Done.")


if __name__ == "__main__":
    main()