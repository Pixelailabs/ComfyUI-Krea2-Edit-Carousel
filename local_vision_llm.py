"""A local Qwen3-VL as the vision LLM, in place of the DeepSeek API.

Nodes build the same chat messages for both. The local model is either a CLIP the workflow already loaded,
which stays as the workflow left it, or a text encoder file loaded for one node call and freed right after,
so the video models get the VRAM and RAM back.
"""
import base64
import contextlib
import io

import numpy as np
import torch
from PIL import Image

import comfy.model_management
import comfy.sd
import folder_paths

DEEPSEEK = "DeepSeek API"
TOOLTIP = ("Who reads the images. DeepSeek API, or a local Qwen3-VL text encoder with vision and an LM head "
           "(e.g. qwen3vl_8b_int8_convrot). The local model is loaded for this node only and freed right after. "
           "The H3 text encoder cannot write text.")
CLIP_TOOLTIP = ("Optional: a Qwen3-VL CLIP the workflow already loads, e.g. Krea2's Huihui text encoder. When "
                "connected it writes the prompt instead of vision_llm, so the model is not loaded a second time.")


def options():
    return [DEEPSEEK] + folder_paths.get_filename_list("text_encoders")


def answerer(vision_llm, clip=None):
    """Who writes the prompt: DEEPSEEK, the connected CLIP or a text encoder file; also names cached readings."""
    if clip is not None:
        return f"connected CLIP {type(clip.cond_stage_model).__name__} {clip.patcher.model_size()}"
    return vision_llm


def _image(url):
    picture = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")
    return torch.from_numpy(np.asarray(picture, dtype=np.float32) / 255.0)[None]


def chat_text(messages):
    """Qwen chat template for OpenAI-style messages; data-URL images become <|image_pad|> slots."""
    text, images = "", []
    for message in messages:
        text += f"<|im_start|>{message['role']}\n"
        parts = message["content"]
        for part in [{"type": "text", "text": parts}] if isinstance(parts, str) else parts:
            if part["type"] == "text":
                text += part["text"] + "\n"
            else:
                text += "<|vision_start|><|image_pad|><|vision_end|>"
                images.append(_image(part["image_url"]["url"]))
        text += "<|im_end|>\n"
    return text + "<|im_start|>assistant\n<think>\n\n</think>\n\n", images


def _chat(clip):
    def chat(messages, max_tokens, temperature=0.0, seed=0):
        text, images = chat_text(messages)
        tokens = clip.tokenize(text, images=images)
        ids = clip.generate(tokens, do_sample=temperature > 0, max_length=int(max_tokens),
                            temperature=float(temperature) or 1.0, seed=int(seed) & 0xffffffffffffffff)
        return clip.decode(ids).strip()
    return chat


@contextlib.contextmanager
def session(vision_llm, clip=None):
    """Yield chat(messages, max_tokens, temperature=0.0, seed=0) for the connected CLIP, or for the named text
    encoder, which is loaded for this call only and freed after it."""
    if clip is not None:
        yield _chat(clip)
        return
    clip = comfy.sd.load_clip(ckpt_paths=[folder_paths.get_full_path_or_raise("text_encoders", vision_llm)],
                              embedding_directory=folder_paths.get_folder_paths("embeddings"))
    try:
        yield _chat(clip)
    finally:
        comfy.model_management.unload_model_and_clones(clip.patcher)
        comfy.model_management.soft_empty_cache()
