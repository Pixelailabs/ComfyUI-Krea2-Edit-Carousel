"""PixelAI Labs - Carousel: one photo -> a 2/4/8-shot Lifestyle / Fashion / UGC set with Krea 2 edit."""
from .carousel_director import (PixelAICarouselDirector, PixelAICarouselKrea, PixelAICarouselOptions,
                                PixelAICarouselSheet)

NODE_CLASS_MAPPINGS = {"PixelAI_Carousel_Director": PixelAICarouselDirector,
                       "PixelAI_Carousel_Options": PixelAICarouselOptions,
                       "PixelAI_Carousel_Krea": PixelAICarouselKrea,
                       "PixelAI_Carousel_Sheet": PixelAICarouselSheet}
NODE_DISPLAY_NAME_MAPPINGS = {"PixelAI_Carousel_Director": "Carousel Director (PixelAI Labs)",
                              "PixelAI_Carousel_Options": "Carousel Options (PixelAI Labs)",
                              "PixelAI_Carousel_Krea": "Carousel Krea Edit (PixelAI Labs)",
                              "PixelAI_Carousel_Sheet": "Carousel Sheet (PixelAI Labs)"}
print("[PixelAI Carousel] loaded: Carousel Director, Carousel Options, Carousel Krea Edit, Carousel Sheet")
