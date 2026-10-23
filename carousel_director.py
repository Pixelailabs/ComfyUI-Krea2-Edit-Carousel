"""PixelAI Carousel: one ORIGINAL photo of a person -> a 2/4/8-shot set, made with Krea 2 + Identity Edit V2.

Modes
- Lifestyle:  a little day story (day photo: morning -> night; night photo: night out), new places, poses, angles.
- Fashion:    e-commerce / lookbook shots of an outfit in a clean studio (front, three-quarter, back, detail, close-up).
- UGC product: the person with a product (beside her face, to the lens, in use, in hand), real home / car / bathroom places.
Optional image 2 with a role: product, outfit (she wears it), or person (a second person in the shots).

Everything in here comes from tests (krea-edit/FINDINGS.md):
- the photo's pose, expression and framing come back unless the prompt replaces them: every shot gets a NEW pose with a
  job for each visible hand, and its expression goes first; subtle expressions only land together with a new pose and
  head angle; never mention her hair (brings the photo's hand-in-hair back);
- Krea frames wide enough to show what is named: close-ups name only the top / bodice, arms out of frame, no props;
- second-image wiring per role: product = both refs boosted 2; outfit = her 3 / outfit 1 (else the outfit photo's model
  takes over); person = both "beside", background removed from image 2 (else his room leaks in);
- a worn-outfit photo leaks its model into close-ups less when "her face and her hair exactly as in the first image";
- studio e-commerce = "professional e-commerce photo studio, seamless light grey paper backdrop, soft even light";
- positive wording only; plain props (they copy the outfit's print otherwise).
"""
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.request

import numpy as np
from PIL import Image

from .camera_styles import CAMERAS, camera_text, strip_blur, strip_lens

HERE = os.path.dirname(os.path.abspath(__file__))
DEEPSEEK = "DeepSeek API"
MODEL = "deepseek-flash"  # DeepSeek V4.1 Flash, reads images
BASE_URL = "https://api.deepseek.com"
CACHE = os.path.join(HERE, "cache")
SETUP_TYPE = "CAROUSEL_SETUP"

MODES = ["Lifestyle (day story)", "Fashion (outfit lookbook)", "UGC (product)"]
EXPRESSIONS = ["auto (subtle, a new one each shot)", "natural (relaxed, real - best for UGC)",
               "my list (one per line, in order)", "one for all shots"]
ROLES = ["product", "outfit (she wears it)", "person (in the shots with her)", "body shape (only her figure)"]
STORIES = ["auto (day photo: morning to night, night photo: night out)", "morning to night", "keep the photo's time of day",
           "night out"]

# relaxed, real expressions of a UGC creator talking to her phone
NATURAL_EXPRESSIONS = [
    "a relaxed, friendly soft smile, eyes warm on the lens",
    "a gentle natural smile showing a little of her teeth",
    "mid-sentence, as if talking to the camera: mouth slightly open, brows lifted a little",
    "a light, easy laugh, eyes slightly crinkled",
    "a calm, pleased look, lips softly together",
    "an impressed little nod, eyebrows slightly raised, a small smile",
]

# auto = a safe, fixed pattern (Yassine 2026-10-21: winks and raised brows are often wrong): shot 1 her own natural
# look (no expression written), then slight smiles, then a calm confident look; repeats for 8 shots
AUTO_EXPRESSIONS = [
    "",
    "a slight smile",
    "a slight smile",
    "a calm, confident look into the lens, lips relaxed",
]

LOOK = """You study one photo of a person so an image editor can put them in new scenes while keeping the EXACT face and
EXACT outfit. Answer ONLY JSON:
{"subject": "woman | man",
 "face": "what makes this face theirs, concrete: face shape, eyes (colour, shape), brows, nose, lips, skin tone, freckles,
          moles, makeup",
 "hair": "colour, length, texture, how it is worn",
 "outfit": "every visible garment, concrete: type, colour, print/pattern, fabric, fit, length, how it is worn",
 "top": "only the upper-body garment(s), as concrete as in outfit",
 "bottom": "only the lower-body garment(s) VISIBLE in the photo, or '' when the photo does not show them (a dress: repeat it)",
 "shoes": "the shoes if visible, else ''",
 "accessories": "jewellery, glasses, bag, watch... or ''",
 "place": "where the photo is taken",
 "time_of_day": "morning | midday | afternoon | golden hour | evening | night",
 "pose": {"body": "standing/sitting/leaning and how", "left_hand": "what it does", "right_hand": "what it does",
          "gaze": "into the lens | away", "framing": "close-up | waist-up | knees-up | full body"},
 "expression": "the facial expression in the photo, concrete",
 "vibe": "the lifestyle this person shows (e.g. 'sunny luxury travel', 'cosy city girl', 'fitness')"}"""

