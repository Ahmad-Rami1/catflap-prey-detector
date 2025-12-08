import asyncio
import logging
import aiohttp
from datetime import datetime, timedelta
from catflap_prey_detector.detection.config import catflap_config, notification_config

logger = logging.getLogger(__name__)

# Global task reference to track the current auto-lock timer
_auto_lock_task: asyncio.Task | None = None


class CatflapController:
    """Manages catflap locking with async timing and state tracking."""
    
    def __init__(self):
        self.lock_duration_seconds = catflap_config.lock_time
        self.is_locked = False
        self.lock_start_time: datetime | None = None
        self.unlock_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()  # Prevent concurrent lock/unlock operations
        
    async def lock_catflap(self, reason: str = "Prey detected") -> bool:
        """
        Lock the catflap for the specified duration.
        
        Args:
            reason: Reason for locking (for logging)
            
        Returns:
            True if lock was activated, False if already locked
        """
        async with self._lock:
            if self.is_locked:
                remaining_time = self.get_remaining_lock_time()
                logger.info(f"Catflap already locked. {remaining_time:.1f} seconds remaining")
                return False
                
            # Lock the catflap
            try:
                self._perform_lock_operation(reason)
                return True
                
            except Exception as e:
                logger.error(f"Failed to lock catflap: {e}")
                self._reset_lock_state()
                raise
                
    async def unlock_catflap(self, reason: str = "Timer expired") -> bool:
        """
        Unlock the catflap.
        
        Args:
            reason: Reason for unlocking (for logging)
            
        Returns:
            True if unlock was successful, False if not locked
        """
        async with self._lock:
            if not self.is_locked:
                logger.info("Catflap was not locked")
                return False
                
            try:
                self._perform_unlock_operation(reason)
                return True
                
            except Exception as e:
                logger.error(f"Failed to unlock catflap: {e}")
                raise
                
    async def _auto_unlock(self):
        """Internal method to automatically unlock after the timer expires."""
        logger.info("Auto-unlock timer started")
        try:
            await asyncio.sleep(self.lock_duration_seconds)
            logger.info("Auto-unlock timer finished, attempting to unlock")
            await self.unlock_catflap("Automatic timer")
        except asyncio.CancelledError:
            logger.info("Auto-unlock timer cancelled")
        finally:
            logger.info("Auto-unlock task finished")
            
    def _perform_lock_operation(self, reason: str) -> None:
        """Perform the actual lock operation with state management."""
        # GPIO removed - Pi Zero handles locking
        self.is_locked = True
        self.lock_start_time = datetime.now()
        
        # Cancel any existing unlock task
        if self.unlock_task and not self.unlock_task.done():
            self.unlock_task.cancel()
        
        self.unlock_task = asyncio.create_task(self._auto_unlock())
            
        lock_until = self.lock_start_time + timedelta(seconds=self.lock_duration_seconds)
        logger.info(f"🔒 Catflap LOCKED: {reason=}")
        logger.info(f"🕐 Lock duration: {self.lock_duration_seconds=} seconds (until {lock_until.strftime('%H:%M:%S')})")
    
    def _perform_unlock_operation(self, reason: str) -> None:
        """Perform the actual unlock operation with state management."""
        # GPIO removed - Pi Zero handles unlocking

        # Cancel unlock task if it exists
        if self.unlock_task and not self.unlock_task.done():
            self.unlock_task.cancel()
            
        lock_duration = (datetime.now() - self.lock_start_time).total_seconds()
        logger.info(f"🔓 Catflap UNLOCKED: {reason=}")
        logger.info(f"🕐 Was locked for {lock_duration:.1f} seconds")
        
        self.is_locked = False
        self.lock_start_time = None
        self.unlock_task = None
    
    def _reset_lock_state(self) -> None:
        """Reset lock state after a failed operation."""
        self.is_locked = False
        self.lock_start_time = None
            
    def get_remaining_lock_time(self) -> float:
        """Get remaining lock time in minutes. Returns 0 if not locked."""
        if not self.is_locked or not self.lock_start_time:
            return 0.0
            
        elapsed = (datetime.now() - self.lock_start_time).total_seconds()
        remaining = max(0, self.lock_duration_seconds - elapsed)
        return remaining
        
    def get_lock_status(self) -> dict:
        """Get detailed lock status information."""
        return {
            "is_locked": self.is_locked,
            "lock_start_time": self.lock_start_time.isoformat() if self.lock_start_time else None,
            "remaining_seconds": self.get_remaining_lock_time(),
            "lock_duration_seconds": self.lock_duration_seconds
        }


