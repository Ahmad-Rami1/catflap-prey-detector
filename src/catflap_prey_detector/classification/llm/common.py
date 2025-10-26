"""
Common utilities and constants for LLM API testing.
"""
import base64
import io
import time
from PIL import Image


CLEAN_IMAGE_PATH = "/home/flo/catflap-prey-detector/data/Poco and Loco/clean/2021_08_07_16-02-29-370676.jpg"
PREY_IMAGE_PATH = "/home/flo/catflap-prey-detector/data/Poco and Loco/prey/2021_08_18_22-28-43-019044.jpg"

TARGET_SIZE = 384

DETECTION_PROMPT = """
There is a NoIR camera installed outdoor below a catflap. You are seeing images of the family cat taken by the camera when it approaches the catflap or exits it. The ground is paved.
The cat is a female with classic tabby with white — dark gray tabby stripes on the back and sides, with white on the face, chest, belly, and legs. It has a dark spot next to its mouth on the left side of the face. its neck is white.
Your goal is to detect if the cat is carrying a prey in its mouth. Preys are usually very small mices.
Your output should ONLY be 2 booleans formatted like this: "True, False"
The first one to indicate if the cat mouth, ears and whiskers are visible
The second one or indicate if the cat is holding something in its mouth
Do not get confused by close up images of the cat chest.
""" 

def encode_image(image_path: str) -> str:
    """Encode an image file to base64 string."""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')


def encode_pil_image(pil_image: Image.Image) -> str:
    """Encode a PIL Image to base64 string."""
    buffer = io.BytesIO()
    pil_image.save(buffer, format='JPEG')
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode('utf-8')


def resize_image_proportionally(img: Image.Image, target_size: int = TARGET_SIZE) -> Image.Image:
    """Resize image proportionally to fit within target_size while maintaining aspect ratio."""
    width, height = img.size
    if width > height:
        new_width = target_size
        new_height = int(height * (target_size / width))
    else:
        new_height = target_size
        new_width = int(width * (target_size / height))
    
    return img.resize((new_width, new_height), Image.Resampling.LANCZOS)


def load_and_prepare_image(image_path: str, target_size: int = TARGET_SIZE, show_image: bool = False, resize: bool = True) -> tuple[Image.Image, str]:
    """Load an image, optionally resize it, and return both the PIL image and base64 encoded string."""
    img = Image.open(image_path)
    
    if resize:
        img_resized = resize_image_proportionally(img, target_size)
    else:
        img_resized = img
    
    if show_image:
        img_resized.show()
    
    image_base64 = encode_pil_image(img_resized)
    return img_resized, image_base64


def measure_response_time(func, *args, **kwargs) -> tuple[float, any]:
    """Measure the response time of a function call in milliseconds."""
    start_time = time.time()
    result = func(*args, **kwargs)
    end_time = time.time()
    response_time_ms = (end_time - start_time) * 1000
    return response_time_ms, result


async def measure_response_time_async(async_func, *args, **kwargs) -> tuple[float, any]:
    """Measure the response time of an async function call in milliseconds."""
    start_time = time.time()
    result = await async_func(*args, **kwargs)
    end_time = time.time()
    response_time_ms = (end_time - start_time) * 1000
    return response_time_ms, result