ITEM = {
    "product": """Describe this PRODUCT photo for an image editor. Answer ONLY JSON:
{"name": "short name a person would say, e.g. 'the e.l.f. jelly cleanser tube'",
 "description": "shape, size in the hand, colours, material, cap/lid, the main label text exactly as printed",
 "use": "how a person uses it in one short phrase (e.g. 'squeezes a little gel onto her fingertips'), or '' if it is not used on the body",
 "kind": "skincare | makeup | haircare | drink | food | tech | fashion accessory | shoes | bag | other"}""",
    "outfit": """Describe this OUTFIT photo (a garment on a mannequin, flat lay, or worn) for an image editor that will put it
on another person. Answer ONLY JSON:
{"name": "short name, e.g. 'the black one-shoulder bow dress'",
 "front": "the garment from the front, concrete: type, colour, fabric, fit, length, neckline, sleeves, every detail
           (bows, buttons, logos, trims, cut-outs)",
 "top_part": "only what shows from the chest up / waist up, concrete (for close-ups)",
 "back": "the back of the garment if the photo shows it (zip, bare back, bow from behind...), else ''",
 "full_length": true}""",
    "body": """Describe only the BODY SHAPE of the person in this photo (not the clothes, not the face), neutrally, as a
stylist would for fitting clothes. Answer ONLY JSON:
{"name": "short label, e.g. 'curvy hourglass figure'",
 "figure": "the figure in one phrase that starts with 'a', e.g. 'a curvy hourglass figure: a full bust, a narrow
            defined waist, wide rounded hips and full thighs' or 'a slim athletic figure: narrow hips, toned arms, long
            legs'"}""",
    "person": """Describe the person in this photo for an image editor that will put them next to someone else. Answer ONLY JSON:
{"subject": "man | woman", "look": "face, hair, beard, build, concrete", "clothes": "what they wear, concrete"}""",
}

PLAN_COMMON = """
SHOT RULES (they come from what the image model can and cannot do):
- Exactly N shots. Each shot gets a NEW pose and a head angle different from the photo, and EVERY hand in frame has a
  job (holding something, on a railing, in a pocket, on her knee); a hand with no job falls back to the photo's pose.
  Never mention her hair in the pose (it brings back the photo's hand-in-hair).
- The image model frames wide enough to show everything that is named or held:
  close-up = her face and shoulders fill the frame, her arms down out of frame, nothing in her hands, no furniture;
  waist-up = at most ONE small object held at chest height, no table in front of her;
  knees-up = she sits; full body = standing, used when the whole outfit or the shoes matter.
- EXPRESSION: use the expression given for each shot (settings "expressions"), written concretely as eyes + brows +
  mouth (an empty expression = her own natural look, write ""), and pair it with the pose that makes it natural (a wink with the head turned back over the shoulder, a smirk
  leaning on a wall with arms folded, a sultry look with the chin lowered...).
- Positive wording only: never "no", "without", "avoid". People in the background are far and out of focus. Props are
  plain, solid colours. No visible phone unless it is a mirror shot. No text or logos except on the product.
Answer ONLY JSON:
{"story": "one sentence",
 "bottom": "REQUIRED when her description has bottom '' (the photo does not show it): ONE lower-body garment that fits
            her style and the day, e.g. 'light-wash straight-leg jeans', worn in every shot; otherwise ''",
 "shots": [{"title": "2-4 words", "time": "morning | midday | afternoon | golden hour | evening | night | studio",
            "place": "starts with in/at/on: a concrete place with 3-5 real details",
            "background": "only what is BEHIND her, 6-14 words, never blur or focus words",
            "camera": "the camera angle and position only (e.g. 'from a low angle beside the table'), never a lens or focus",
            "framing": "close-up | waist-up | knees-up | full body",
            "pose": "her body and BOTH hands concretely, each doing one thing",
            "expression": "the face as eyes + brows + mouth",
            "eye_contact": true,
            "view": "front | back | side"}]}"""

PLAN_MODE = {
    MODES[0]: """You are a creative Instagram photographer planning a LIFESTYLE CAROUSEL: N moments of one day of her life,
all in the SAME outfit, in time order, telling a small story. A day photo runs morning -> night; a night photo is a night
out. Real places influencers post: in a car, a cafe, a restaurant, a hotel balcony, a boutique, a beach club, a rooftop
bar, a yacht, a gym, a street, an elevator mirror, a market. Every shot a DIFFERENT place and a DIFFERENT, creative camera
angle. Mostly close-up and waist-up; at least one tight close-up of her face. She wears exactly the outfit from the photo
(or the outfit from image 2 when the settings say so).""",
    MODES[1]: """You are a fashion e-commerce photographer planning a LOOKBOOK for an outfit: N clean catalogue shots of her
wearing it. Unless the settings give a vibe/place, every shot is in a professional e-commerce photo studio with a
seamless light grey paper backdrop and soft even light from large softboxes (time "studio"). Shot list in this order, as
many as N allows: 1 full body front, standing, weight on one leg, arms relaxed; 2 tight close-up of her face and the
top of the outfit; 3 full body three-quarter turn, one hand on her hip; 4 full body from behind (view "back"), her head
turned over her shoulder - only when the outfit's back is known, else a side profile; 5 waist-up detail of the outfit's
main feature; 6 walking toward the camera mid-stride; 7 seated on a white studio cube, knees-up; 8 side profile. Calm,
confident, subtle expressions; eye contact on most shots.""",
    MODES[2]: """You are a UGC creator planning PRODUCT PHOTOS for social media: N real, casual shots of her with the
product from image 2, in real places that fit the product (bathroom vanity, bedroom, kitchen counter, car, gym bag,
cafe table, mirror). Shot list in this order, as many as N allows: 1 holding the product up beside her face, its front to the
lens, waist-up; 2 the product held close to the lens in her hand, its front sharp, her face softly behind; 3 using it the way
it is used (from the product's "use"), waist-up; 4 a tight close-up of her face with the product just below her chin;
5 a mirror selfie holding the product; 6 the product in her hand resting on her knee, seated; 7 on the go (car or street)
with the product; 8 a candid laugh with the product. The front of the product always faces the camera (never invent a label). Natural, friendly
expressions; eye contact on most shots.""",
}