catflap_controller = CatflapController()


async def handle_prey_detection() -> str:
    """
    Handle prey detection by locking cat door indefinitely (RED mode).
    Door will remain locked until manually unlocked.

    Returns:
        Status message for joining with detection message
    """
    try:
        logger.info(f"Locking cat door indefinitely (RED mode)")

        # Send HTTP request to lock cat door to RED (indefinitely)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{notification_config.catdoor_base_url}/mode/red",
                timeout=aiohttp.ClientTimeout(total=5.0)
            ) as response:
                if response.status == 200:
                    logger.info("Cat door locked to RED successfully (indefinitely)")
                    return "🔒 Cat door LOCKED indefinitely - manual unlock required"
                else:
                    logger.warning(f"Cat door API returned status {response.status}")
                    return f"⚠️ Cat door API returned status {response.status}"

    except asyncio.TimeoutError:
        logger.error("Timeout while contacting cat door API")
        return "⚠️ Timeout contacting cat door API"
    except Exception as e:
        logger.error(f"Error locking cat door: {e}")
        return f"⚠️ Could not lock cat door: {str(e)}"


class DetectionPauser:
    """Optional helper to pause detection during catflap lock."""
    
    def __init__(self, controller: CatflapController):
        self.controller = controller
        
    def should_pause_detection(self) -> bool:
        """Returns True if detection should be paused due to catflap lock."""
        return self.controller.is_locked
        
    def get_pause_reason(self) -> str:
        """Get reason for detection pause."""
        if self.controller.is_locked:
            remaining = self.controller.get_remaining_lock_time()
            return f"Detection paused - catflap locked for {remaining:.1f} more seconds"
        return ""


detection_pauser = DetectionPauser(catflap_controller)


async def handle_no_prey_detection() -> str:
    """
    Handle no prey detection by unlocking door (GREEN), then auto-locking to YELLOW after 2 minutes.
    If door is already RED (locked due to prey), leave it locked.

    Returns:
        Status message for joining with detection message
    """
    global _auto_lock_task

    try:
        async with aiohttp.ClientSession() as session:
            # First, check current status
            async with session.get(
                f"{notification_config.catdoor_base_url}/status",
                timeout=aiohttp.ClientTimeout(total=5.0)
            ) as status_response:
                if status_response.status == 200:
                    current_status = await status_response.text()
                    logger.info(f"No prey - current door status: {current_status}")

                    # If already RED (locked due to prey), don't unlock
                    if "RED" in current_status.upper():
                        logger.info("Door is RED (locked) - keeping it locked, not unlocking")
                        return "🔒 Door remains LOCKED (prey detected earlier)"

            # Not RED, so unlock to GREEN
            logger.info(f"No prey detected - unlocking cat door (GREEN)")
            async with session.get(
                f"{notification_config.catdoor_base_url}/mode/green",
                timeout=aiohttp.ClientTimeout(total=5.0)
            ) as response:
                if response.status == 200:
                    logger.info("Cat door unlocked successfully (GREEN)")

                    # Cancel any existing auto-lock timer
                    if _auto_lock_task is not None and not _auto_lock_task.done():
                        logger.info("Cancelling existing auto-lock timer")
                        _auto_lock_task.cancel()

                    # Schedule new auto-lock to YELLOW after 2 minutes
                    _auto_lock_task = asyncio.create_task(_auto_lock_after_delay())
                    return "✅ Cat door unlocked - will auto-lock in 2 minutes"
                else:
                    logger.warning(f"Cat door API returned status {response.status}")
                    return f"⚠️ Cat door API returned status {response.status}"

    except asyncio.TimeoutError:
        logger.error("Timeout while contacting cat door API")
        return "⚠️ Timeout contacting cat door API"
    except Exception as e:
        logger.error(f"Error unlocking cat door: {e}")
        return f"⚠️ Could not unlock cat door: {str(e)}"


