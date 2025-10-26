import os
import logging
import base64
import time
from datetime import datetime

from google import genai
import asyncio
from catflap_prey_detector.classification.llm.common import DETECTION_PROMPT 
import aiohttp
from tenacity import retry, stop_after_attempt, retry_if_exception_type
from catflap_prey_detector.hardware.catflap_controller import handle_prey_detection
from catflap_prey_detector.detection.detection_result import DetectionResult
from catflap_prey_detector.detection.config import runtime_config

logger = logging.getLogger(__name__)

LOCATION = os.getenv("LOCATION")
PROJECT_ID = os.getenv("PROJECT_ID")
MODEL = "gemini-2.5-flash-lite" 

client = genai.Client(
    vertexai=True, project=PROJECT_ID, location=LOCATION
)

request_counter = 0


@retry(
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(aiohttp.ClientError),
    reraise=True
)
async def make_request(image_base64: str):
    start_time = time.perf_counter()
    logger.info("Making request to Gemini API")
    response = await client.aio.models.generate_content(
        model=MODEL, 
        contents=[{
            "role": "user",
            "parts": [
                {"text": DETECTION_PROMPT},
                {"inline_data": {"mime_type": "image/jpeg", "data": image_base64}}
            ]
        }],
        config=genai.types.GenerateContentConfig(temperature=0)
    )
    end_time = time.perf_counter()
    logger.info(f"Gemini API response received in {end_time - start_time:.2f}s")
    global request_counter
    request_counter += 1
    logger.info(f"Request counter: {request_counter}")
    return response

async def process_gemini_output(response: genai.types.GenerateContentResponse) -> tuple[bool, bool]:
    """Parse Gemini response text into a tuple of booleans.
    
    Expected format: "True, False" or "False, True" etc.
    Returns: (cat_detected, prey_detected)
    """
    try:
        response_text = response.text.strip()
        logger.info(f"Raw response: {response_text=}")
        
        parts = [part.strip() for part in response_text.split(",")]
        
        if len(parts) != 2:
            raise ValueError(f"Expected 2 boolean values, got {len(parts)}")
        
        cat_detected = parts[0].lower() == "true"
        prey_detected = parts[1].lower() == "true"
        
        logger.info(f"Parsed: cat_detected={cat_detected}, prey_detected={prey_detected}")
        return cat_detected, prey_detected
        
    except Exception as e:
        logger.error(f"Failed to parse Gemini response '{response.text}': {e}")
        return False, False

async def detect_prey(image_bytes: bytes | None) -> DetectionResult:
    """
    Analyze image bytes for cat with prey detection.
    
    Args:
        image_bytes: Raw image data in bytes format (from cv2.imencode)
        
    Returns:
        DetectionResult object with detection status, message, and image data
    """
    try:
        if image_bytes is None:
            return DetectionResult.negative()
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        
        response = await make_request(image_base64)
        cat_detected, prey_detected = await process_gemini_output(response)
        if cat_detected & prey_detected:
            message = "🔒 CAT WITH PREY DETECTED! 🔒"
            lock_status_message = await handle_prey_detection()
            # persist image
            enhanced_message = f"{message}\n{lock_status_message}"
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            os.makedirs(runtime_config.prey_images_dir, exist_ok=True)
            image_path = f"{runtime_config.prey_images_dir}/prey_{timestamp}.jpg"
            with open(image_path, "wb") as img_file:
                img_file.write(image_bytes)
            logger.info(f"Persisted prey image at {image_path}")
            return DetectionResult.positive(enhanced_message, image_bytes)
        else:
            return DetectionResult.negative()
    except Exception as e:
        logger.error(f"Error processing image: {type(e).__name__}: {e}", exc_info=True)
        return DetectionResult.error(f"Error processing image: {type(e).__name__}: {e}", image_bytes)


async def main(image_base64: str):
    response_time_ms, response = await measure_response_time_async(make_request, image_base64)
    print(f"Response time: {response_time_ms:.2f} ms")
    print(response.text)

if __name__ == "__main__":
    from catflap_prey_detector.classification.llm.common import measure_response_time_async, PREY_IMAGE_PATH, load_and_prepare_image
    # image_path = CLEAN_IMAGE_PATH
    # image_path = PREY_IMAGE_PATH
    image_path ="/Users/flo/Documents/projets/catflap-prey-detector/test.jpg"

    img, image_base64 = load_and_prepare_image(image_path, resize=False, target_size=384, show_image=True)
    asyncio.run(main(image_base64))

