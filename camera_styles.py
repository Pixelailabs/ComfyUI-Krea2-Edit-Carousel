"""Camera looks for the Carousel (tested in krea-edit/cam/, General Edit V2, 2 MP).

The plan's own lens and blur words ("35mm lens feel", "softly blurred ... behind her") gave each shot a different camera
look; they are stripped, and ONE camera sentence is added to every prompt instead.
"""
import re

CAMERAS = ["auto (Lifestyle = smartphone, Fashion / UGC = professional)", "clean (keep the photo's quality)",
           "smartphone (iPhone)", "professional camera", "cinematic"]

CAMERA_TEXT = {
    CAMERAS[1]: "",
    CAMERAS[2]: ("Shot on an iPhone, a casual smartphone photo with the natural look of a phone camera: wide phone lens, "
                 "the background in focus, bright phone HDR, slight digital sharpening."),
    CAMERAS[3]: ("A professional photo on a full-frame camera with an 85mm lens: shallow depth of field, a creamy blurred "
                 "background, crisp detail on her face, polished editorial colour grade."),
    CAMERAS[4]: ("A cinematic film still: anamorphic widescreen look, moody motivated light with deep soft shadows, a teal "
                 "and warm colour grade, fine 35mm film grain, gentle halation around the highlights."),
}

LENS = re.compile(r",?\s*(\d{2,3}\s?mm( lens)?( feel)?|shallow depth of field|bokeh)", re.IGNORECASE)
BLUR = re.compile(r"\b(softly |soft )?(blurred|out of focus|bokeh)\b", re.IGNORECASE)


def camera_text(camera, mode):
    if camera == CAMERAS[0]:
        camera = CAMERAS[2] if mode.startswith("Lifestyle") else CAMERAS[3]
    return CAMERA_TEXT.get(camera, "")


def strip_lens(text):
    return re.sub(r"\s{2,}", " ", LENS.sub("", text)).strip(" ,")


def strip_blur(text):
    return re.sub(r"\s{2,}", " ", BLUR.sub("", text)).strip(" ,")
