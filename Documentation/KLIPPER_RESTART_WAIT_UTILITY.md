# Klipper Restart Wait Utility - Code Changes Documentation

## Overview

This document describes the implementation of a reusable utility for restarting Klipper and waiting for it to become ready before continuing execution. This prevents transient "Failed automated reset of MCU 'mcu'" error dialogs that appear during intentional Klipper restarts after saving settings.

## Problem Statement 

When saving calibration results or settings that require a Klipper restart:
1. The `RESTART` command is sent to Klipper
2. Klipper temporarily loses MCU communication during restart
3. A transient error "Failed automated reset of MCU 'mcu'" is emitted
4. The UI navigates away immediately without waiting
5. The error gets displayed to the user even though Klipper recovers normally

## Solution

Implement a centralized `restart_klipper_and_wait()` utility in `MainController` that:
1. Sets a "grace period" flag before restarting
2. Sends the `RESTART` command
3. Waits asynchronously for Klipper to become ready
4. Suppresses transient MCU errors during the grace period
5. Calls a completion callback when ready (or timeout occurs)

---

## Code Changes

### File 1: `controller/main_controller.py`

#### Change 1.1: Add New Signal for Restart Completion

**Location:** Class definition, after existing signals

```python
# Define signals
klipper_error_signal = QtCore.pyqtSignal(str)  # Signal to show error dialog from main thread
klipper_restart_complete_signal = QtCore.pyqtSignal(bool, str)  # Signal emitted when Klipper restart completes (success, message)
```

#### Change 1.2: Initialize Grace Period State in `__init__`

**Location:** In `__init__()`, after `self.startup_time = time.time()`

```python
# Klipper restart grace period - suppresses transient MCU errors during intentional restarts
self._klipper_restart_in_progress = False
self._klipper_restart_grace_timer = QtCore.QTimer(self)
self._klipper_restart_grace_timer.setSingleShot(True)
self._klipper_restart_grace_timer.timeout.connect(self._end_klipper_restart_grace_period)
```

#### Change 1.3: Add New Section - Klipper Restart Utilities

**Location:** After `restart_printer_system()` method, before "Print Restore Management" section

```python
# =========================================================================
# SECTION: Klipper Restart Utilities
# =========================================================================

def _end_klipper_restart_grace_period(self):
    """End the Klipper restart grace period (called by timer)."""
    self._klipper_restart_in_progress = False
    self.logger.debug("Klipper restart grace period ended")

def restart_klipper_and_wait(self, on_complete=None, timeout_seconds=30, use_firmware_restart=False):
    """
    Restart Klipper and wait for it to become ready before calling the completion callback.
    
    This method provides a reusable way to restart Klipper after saving settings,
    suppressing transient MCU reset errors during the restart process.
    
    Args:
        on_complete: Optional callback function to call when restart completes.
                    Called with (success: bool, message: str) arguments.
                    If None, the klipper_restart_complete_signal is emitted instead.
        timeout_seconds: Maximum time to wait for Klipper to become ready (default: 30).
        use_firmware_restart: If True, use FIRMWARE_RESTART instead of RESTART (default: False).
    
    Usage:
        # With callback:
        self.main_window.controller.restart_klipper_and_wait(
            on_complete=lambda success, msg: self.on_restart_done(success, msg)
        )
        
        # With signal:
        self.main_window.controller.klipper_restart_complete_signal.connect(self.on_restart_done)
        self.main_window.controller.restart_klipper_and_wait()
    """
    try:
        self.logger.info(f"Starting Klipper restart (firmware_restart={use_firmware_restart}, timeout={timeout_seconds}s)")
        
        # Enable grace period to suppress transient MCU errors
        self._klipper_restart_in_progress = True
        # Set a grace period timer that extends beyond the expected restart time
        grace_period_ms = (timeout_seconds + 10) * 1000
        self._klipper_restart_grace_timer.start(grace_period_ms)
        
        # Send restart command
        restart_command = 'FIRMWARE_RESTART' if use_firmware_restart else 'RESTART'
        self.octoprint_client.gcode(command=restart_command)
        self.logger.info(f"Sent {restart_command} command")
        
        # Start async wait for Klipper ready
        self._wait_for_klipper_ready_async(on_complete, timeout_seconds)
        
    except Exception as e:
        self.logger.error(f"Error initiating Klipper restart: {e}")
        self._klipper_restart_in_progress = False
        self._klipper_restart_grace_timer.stop()
        error_msg = f"Failed to restart Klipper: {e}"
        if on_complete:
            on_complete(False, error_msg)
        else:
            self.klipper_restart_complete_signal.emit(False, error_msg)

@run_async
def _wait_for_klipper_ready_async(self, on_complete, timeout_seconds):
    """
    Background thread that waits for Klipper to become ready after restart.
    
    Args:
        on_complete: Callback function or None to use signal instead.
        timeout_seconds: Maximum wait time.
    """
    deadline = time.time() + timeout_seconds
    ready = False
    final_state = 'unknown'
    
    self.logger.info(f"Waiting up to {timeout_seconds}s for Klipper to become ready...")
    
    # Wait for Klipper state to become 'ready'
    while time.time() < deadline:
        try:
            current_state = getattr(self.printer_model, 'klipper_state', 'unknown')
            final_state = current_state
            
            if current_state.lower() == 'ready':
                ready = True
                self.logger.info(f"Klipper is ready (took ~{timeout_seconds - (deadline - time.time()):.1f}s)")
                break
                
            self.logger.debug(f"Klipper state: {current_state}, waiting...")
            
        except Exception as e:
            self.logger.debug(f"Error checking Klipper state: {e}")
        
        time.sleep(1)
    
    # End grace period
    self._klipper_restart_in_progress = False
    self._klipper_restart_grace_timer.stop()
    
    # Prepare result
    if ready:
        success = True
        message = "Klipper restart completed successfully"
    else:
        success = False
        message = f"Klipper restart timed out (state: {final_state})"
        self.logger.warning(message)
    
    # Notify completion on main thread
    # Use default arguments in lambda to properly capture current values
    if on_complete:
        QtCore.QTimer.singleShot(0, lambda s=success, m=message: on_complete(s, m))
    else:
        QtCore.QTimer.singleShot(0, lambda s=success, m=message: self.klipper_restart_complete_signal.emit(s, m))

def is_klipper_restart_in_progress(self):
    """
    Check if a Klipper restart is currently in progress.
    
    Returns:
        bool: True if restart is in progress (grace period active), False otherwise.
    """
    return self._klipper_restart_in_progress
```