async def _auto_lock_after_delay():
    """Auto-lock the cat door after 2 minutes - sets to YELLOW unless already RED."""
    try:
        logger.info("Auto-lock timer started (2 minutes)")
        await asyncio.sleep(120)  # 2 minutes

        logger.info("Auto-lock triggered - checking current status first")

        async with aiohttp.ClientSession() as session:
            # First, get current status
            async with session.get(
                f"{notification_config.catdoor_base_url}/status",
                timeout=aiohttp.ClientTimeout(total=5.0)
            ) as status_response:
                if status_response.status == 200:
                    current_status = await status_response.text()
                    logger.info(f"Current door status: {current_status}")

                    # Check if already RED (locked due to prey detection)
                    if "RED" in current_status.upper():
                        logger.info("Door is already RED (locked) - keeping it RED, not changing to YELLOW")
                        return

                    # Not RED, check reed sensor before setting to YELLOW
                    reed_is_closed = False
                    max_retries = 3
                    for attempt in range(max_retries):
                        async with session.get(
                            f"{notification_config.catdoor_base_url}/reed/status",
                            timeout=aiohttp.ClientTimeout(total=5.0)
                        ) as reed_response:
                            if reed_response.status == 200:
                                reed_data = await reed_response.json()
                                reed_status = reed_data.get("reed_status", "UNKNOWN")
                                logger.info(f"Reed sensor status (attempt {attempt + 1}/{max_retries}): {reed_status}")

                                if reed_status == "CLOSED":
                                    reed_is_closed = True
                                    break
                                else:
                                    # Reed is OPEN (cat in door)
                                    if attempt < max_retries - 1:
                                        logger.info(f"Reed is OPEN - waiting 30 seconds before retry")
                                        await asyncio.sleep(30)  # 30 seconds
                                    else:
                                        logger.info(f"Reed still OPEN after {max_retries} attempts - will unlock briefly then lock")
                            else:
                                logger.warning(f"⚠️ Failed to get reed status - status {reed_response.status}")
                                break

                    # If reed is still open after retries, unlock to GREEN, wait, then lock to YELLOW
                    if not reed_is_closed:
                        logger.info("Reed still OPEN - setting to GREEN to let cat through")
                        async with session.get(
                            f"{notification_config.catdoor_base_url}/mode/green",
                            timeout=aiohttp.ClientTimeout(total=5.0)
                        ) as green_response:
                            if green_response.status == 200:
                                logger.info("Set to GREEN - waiting 5 seconds")
                                await asyncio.sleep(5)
                            else:
                                logger.warning(f"⚠️ Failed to set GREEN - status {green_response.status}")

                    # Set to YELLOW (only out)
                    logger.info("Setting cat door to YELLOW (only out)")
                    async with session.get(
                        f"{notification_config.catdoor_base_url}/mode/yellow",
                        timeout=aiohttp.ClientTimeout(total=5.0)
                    ) as mode_response:
                        if mode_response.status == 200:
                            logger.info("✅ Cat door set to YELLOW successfully")
                        else:
                            logger.warning(f"⚠️ Failed to set YELLOW - status {mode_response.status}")
                else:
                    logger.warning(f"⚠️ Failed to get status - status {status_response.status}")

    except asyncio.CancelledError:
        logger.info("Auto-lock timer cancelled")
    except Exception as e:
        logger.error(f"Error during auto-lock: {e}")