FACE_LOCK = "Preserve her exact facial identity: {face}."


# ------------------------------------------------------------------------------------------------ LLM plumbing
def _llm_options():
    try:
        import folder_paths
        return [DEEPSEEK] + folder_paths.get_filename_list("text_encoders")
    except Exception:  # outside ComfyUI (tests)
        return [DEEPSEEK]


def _env_key():
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key and "GOES_HERE" not in key:
                    return key
    return os.environ.get("DEEPSEEK_API_KEY", "").strip() or None


def _jpeg_url(image, side=1024):
    arr = (image[0].cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
    im = Image.fromarray(arr).convert("RGB")
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _json(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON in the answer: {text[:200]}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", match.group(0)))


def _ask(messages, temperature, seed, max_tokens=4000):
    key = _env_key()
    if not key:
        raise RuntimeError("PixelAI Carousel: no DeepSeek API key. Put DEEPSEEK_API_KEY=... in the .env file next to "
                           "this node (in its custom_nodes folder), or pick a local vision_llm.")
    body = json.dumps({"model": MODEL, "messages": messages, "thinking": {"type": "disabled"},
                       "temperature": float(temperature), "max_tokens": max_tokens, "seed": int(seed) % 2**31,
                       "response_format": {"type": "json_object"}}).encode("utf-8")
    last = None
    for attempt in range(4):
        req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=body,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                text = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"] or ""
            return _json(text)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
            last = str(e)  # dropped connection, timeout or broken JSON: ask again
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"PixelAI Carousel: DeepSeek failed: {last}")


@contextlib.contextmanager
def _asker(vision_llm, clip=None):
    """ask(messages, temperature, seed) -> JSON, from DeepSeek or a local Qwen3-VL (loaded once, freed after)."""
    if vision_llm == DEEPSEEK and clip is None:
        yield _ask
        return
    from . import local_vision_llm
    with local_vision_llm.session(vision_llm, clip) as chat:
        def ask(messages, temperature, seed, max_tokens=4000):
            last = None
            for attempt in range(3):
                text = chat(messages, max_tokens, temperature, seed + attempt)
                try:
                    return _json(re.sub(r"^```\w*|```$", "", text.strip()))
                except ValueError as e:
                    last = e
            raise RuntimeError(f"PixelAI Carousel: the local model did not answer JSON: {last}")
        yield ask


# ------------------------------------------------------------------------------------------------ prompt writing
NEGATIVE = re.compile(r"\b(no|without|avoid|never|not|don't|do not)\b[^,.;]*[,.;]?", re.IGNORECASE)
FURNITURE = r"\b(table|plate|counter|desk|bar top|tray|menu|across the)\b"
FRAMING = {"close": "Tight close-up portrait", "waist": "Waist-up shot", "knee": "Shot from the knees up",
           "full": "Full-length shot"}


def _clean(text):
    text = str(text or "").replace('"', "").strip()
    return re.sub(r"\s{2,}", " ", NEGATIVE.sub("", text)).strip(" ,;.")


def _sentence(text):
    text = str(text).strip()
    return (text[:1].upper() + text[1:]) if text else ""


def _article(text):
    return re.sub(r"^(a|an|the)\s+", "", str(text).strip(), flags=re.IGNORECASE)


def _frame(framing):
    f = str(framing).lower()
    for key, words in FRAMING.items():
        if key in f:
            return key, words
    return "waist", FRAMING["waist"]


def _wear(look, item, role, kind):
    """What she wears, naming only what the framing shows (naming the skirt frames wide enough to show it)."""
    if role == "outfit" and item:
        piece = item.get("top_part") if kind in ("close", "waist") and item.get("top_part") else item.get("front")
        return f"she now wears only {_article(_clean(item.get('name')))} from the second image: {_clean(piece)}"
    top, bottom = _clean(look.get("top")), _clean(look.get("user_bottom") or look.get("bottom") or look.get("chosen_bottom"))
    if kind == "close" and top:
        return f"wearing the same {_article(top)}"
    if kind == "waist" and top:
        return f"wearing the same {_article(top)}" + (f" with {_article(bottom)}" if bottom else "")
    parts = [x for x in (top, bottom) if x] or [_clean(look.get("outfit"))]
    if kind == "full" and _clean(look.get("shoes")):
        parts.append(_clean(look.get("shoes")))
    return "wearing the same " + _article(", ".join(parts))


def build_prompt(shot, look, mode, role="", item=None, keep_face=False, camera=CAMERAS[0], studio_ok=True):
    kind, lead = _frame(shot.get("framing"))
    if role == "person" and kind == "close":  # two faces in a close-up fuse into one (v2/4_couple shot 3)
        kind, lead = "waist", FRAMING["waist"]
    studio = studio_ok and mode == MODES[1] and "studio" in str(shot.get("time", "")).lower() + str(shot.get("place", "")).lower()
    if studio:
        where = ("in a professional e-commerce photo studio, a seamless light grey paper backdrop, soft even studio "
                 "lighting from large softboxes")
    elif kind in ("close", "waist") and shot.get("background"):
        where = f"with {_article(strip_blur(_clean(shot.get('background'))))} behind her"
    else:
        where = _clean(shot.get("place"))
        if not where.lower().startswith(("in ", "on ", "at ", "by ", "inside ", "under ")):
            where = "in " + where
    time_ = "" if studio else f", {_clean(shot.get('time'))}"
    wear = _wear(look, item, role, kind)
    back = str(shot.get("view", "")).lower() == "back"
    if role == "outfit" and back and item and item.get("back"):
        wear = (f"she now wears only {_article(_clean(item.get('name')))} from the second image, seen from behind: "
                f"{_clean(item.get('back'))}")
    parts = [_sentence(_clean(shot.get("expression"))) + "." if shot.get("expression") and not back else "",
             f"{lead} {where}{time_}."]
    if role == "outfit":
        parts.append(f"Remove her own clothes: {wear}.")
    else:
        parts.append(f"She is {wear}.")
    if kind == "close" and role == "product":   # UGC close-up: the product stays in the shot (it was dropped)
        parts.append("Her face and shoulders fill the frame, one hand holds the product up just below her chin.")
    elif kind == "close":
        parts.append("Her face and shoulders fill the frame, both arms down out of frame.")
    else:
        angle = strip_lens(_clean(shot.get("camera")))
        if kind in ("close", "waist"):
            angle = ", ".join(c for c in angle.split(",") if not re.search(FURNITURE, c, re.IGNORECASE))
        if angle:
            parts.append(_sentence(angle) + ".")
        pose = re.sub(r"[^,.;]*\bhair\b[^,.;]*[,.;]?", "", _clean(shot.get("pose")))
        if pose:
            parts.append(_sentence(pose) + ".")
    if role == "product" and item:
        parts.append(f"The product is {_article(_clean(item.get('name')))} from the second image: "
                     f"{_clean(item.get('description'))}; its front faces the camera.")  # 'label' invented paper labels
    if role == "person" and item:
        parts.append(f"The {item.get('subject') or 'man'} from the second image stands on the right, she is on the left, "
                     f"both faces clearly visible: {_clean(item.get('look'))}, wearing {_clean(item.get('clothes'))}.")
    if back:
        parts.append("She is alone in the photo, one woman seen from behind, her head turned over her shoulder "
                     "toward the camera.")
    elif shot.get("eye_contact"):
        parts.append("Her eyes look straight into the camera lens.")
    if role == "outfit" and not back:  # the outfit's model walked into the shot as a second woman (course part 3)
        parts.append("She is the only woman in the photo.")
    if look.get("body_text"):
        body = _clean(look["body_text"])
        if not re.match(r"(a|an)\s", body, re.IGNORECASE):
            body = "a " + body
        if look.get("body_from_image"):
            parts.append(f"Her body has the figure of the woman in the second image: {body}; only her body shape comes "
                         f"from the second image, her clothes stay her own.")
        else:
            parts.append(f"Her body has {body}.")
    if role in ("outfit", "person", "body"):
        hair = _clean(look.get("hair"))
        parts.append("Her face and her hair are exactly as in the first image" + (f": {hair}." if hair else "."))
    if keep_face and look.get("face"):
        parts.append(FACE_LOCK.format(face=_clean(look["face"])))
    parts.append(camera_text(camera, mode))
    prompt = " ".join(p for p in parts if p.strip(". "))
    return re.sub(r"\.\s*\.", ".", re.sub(r"\s{2,}", " ", prompt))


def _expressions(mode_, text, n, seed):
    lines = [l.strip() for l in str(text or "").splitlines() if l.strip()]
    if mode_ == EXPRESSIONS[2] and lines:
        return [lines[k % len(lines)] for k in range(n)]
    if mode_ == EXPRESSIONS[3] and lines:
        return [lines[0]] * n
    if mode_ == EXPRESSIONS[1]:
        start = int(seed) % len(NATURAL_EXPRESSIONS)
        return [NATURAL_EXPRESSIONS[(start + k) % len(NATURAL_EXPRESSIONS)] for k in range(n)]
    return [AUTO_EXPRESSIONS[k % len(AUTO_EXPRESSIONS)] for k in range(n)]


def _setup(role):
    """The second-image wiring the tests proved (roles/, step2, step4, body/)."""
    if role in ("outfit", "body"):
        return {"role": role, "ref_boost_her": 3.0, "ref_boost_item": 1.0, "her_position": "aligned (trained)",
                "item_position": "beside (right of target)", "remove_item_background": False}
    if role == "person":
        return {"role": role, "ref_boost_her": 2.0, "ref_boost_item": 2.0, "her_position": "beside (right of target)",
                "item_position": "beside (right of target)", "remove_item_background": True}
    if role == "product":
        return {"role": role, "ref_boost_her": 2.0, "ref_boost_item": 2.0, "her_position": "beside (right of target)",
                "item_position": "beside (right of target)", "remove_item_background": False}
    return {"role": "", "ref_boost_her": 2.0, "ref_boost_item": 1.0, "her_position": "beside (right of target)",
            "item_position": "beside (right of target)", "remove_item_background": False}


def _role_key(role_label):
    for key in ("product", "outfit", "body", "person"):
        if role_label.startswith(key):
            return key
    return "person"


def _hash(image):
    return hashlib.sha256(image[0].cpu().numpy().tobytes()[::97]).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------ nodes
OPTIONS_TYPE = "CAROUSEL_OPTIONS"
FACE_CHOICES = ["auto (a new subtle one each shot)", "natural (relaxed - best for UGC)", "custom (write them below)"]
IMAGE_2_CHOICES = ["outfit - she wears it", "product - she holds / uses it", "person - in the shots with her",
                   "body shape - only her figure"]


class PixelAICarouselOptions:
    """Rarely needed settings for the Carousel Director (connect to its 'options' input)."""
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "story": (STORIES, {"default": STORIES[0], "tooltip": "Lifestyle only: the order of the day."}),
                "creativity": ("FLOAT", {"default": 0.9, "min": 0.0, "max": 1.5, "step": 0.05,
                                         "tooltip": "How bold the places and camera angles are. 0.6 calm, 1.2 wild."}),
                "image_2_name": ("STRING", {"default": "", "tooltip": "What image 2 is, e.g. 'e.l.f. jelly cleanser'. "
                                                                      "Empty = read from the image."}),
                "face_detail_line": ("BOOLEAN", {"default": False,
                                     "tooltip": "Adds a line describing her face. Turn on if a face drifts."}),
                "body_image_to_krea": ("BOOLEAN", {"default": False, "tooltip":
                                       "Only for image_2_is = body shape. Off (recommended): the figure is described "
                                       "in words. On: the image is also given to the image model - a stronger figure, "
                                       "but its clothes can leak into full-length shots."}),
                "vision_llm": (_llm_options(), {"default": DEEPSEEK,
                               "tooltip": "Who plans the shots: DeepSeek API (best), or a local Qwen3-VL model."}),
            },
            "optional": {"clip": ("CLIP", {"tooltip": "A Qwen3-VL CLIP already loaded (used instead of vision_llm)."})},
        }

    RETURN_TYPES = (OPTIONS_TYPE,)
    RETURN_NAMES = ("options",)
    FUNCTION = "options"
    CATEGORY = "PixelAI Labs/Carousel"
    DESCRIPTION = "Optional extra settings for the Carousel Director."

    def options(self, story, creativity, image_2_name, face_detail_line, body_image_to_krea, vision_llm, clip=None):
        return ({"story": story, "creativity": creativity, "image_2_name": image_2_name,
                 "face_detail_line": face_detail_line, "body_image_to_krea": body_image_to_krea,
                 "vision_llm": vision_llm, "clip": clip},)