#### Change 1.4: Update `showPrinterError()` to Suppress MCU Errors During Restart

**Location:** In `showPrinterError()`, after cleaning the message and before "Printer is not ready" check

```python
# Suppress transient MCU reset errors during intentional Klipper restarts
if self._klipper_restart_in_progress:
    if "Failed automated reset of MCU" in cleaned_msg or "MCU" in cleaned_msg:
        self.logger.debug(f"Suppressing transient MCU error during Klipper restart: {cleaned_msg}")
        return
```

---

### File 2: `ui/calibrate_screen/ZProbeOffsetWizard/ZProbeOffsetWizard.py`

#### Change 2.1: Update `apply_and_finish()` Method

**Replace the direct `RESTART` call with `restart_klipper_and_wait()`:**

```python
def apply_and_finish(self):
    """
    Apply the calculated probe offset and finish calibration.
    
    Applies M851 command with calculated offset and returns to main screen.
    Uses restart_klipper_and_wait() to properly wait for Klipper to be ready
    before navigating away, preventing transient MCU error dialogs.
    """
    try:
        # ... existing validation and offset application code ...
        
        # Apply the probe offset using M851
        offset_command = f"M851 Z{self.calculated_offset:.6f}"
        self.octoprint_client.gcode(command=offset_command)
        
        # Save to EEPROM
        self.octoprint_client.gcode(command='M500')
        
        # Clean up wizard state
        self.cleanup()
        
        # Turn off heating and home
        self.octoprint_client.gcode("M104 T0 S0")
        self.octoprint_client.home(['x', 'y', 'z'])
        
        # Use restart_klipper_and_wait instead of direct RESTART
        self.main_window.controller.restart_klipper_and_wait(
            on_complete=self._on_klipper_restart_complete,
            timeout_seconds=30
        )
        
    except Exception as e:
        self.logger.error(f"Error applying probe offset: {e}")
        self._show_error("Application Error", str(e))

def _on_klipper_restart_complete(self, success, message):
    """
    Callback when Klipper restart completes after applying probe offset.
    
    Args:
        success: True if Klipper restarted successfully
        message: Status message from the restart process
    """
    try:
        if success:
            self.logger.info(f"Klipper restart completed: {message}")
        else:
            self.logger.warning(f"Klipper restart issue: {message}")
        
        # Return to main calibration screen
        if hasattr(self.main_window, 'calibrate_screen'):
            self.main_window.calibrate_screen.show_calibrate_screen()
            
    except Exception as e:
        self.logger.error(f"Error in _on_klipper_restart_complete: {e}")
```

