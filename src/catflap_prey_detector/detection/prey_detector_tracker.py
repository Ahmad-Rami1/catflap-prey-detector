from __future__ import annotations
from typing import Literal
import asyncio
import logging
import cv2
import numpy as np
import os
import uuid
import json
import urllib.request
from datetime import datetime, timedelta
from catflap_prey_detector.classification.prey_detector_api.async_utils import async_consumer_with_task_group_and_result_processor, async_consumer_queue
from catflap_prey_detector.classification.prey_detector_api.detector import detect_prey
from catflap_prey_detector.notifications.telegram_bot import notify_event_async
from catflap_prey_detector.hardware.catflap_controller import detection_pauser
from catflap_prey_detector.detection.config import prey_detector_tracker_config, runtime_config, notification_config
from catflap_prey_detector.detection.detection_result import DetectionResult
from skimage.metrics import structural_similarity as ssim

logger = logging.getLogger(__name__)

# Track how many consecutive negative-only result batches we've seen.
# A "batch" here is one full consumer run in async_consumer_with_task_group_and_result_processor.
CONSECUTIVE_NEGATIVE_ONLY_BATCHES = 0

# Track which trigger positions ("left", "middle", "right") have contributed
# images to the current detection episode (since last flap or unlock).
EPISODE_TRIGGER_POSITIONS: set[str] = set()

# Fallback: last image bytes enqueued for prey detection, used if the
# current batch has no image_bytes attached (e.g. due to overflow tasks).
LAST_ENQUEUED_IMAGE_BYTES: bytes | None = None

# First image bytes where the trigger position was "middle" in the current
# episode. This is what we'll try to use for the unlock notification photo.
FIRST_MIDDLE_IMAGE_BYTES: bytes | None = None


def should_skip_detection_recent_exit() -> bool:
    """
    Check if cat just exited through flap (within last 3 minutes).
    Returns True if should skip detection (recent exit), False if should proceed.
    """
    logger.info("🔍 Checking if should skip detection due to recent exit...")
    try:
        reed_log_url = f"{notification_config.catdoor_base_url}/logs/reed/last"
        logger.debug(f"Fetching reed log from: {reed_log_url}")

        with urllib.request.urlopen(reed_log_url, timeout=2) as response:
            data = json.loads(response.read().decode())
            logger.debug(f"Reed log response: {data}")

            timestamp_str = data.get("timestamp")
            if not timestamp_str:
                logger.warning("⚠️ No timestamp in reed log response - proceeding with detection")
                return False

            # Parse timestamp format: "2025-12-07 13:04:08"
            last_flap_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            time_since_last_flap = datetime.now() - last_flap_time

            logger.info(f"⏱️ Time since last flap: {time_since_last_flap.total_seconds():.1f}s")

            if time_since_last_flap < timedelta(seconds=180):
                logger.info(f"🚫 SKIPPING prey detection - cat just exited {time_since_last_flap.total_seconds():.1f}s ago")
                return True
            else:
                logger.info(f"✅ PROCEEDING with detection - last flap was {time_since_last_flap.total_seconds():.1f}s ago")
                return False

    except Exception as e:
        logger.error(f"❌ Failed to check reed log: {type(e).__name__}: {e} - proceeding with detection")
        return False