class PixelAICarouselDirector:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "photo": ("IMAGE", {"tooltip": "The ORIGINAL photo of the person. Never a generated image."}),
                "mode": (MODES, {"default": MODES[0], "tooltip":
                         "Lifestyle: her day in real places (cafe, car, rooftop...).\n"
                         "Fashion: a lookbook of an outfit (connect it as image 2) - studio, or a place you write in "
                         "'scene'.\nUGC: her with a product (connect it as image 2)."}),
                "shots": (["2", "4", "8"], {"default": "4"}),
                "camera": (CAMERAS, {"default": CAMERAS[0], "tooltip":
                           "smartphone: real iPhone look. professional: pro camera, blurred background. cinematic: "
                           "film still. clean: keeps the photo's own quality. auto: smartphone for Lifestyle, "
                           "professional for Fashion and UGC."}),
                "face": (FACE_CHOICES, {"default": FACE_CHOICES[0], "tooltip":
                         "auto: a different subtle expression each shot (smirk, wink, soft smile...).\n"
                         "natural: relaxed and real, best for UGC.\ncustom: write them in 'custom_faces'."}),
                "custom_faces": ("STRING", {"default": "", "multiline": True, "tooltip":
                                 "Only for face = custom. One line = the same for every shot. Several lines = one per "
                                 "shot, in order. e.g.\nslight smirk\nwink\nsoft smile"}),
                "scene": ("STRING", {"default": "", "multiline": True, "tooltip":
                          "Optional. One line = the vibe, e.g. 'her day in Marrakech', 'street style on a sunny city "
                          "street'. Several lines = exact places, one per shot. Empty = chosen from the photo."}),
                "bottoms": ("STRING", {"default": "", "tooltip":
                            "Optional. What she wears below the waist when the photo does not show it, e.g. "
                            "'light-wash jeans'. Empty = picked once and kept in every shot."}),
                "body_shape": ("STRING", {"default": "", "tooltip":
                               "Optional. Her figure in words, e.g. 'curvy hourglass figure', 'slim athletic figure', "
                               "'petite'. Empty = her own. (Or connect a body photo as image 2 with image_2_is = body "
                               "shape.) Shows from waist-up shots down, most in full length."}),
                "image_2_is": (IMAGE_2_CHOICES, {"default": IMAGE_2_CHOICES[0], "tooltip":
                               "Only used when image 2 is connected."}),
                "seed": ("INT", {"default": 1, "min": 0, "max": 0xffffffff,
                                 "tooltip": "Change it for a new set of shots from the same photo."}),
            },
            "optional": {
                "image_2": ("IMAGE", {"tooltip": "Optional: an outfit (best: ghost mannequin / flat lay; front + back "
                                                 "for back shots), a product, or a second person."}),
                "options": (OPTIONS_TYPE, {"tooltip": "Optional: Carousel Options node (story, creativity, LLM...)."}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", SETUP_TYPE, "INT")
    RETURN_NAMES = ("prompts", "plan", "setup", "count")
    OUTPUT_IS_LIST = (True, False, False, False)
    FUNCTION = "direct"
    CATEGORY = "PixelAI Labs/Carousel"
    DESCRIPTION = ("One photo -> 2/4/8 shots of the same person: Lifestyle carousel, Fashion lookbook, or UGC product "
                   "photos. Optional image 2: an outfit, a product or a second person.")

    def direct(self, photo, mode, shots, camera, face, custom_faces, scene, bottoms, image_2_is, seed, image_2=None,
               options=None, body_shape=""):
        o = options or {}
        lines = [l.strip() for l in str(scene or "").splitlines() if l.strip()]
        vibe, places = (lines[0], "") if len(lines) == 1 else ("", "\n".join(lines))
        faces = [l.strip() for l in str(custom_faces or "").splitlines() if l.strip()]
        if face == FACE_CHOICES[0]:
            expressions = EXPRESSIONS[0]
        elif face == FACE_CHOICES[1]:
            expressions = EXPRESSIONS[1]
        else:
            if not faces:
                raise ValueError("face = custom: write at least one expression in 'custom_faces'.")
            expressions = EXPRESSIONS[3] if len(faces) == 1 else EXPRESSIONS[2]
        role_label = {"outfit": ROLES[1], "product": ROLES[0], "person": ROLES[2],
                      "body": ROLES[3]}[image_2_is.split(" ")[0]]
        return self._run(photo, mode, shots, expressions, camera, "\n".join(faces), seed, image_2, role_label,
                         o.get("image_2_name", ""), vibe, places, o.get("story", STORIES[0]), o.get("creativity", 0.9),
                         o.get("face_detail_line", False), o.get("vision_llm", DEEPSEEK), o.get("clip"), bottoms,
                         body_shape, o.get("body_image_to_krea", False))

    def _run(self, image, mode, shots, expressions, camera, expression_list, seed, image_2=None, image_2_is=ROLES[0],
             image_2_text="", vibe="", places="", story=STORIES[0], creativity=0.9, keep_face_line=False,
             vision_llm=DEEPSEEK, clip=None, bottoms="", body_shape="", body_to_krea=False):
        n = int(shots)
        role = _role_key(image_2_is) if image_2 is not None else ""
        if mode == MODES[2] and role != "product":
            raise ValueError("UGC mode needs the product photo connected as image 2, with image_2_is = product.")
        if mode == MODES[1] and role not in ("outfit", "body", ""):
            raise ValueError("Fashion mode: connect the outfit as image 2 with image_2_is = outfit (or no image 2 to "
                             "shoot her own outfit).")
        os.makedirs(CACHE, exist_ok=True)
        who = vision_llm if clip is None else f"clip:{type(clip.cond_stage_model).__name__}"
        h1, h2 = _hash(image), (_hash(image_2) if image_2 is not None else "")
        key = hashlib.sha256(json.dumps([h1, h2, role, mode, n, expressions, expression_list, seed, image_2_text, vibe,
                                         places, story, creativity, keep_face_line, who, bottoms,
                                         camera, body_shape, body_to_krea]).encode()).hexdigest()[:16]
        path = os.path.join(CACHE, f"{key}.json")
        setup = _setup("" if role == "body" and not body_to_krea else role)  # body in words only: no image 2 to Krea
        if os.path.exists(path):
            data = json.load(open(path, encoding="utf-8"))
            return (data["prompts"], data["plan"], setup, len(data["prompts"]))
        with _asker(vision_llm, clip) as ask:
            prompts, plan_text = self._direct(ask, who, image, image_2, h1, h2, role, mode, n, expressions,
                                              expression_list, seed, image_2_text, vibe, places, story, creativity,
                                              keep_face_line, bottoms, camera, body_shape, body_to_krea)
        json.dump({"prompts": prompts, "plan": plan_text}, open(path, "w", encoding="utf-8"), indent=1,
                  ensure_ascii=False)
        return (prompts, plan_text, setup, len(prompts))

    def _direct(self, ask, who, image, image_2, h1, h2, role, mode, n, expressions, expression_list, seed,
                image_2_text, vibe, places, story, creativity, keep_face_line, bottoms="", camera=CAMERAS[0],
                body_shape="", body_to_krea=False):
        tag = "" if who == DEEPSEEK else "_" + hashlib.sha256(who.encode()).hexdigest()[:6]
        side = 1024 if who == DEEPSEEK else 768
        url = _jpeg_url(image, side)

        def cached(name, build):
            p = os.path.join(CACHE, f"{name}{tag}.json")
            if os.path.exists(p):
                return json.load(open(p, encoding="utf-8"))
            data = build()
            json.dump(data, open(p, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
            return data

        look = cached(f"look_{h1}", lambda: ask([{"role": "system", "content": LOOK},
                                                 {"role": "user", "content": [{"type": "text", "text": "The photo:"},
                                                  {"type": "image_url", "image_url": {"url": url}}]}], 0.2, seed))
        item = None
        if role:
            item = cached(f"item_{role}_{h2}", lambda: ask(
                [{"role": "system", "content": ITEM[role]},
                 {"role": "user", "content": [{"type": "text", "text": "The image" + (
                     f" ({image_2_text.strip()})" if image_2_text.strip() else "") + ":"},
                     {"type": "image_url", "image_url": {"url": _jpeg_url(image_2, side)}}]}], 0.2, seed))
            if image_2_text.strip():
                item["name"] = image_2_text.strip()

        exprs = _expressions(expressions, expression_list, n, seed)
        if bottoms.strip():
            look = dict(look, bottom=bottoms.strip())  # the plan knows it too
        settings = {"N": n, "mode": mode, "story": story, "vibe": vibe.strip() or look.get("vibe", ""),
                    "places_to_use": [p.strip() for p in places.splitlines() if p.strip()],
                    "photo_time_of_day": look.get("time_of_day", ""),
                    "expressions": [f"shot {k + 1}: {e}" for k, e in enumerate(exprs)],
                    "image_2": ({"role": role, **item} if item else None)}
        if role == "outfit":
            settings["note"] = "She wears the outfit from image 2 in every shot (not her own clothes)."
        if mode == MODES[1] and (vibe.strip() or places.strip()):
            settings["location"] = ("The director gave a place: every shot is in " + (vibe.strip() or places.strip())
                                    + ", a real outdoor or indoor location (time of day, never 'studio').")
        if role == "person":
            settings["note"] = ("The person from image 2 is in EVERY shot with her, side by side (she on the left, they on "
                                "the right), both faces clearly visible. Framing waist-up or knees-up only, never a "
                                "close-up (two faces close together fuse into one).")
        system = PLAN_MODE[mode] + "\n" + PLAN_COMMON
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": [{"type": "text", "text": "HER (from the photo): "
                                                  + json.dumps(look, ensure_ascii=False) + "\nSETTINGS: "
                                                  + json.dumps(settings, ensure_ascii=False)},
                                                 {"type": "image_url", "image_url": {"url": url}}]}]
        plan = None
        for _ in range(3):
            plan = ask(messages, creativity, seed)
            got = [s for s in plan.get("shots", []) if isinstance(s, dict)]
            problems = []
            if len(got) != n:
                problems.append(f"give exactly {n} shots (you gave {len(got)})")
            if mode == MODES[0] and len({str(s.get('place', '')).lower()[:30] for s in got}) < len(got):
                problems.append("every shot needs a different place")
            if not problems:
                break
            messages = messages + [{"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)},
                                   {"role": "user", "content": "Fix and answer the full JSON again: " + "; ".join(problems)}]
        shots_ = [s for s in plan.get("shots", []) if isinstance(s, dict)][:n]
        if not shots_:
            raise RuntimeError("PixelAI Carousel: the plan has no shots; change the seed and run again.")
        if bottoms.strip():
            look = dict(look, user_bottom=bottoms.strip())
        elif role != "outfit" and not str(look.get("bottom") or "").strip():  # one bottom for the whole set
            look = dict(look, chosen_bottom=str(plan.get("bottom") or "").strip() or "light-wash straight-leg jeans")
        body = body_shape.strip() or (_clean(item.get("figure")) if role == "body" and item else "")
        if body:  # tested (body/): the figure in words changes her shape, her clothes and face stay
            look = dict(look, body_text=body, body_from_image=(role == "body" and body_to_krea))
        for s, e in zip(shots_, exprs):  # the chosen expression, exactly (the LLM tends to dramatize it)
            s["expression"] = e
        studio_ok = not (mode == MODES[1] and (vibe.strip() or places.strip()))
        prompts = [build_prompt(s, look, mode, role, item, keep_face_line, camera, studio_ok) for s in shots_]
        text = [f"MODE: {mode}" + (f" | IMAGE 2: {role} - {item.get('name', '')}" if item else ""),
                f"STORY: {plan.get('story', '')}", ""]
        for k, (s, p) in enumerate(zip(shots_, prompts), 1):
            text.append(f"{k}. {s.get('title', '')} ({s.get('time', '')}, {s.get('framing', '')})\n{p}\n")
        print(f"[PixelAI Carousel] {mode}: {len(prompts)} shots: " + " | ".join(s.get("title", "") for s in shots_))
        return prompts, "\n".join(text)


class PixelAICarouselKrea:
    """Krea 2 identity edit for each shot, wired the way the tests proved for the image 2 role (from the Director)."""
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL", {"tooltip": "Krea 2 with the Identity Edit V2 LoRA (+ your style LoRAs)."}),
                "clip": ("CLIP", {"tooltip": "The Krea 2 text encoder (Qwen3-VL 4B, type krea2)."}),
                "vae": ("VAE",),
                "image": ("IMAGE", {"tooltip": "The same ORIGINAL photo as the Director."}),
                "prompt": ("STRING", {"forceInput": True, "tooltip": "From Carousel Director 'prompts' (one run per shot)."}),
                "setup": (SETUP_TYPE,),
                "latent": ("LATENT", {"tooltip": "The empty latent that goes to the sampler."}),
            },
            "optional": {
                "image_2": ("IMAGE", {"tooltip": "The same image 2 as the Director (product / outfit / person)."}),
                "grounding_px": ("INT", {"default": 768, "min": 256, "max": 1536, "step": 64}),
            },
        }

    RETURN_TYPES = ("MODEL", "CONDITIONING", "CONDITIONING")
    RETURN_NAMES = ("model", "positive", "negative")
    FUNCTION = "edit"
    CATEGORY = "PixelAI Labs/Carousel"
    DESCRIPTION = "Krea 2 edit conditioning + model patch for one carousel shot, with the right image-2 wiring."

    def edit(self, model, clip, vae, image, prompt, setup, latent, image_2=None, grounding_px=768):
        import nodes
        mappings = nodes.NODE_CLASS_MAPPINGS
        if "Krea2EditModelPatch" not in mappings:
            raise RuntimeError("PixelAI Carousel needs the comfyui-krea2edit node pack (Krea2EditModelPatch).")
        patch, encode = mappings["Krea2EditModelPatch"](), mappings["Krea2EditGroundedEncode"]()
        setup = setup or _setup("")
        if image_2 is not None and setup.get("remove_item_background") and "easy imageRemBg" in mappings:
            try:  # a second person's room leaks into the scene: keep only the person
                image_2 = mappings["easy imageRemBg"]().remove(rem_mode="RMBG-1.4", image_output="Hide",
                                                               save_prefix="ComfyUI", images=image_2,
                                                               add_background="white")["result"][0]
            except Exception as error:  # the remover's API differs between versions: go on without it
                print(f"[PixelAI Carousel] background remover skipped: {error}")
        src = vae.encode(image[:, :, :, :3])
        kw = dict(model=model, source_latent={"samples": src}, vae=vae, source_image=image, target_latent=latent,
                  fit_mode="fit")
        if image_2 is not None and setup.get("role"):
            kw.update(source_latent_b={"samples": vae.encode(image_2[:, :, :, :3])}, source_image_b=image_2,
                      ref_boost_a=setup["ref_boost_her"], ref_boost=setup["ref_boost_item"],
                      ref_position_a=setup["her_position"], ref_position=setup["item_position"])
            img_b = image_2
        else:
            kw.update(ref_boost=setup.get("ref_boost_her", 2.0), ref_position=setup.get("her_position",
                                                                                          "beside (right of target)"))
            img_b = None
        patched = patch.patch(**kw)[0]
        pos = encode.encode(clip, prompt, image=image, image_b=img_b, grounding_px=grounding_px)[0]
        neg = encode.encode(clip, "", image=image, image_b=img_b, grounding_px=grounding_px)[0]
        return (patched, pos, neg)


