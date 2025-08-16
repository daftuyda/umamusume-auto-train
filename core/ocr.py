import easyocr
from PIL import Image
import numpy as np

reader = easyocr.Reader(["en"], gpu=False)

def extract_text(pil_img: Image.Image) -> str:
  img_np = np.array(pil_img)
  result = reader.readtext(img_np)
  texts = [text[1] for text in result]
  return " ".join(texts)

def extract_number(pil_img: Image.Image) -> int:
  img_np = np.array(pil_img)
  result = reader.readtext(img_np, allowlist="0123456789")
  texts = [text[1] for text in result]
  return " ".join(texts)