---

### File 3: `ui/calibrate_screen/nozzleOffsetPage/nozzleOffsetPage.py`

#### Change 3.1: Update `setZProbeOffset()` Method

**Replace the direct `RESTART` call with `restart_klipper_and_wait()`:**

```python
def setZProbeOffset(self, offset):
    """Sets Z Probe offset from spinbox and updates UI accordingly.
    
    Uses restart_klipper_and_wait() to properly restart Klipper after saving,
    preventing transient MCU error dialogs.
    """
    try:
        rounded_offset = round(float(offset), 2)
        logger.info(f"Setting Z Probe Offset to: {rounded_offset} mm")

        # Send G-code commands
        self.octoprint_client.gcode(command=f'M851 Z{rounded_offset}')
        self.octoprint_client.gcode(command='M500')
        
        # Use restart_klipper_and_wait instead of direct RESTART
        self.main_window.controller.restart_klipper_and_wait(
            on_complete=self._on_klipper_restart_complete,
            timeout_seconds=30
        )
        
    except Exception as e:
        logger.error("Error in NozzleOffsetPage.setZProbeOffset: {}".format(e))
        dialog.WarningOk(self, "Error: {}".format(e), overlay=True)

def _on_klipper_restart_complete(self, success, message):
    """
    Callback when Klipper restart completes after setting probe offset.
    """
    try:
        if success:
            logger.info(f"Klipper restart completed: {message}")
        else:
            logger.warning(f"Klipper restart issue: {message}")
    except Exception as e:
        logger.error(f"Error in _on_klipper_restart_complete: {e}")
```

---

## Usage Pattern for Other Files

When you need to restart Klipper after saving settings, use this pattern:

### Pattern 1: With Callback (Recommended)

```python
def save_settings(self):
    # Save your settings
    self.octoprint_client.gcode(command='YOUR_SETTING_COMMAND')
    self.octoprint_client.gcode(command='M500')
    
    # Restart and wait with callback
    self.main_window.controller.restart_klipper_and_wait(
        on_complete=self._on_restart_complete,
        timeout_seconds=30
    )

def _on_restart_complete(self, success, message):
    if success:
        self.logger.info("Settings saved and Klipper restarted successfully")
        # Navigate to next screen or show success message
    else:
        self.logger.warning(f"Klipper restart issue: {message}")
        # Still navigate, but log the issue
```

### Pattern 2: With Signal

```python
def __init__(self, main_window):
    # ... existing init code ...
    
    # Connect to restart complete signal
    self.main_window.controller.klipper_restart_complete_signal.connect(
        self._on_restart_complete
    )

def save_settings(self):
    # Save your settings and restart
    self.octoprint_client.gcode(command='M500')
    self.main_window.controller.restart_klipper_and_wait()  # Uses signal

def _on_restart_complete(self, success, message):
    # Handle completion
    pass
```

### Pattern 3: Use FIRMWARE_RESTART Instead

```python
self.main_window.controller.restart_klipper_and_wait(
    on_complete=self._on_restart_complete,
    timeout_seconds=30,
    use_firmware_restart=True  # Uses FIRMWARE_RESTART instead of RESTART
)
```

---

## Files That May Need Similar Updates

The following files also use `RESTART` or `FIRMWARE_RESTART` and may benefit from this utility:

1. **`settings_screen/settings_screen.py`** - Uses `FIRMWARE_RESTART` when restoring print settings
2. **`controller/main_controller.py`** - Uses `FIRMWARE_RESTART` in `refresh_klipper_status()` and error recovery
3. **Other calibration wizards** - Any wizard that saves settings to EEPROM and restarts

---

## Testing Checklist

- [ ] Save Z probe offset in ZProbeOffsetWizard - verify no MCU error dialog
- [ ] Save Z probe offset in NozzleOffsetPage - verify no MCU error dialog
- [ ] Verify Klipper becomes ready after restart
- [ ] Verify timeout handling if Klipper fails to restart
- [ ] Verify UI navigates correctly after restart completes
- [ ] Test with actual hardware to confirm MCU communication recovers

---

## Summary

| File | Change | Purpose |
|------|--------|---------|
| `main_controller.py` | Added `restart_klipper_and_wait()` | Reusable restart utility |
| `main_controller.py` | Added grace period suppression | Prevents MCU error dialogs |
| `ZProbeOffsetWizard.py` | Updated `apply_and_finish()` | Uses new utility |
| `nozzleOffsetPage.py` | Updated `setZProbeOffset()` | Uses new utility |