class PixelAICarouselSheet:
    """All shots of one run (a list) -> one preview grid (the 4-px right edge Krea sometimes leaves is trimmed)."""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",), "columns": ("INT", {"default": 4, "min": 1, "max": 8})}}

    INPUT_IS_LIST = True
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("sheet",)
    FUNCTION = "sheet"
    CATEGORY = "PixelAI Labs/Carousel"
    DESCRIPTION = "Lays every carousel shot side by side in one image to preview the set."

    def sheet(self, images, columns):
        import torch
        cols = int(columns[0])
        frames = [im[i] for im in images for i in range(im.shape[0])]
        h = min(f.shape[0] for f in frames)
        tiles = []
        for f in frames:
            w = round(f.shape[1] * h / f.shape[0])
            tiles.append(torch.nn.functional.interpolate(f.permute(2, 0, 1)[None], size=(h, w), mode="bilinear",
                                                         align_corners=False)[0].permute(1, 2, 0))
        cols = max(1, min(cols, len(tiles)))  # 2 shots = 2 columns, no empty space
        w = max(t.shape[1] for t in tiles)
        rows = (len(tiles) + cols - 1) // cols
        gap = 12
        out = torch.ones((rows * h + (rows - 1) * gap, cols * w + (cols - 1) * gap, 3))
        for k, t in enumerate(tiles):
            r, c = divmod(k, cols)
            out[r * (h + gap): r * (h + gap) + h, c * (w + gap): c * (w + gap) + t.shape[1]] = t[..., :3]
        return (out[None],)