def crop_image(image: np.ndarray, position: str, crop_width: int) -> np.ndarray:
    """Crop image horizontally based on position.
    
    Args:
        image: Input image array
        position: Crop position - "left", "right", or "middle"
        crop_width: Width of the cropped image
        
    Returns:
        Cropped image array
    """
    height, width = image.shape[:2]
    
    if position == "left":
        start_x = 0
    elif position == "right":
        start_x = max(0, width - crop_width)
    else:  # middle
        start_x = max(0, (width - crop_width) // 2)
    
    end_x = min(width, start_x + crop_width)
    start_x = max(0, end_x - crop_width)
    
    return image[:, start_x:end_x]


async def process_detection_results(results: list[DetectionResult]) -> None:
    """
    Process a list of detection results and trigger notifications / door control.

    We now require multiple *full* negative-only batches before unlocking:
    - Any positive result (prey detected) triggers a notification and resets the
      negative-batch counter (door is locked via handle_prey_detection elsewhere).
    - A negative-only batch with at least MIN_RESULTS_PER_BATCH results increments
      the consecutive negative-only batch counter.
    - Only after REQUIRED_NEGATIVE_ONLY_BATCHES such batches do we call
      handle_no_prey_detection() to unlock.

    Args:
        results: List of DetectionResult objects from detect_prey coroutine calls
    """
    from catflap_prey_detector.hardware.catflap_controller import handle_no_prey_detection
    global CONSECUTIVE_NEGATIVE_ONLY_BATCHES, EPISODE_TRIGGER_POSITIONS, FIRST_MIDDLE_IMAGE_BYTES

    logger.info(f"Processing {len(results)=} detection results")

    first_positive_result = _get_positive_results(results)

    # Any positive result: send notification and reset counter
    if first_positive_result is not None:
        logger.info(
            "Positive prey detection found in batch - resetting consecutive "
            "negative-only batch counter"
        )
        CONSECUTIVE_NEGATIVE_ONLY_BATCHES = 0
        EPISODE_TRIGGER_POSITIONS.clear()
        FIRST_MIDDLE_IMAGE_BYTES = None
        await _send_notification(first_positive_result)
        return

    # No positive detections in this batch
    negative_count = len(results)
    MIN_RESULTS_PER_BATCH = 1
    REQUIRED_NEGATIVE_ONLY_BATCHES = 2

    if negative_count < MIN_RESULTS_PER_BATCH:
        logger.info(
            "No positive detections found but no valid results in batch - "
            "not counting this batch towards unlock; keeping current door state"
        )
        return

    # Count this as a full negative-only batch (at least one negative result)
    CONSECUTIVE_NEGATIVE_ONLY_BATCHES += 1
    logger.info(
        "No positive detections found in batch - "
        f"consecutive_negative_only_batches={CONSECUTIVE_NEGATIVE_ONLY_BATCHES}/"
        f"{REQUIRED_NEGATIVE_ONLY_BATCHES}"
    )

    if CONSECUTIVE_NEGATIVE_ONLY_BATCHES >= REQUIRED_NEGATIVE_ONLY_BATCHES:
        # Require that we've seen at least two different trigger positions
        # in this detection episode before unlocking.
        if len(EPISODE_TRIGGER_POSITIONS) < 2:
            logger.info(
                "Reached required negative-only batches but only saw positions "
                f"{EPISODE_TRIGGER_POSITIONS} (<2 distinct); keeping door state"
            )
            return

        logger.info(
            "Reached required number of consecutive negative-only batches with "
            f"positions {EPISODE_TRIGGER_POSITIONS} - unlocking door"
        )
        CONSECUTIVE_NEGATIVE_ONLY_BATCHES = 0

        # No prey detected across multiple batches - unlock door ONCE
        # and send a Telegram notification with the earliest available negative image.
        first_with_image = next(
            (r for r in results if r.image_bytes is not None),
            None,
        )
        unlock_message = await handle_no_prey_detection()
        if unlock_message:
            from catflap_prey_detector.notifications.telegram_bot import notify_event_async
            # Prefer the first image from the current batch; if none are available
            # (e.g., all tasks were overflow/error results), fall back to the
            # last image we enqueued for prey detection.
            global LAST_ENQUEUED_IMAGE_BYTES
            if first_with_image is not None and first_with_image.image_bytes is not None:
                image_bytes = first_with_image.image_bytes
            else:
                image_bytes = LAST_ENQUEUED_IMAGE_BYTES
            positions_str = ", ".join(sorted(EPISODE_TRIGGER_POSITIONS)) or "unknown"
            caption = f"{unlock_message}\nPositions in this episode: {positions_str}"

            # Optionally overlay positions onto the image itself for easier visual debugging
            if image_bytes is not None:
                try:
                    nparr = np.frombuffer(image_bytes, np.uint8)
                    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                    if img is not None:
                        overlay_text = f"Positions: {positions_str}"
                        cv2.putText(
                            img,
                            overlay_text,
                            (10, 25),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.7,
                            (0, 255, 0),
                            2,
                            cv2.LINE_AA,
                        )
                        _, buf = cv2.imencode('.jpg', img)
                        image_bytes = buf.tobytes()
                except Exception as e:
                    logger.error(f"Failed to overlay positions on unlock image: {e}")

            await notify_event_async(caption, image_bytes)

        # Reset episode positions after a completed unlock decision
        EPISODE_TRIGGER_POSITIONS.clear()
    else:
        logger.info("Keeping current door state (waiting for more negative-only batches)")


def _get_positive_results(results: list[DetectionResult]) -> DetectionResult | None:
    """Filter out negative results, keeping only positive detections."""
    return next((result for result in results if result.is_positive), None)


async def _send_notification(result: DetectionResult) -> None:
    """Send notification for a single detection result."""
    message, image_bytes = result.message, result.image_bytes
    await notify_event_async(message, image_bytes)


class PreyDetectorTracker:
    """Tracks object detections over time to spawn prey detection analysis tasks"""
    
    def __init__(self, prey_detection_enabled: bool = True) :
        self.time_window = prey_detector_tracker_config.reset_time_window
        self.thread_timeout = prey_detector_tracker_config.reset_time_window + 1
        self.concurrency = prey_detector_tracker_config.concurrency
        self.detector_task: asyncio.Future | None = None
        self.prey_detection_enabled = prey_detection_enabled
        self.previous_image: np.ndarray | None = None
        self.ssim_threshold = prey_detector_tracker_config.ssim_threshold
        self.save_images = prey_detector_tracker_config.save_images
        self.uuid = uuid.uuid4()
        self._next_id = 0
        self.allowed_trigger_positions = set(prey_detector_tracker_config.allowed_trigger_positions)
        self.require_middle_after_right = prey_detector_tracker_config.require_middle_after_right
        self.last_trigger_position: Literal["left", "middle", "right"] | None = None
        
        logger.debug(f"PreyDetectorTracker {self.uuid=}")
    
    def update(self, trigger_object_position: Literal["left", "middle", "right"] | None, image_array: np.ndarray) -> None:
        # Track previous trigger position to infer simple left/right movement
        prev_position: Literal["left", "middle", "right"] | None = self.last_trigger_position
        if trigger_object_position is not None:
            self.last_trigger_position = trigger_object_position

        if not self.prey_detection_enabled:
            logger.info("Prey detection is disabled")
            return

        # Skip detection if cat just exited (within last 3 minutes)
        if trigger_object_position and should_skip_detection_recent_exit():
            # Reset negative batch counter and episode tracking on a fresh flap event
            global CONSECUTIVE_NEGATIVE_ONLY_BATCHES, EPISODE_TRIGGER_POSITIONS, FIRST_MIDDLE_IMAGE_BYTES
            CONSECUTIVE_NEGATIVE_ONLY_BATCHES = 0
            EPISODE_TRIGGER_POSITIONS.clear()
            FIRST_MIDDLE_IMAGE_BYTES = None
            # Also reset orientation state on a new flap event
            self.last_trigger_position = None
            return

        # Optionally restrict which side of the frame can trigger prey detection
        if (
            trigger_object_position
            and self.allowed_trigger_positions
            and trigger_object_position not in self.allowed_trigger_positions
        ):
            logger.info(
                f"Skipping prey detection for trigger position {trigger_object_position!r} "
                f"(allowed={self.allowed_trigger_positions})"
            )
            return

        if trigger_object_position:
            from catflap_prey_detector.main import MAIN_LOOP
            if MAIN_LOOP is None:
                logger.error("MAIN_LOOP is not initialized. Cannot schedule prey detection analysis task.")
            else:
                if self.detector_task is None or self.detector_task.done():
                    self.detector_task = asyncio.run_coroutine_threadsafe(
                        async_consumer_with_task_group_and_result_processor(
                            detect_prey, process_detection_results, self.thread_timeout, self.concurrency
                        ),
                        MAIN_LOOP
                    )
                    logger.info("Scheduled new prey detection analysis task on main asyncio loop")

            try:
                skip_image = (self.previous_image is not None) and (ssim(self.previous_image, image_array, data_range=image_array.max() - image_array.min(), channel_axis = 2) > self.ssim_threshold)
                if skip_image:
                    logger.info("Skipping image based on ssim")
                    return
                self.previous_image = image_array

                if prey_detector_tracker_config.image_size:
                    height, width = image_array.shape[:2]
                    crop_width = prey_detector_tracker_config.image_size[0]
                    
                    cropped_frame = crop_image(image_array, trigger_object_position, crop_width)
                    logger.debug(f"Image cropped from {width}x{height} to {cropped_frame.shape[1]}x{cropped_frame.shape[0]} (position: {trigger_object_position})")
                    
                    if prey_detector_tracker_config.image_size[1] < height:
                        raise NotImplementedError(f"Image height {height} is greater than the target height {prey_detector_tracker_config.image_size[1]}")
                else:
                    cropped_frame = image_array
                _, buffer = cv2.imencode('.jpg', cropped_frame)
                image_bytes = buffer.tobytes()

                # Track which trigger positions contributed frames this episode
                if trigger_object_position is not None:
                    EPISODE_TRIGGER_POSITIONS.add(trigger_object_position)

                # Remember the last image we enqueued, for use as a fallback
                # when the result batch has no image_bytes attached.
                global LAST_ENQUEUED_IMAGE_BYTES, FIRST_MIDDLE_IMAGE_BYTES
                LAST_ENQUEUED_IMAGE_BYTES = image_bytes

                # Capture the first image where the trigger position is "middle"
                # for use in the unlock notification photo.
                if trigger_object_position == "middle" and FIRST_MIDDLE_IMAGE_BYTES is None:
                    FIRST_MIDDLE_IMAGE_BYTES = image_bytes

                # Orientation debug: send only middle frames that follow a right-side trigger,
                # and draw the previous/current trigger positions onto the debug image.
                if (
                    self.require_middle_after_right
                    and trigger_object_position == "middle"
                    and prev_position == "right"
                ):
                    try:
                        debug_image = cropped_frame.copy()
                        debug_text = f"{prev_position or 'None'}→{trigger_object_position}"
                        cv2.putText(
                            debug_image,
                            debug_text,
                            (10, 25),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.7,
                            (255, 0, 0),
                            2,
                            cv2.LINE_AA,
                        )
                        _, dbg_buf = cv2.imencode('.jpg', debug_image)
                        debug_bytes = dbg_buf.tobytes()

                        from catflap_prey_detector.main import MAIN_LOOP
                        if MAIN_LOOP is not None:
                            asyncio.run_coroutine_threadsafe(
                                notify_event_async(
                                    "🔍 Orientation debug: right→middle frame",
                                    debug_bytes,
                                ),
                                MAIN_LOOP,
                            )
                            logger.info(
                                "Sent orientation debug frame (prev_position='right', current='middle')"
                            )
                        else:
                            logger.error("MAIN_LOOP is not initialized. Cannot send orientation debug notification.")
                    except Exception as e:
                        logger.error(f"Failed to send orientation debug notification: {e}")

                # Always enqueue image for prey detection as before
                async_consumer_queue.sync_q.put(image_bytes)
                logger.info(f"Image added to prey detection analysis queue {len(image_bytes)=}")
                
                if self.save_images:
                    self._save_detector_image(cropped_frame, datetime.now())
            except Exception as e:
                logger.error(f"Failed to process and queue image for prey detection analysis: {e}")
    
    def _save_detector_image(self, image_array: np.ndarray, timestamp: datetime) -> None:
        """Save the image sent to prey detector for analysis."""
        directory = f"{runtime_config.prey_detector_images_dir}/{self.uuid}"
        os.makedirs(directory, exist_ok=True)
        
        image_id = self._next_id
        self._next_id += 1
        
        filename = f"{directory}/{timestamp.strftime('%Y-%m-%d_%H-%M-%S-%f')[:-3]}_id{image_id}.jpg"
        _, buffer = cv2.imencode('.jpg', image_array)
        with open(filename, 'wb') as f:
            f.write(buffer.tobytes())
        logger.debug(f"Saved prey detector analysis image: {filename=}")


class PausablePreyDetectorTracker(PreyDetectorTracker):
    """Tracks object detections over time to spawn prey detection analysis tasks and pauses during lock"""
    def __init__(self, prey_detection_enabled: bool = True, pause_during_lock: bool = True) :
        self.pause_during_lock = pause_during_lock
        super().__init__(prey_detection_enabled)

    def update(self, trigger_object_position: Literal["left", "middle", "right"] | None, image_array: np.ndarray) -> None:
        # Skip all prey detection analysis during lock
        if self.pause_during_lock and detection_pauser.should_pause_detection():
            if trigger_object_position:
                pause_reason = detection_pauser.get_pause_reason()
                logger.info(f"🔒 Prey detection tracker paused: {pause_reason=}")
            return  
        return super().update(trigger_object_position, image_array)