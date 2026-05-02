# Firmware Update & Critical Error Fixes — Migration Guide

This document describes two related fixes that have been applied to
**VolterraControlCenter** and need to be ported to:

- **PenroseControlCenter** — `https://github.com/FracktalWorks/PenroseControlCenter` (default branch: `production`)
- **ControlCenter** — `https://github.com/FracktalWorks/ControlCenter` (default branch: `production`)

Both repos currently contain the same buggy `perform_firmware_update()` and
(in ControlCenter's case) an incomplete `CRITICAL_PRINTER_ERRORS` list.

---

## Fix 1 — `perform_firmware_update()` in `controller/main_controller.py`

### What was wrong

The original code shared by all three repos looks like this:

```python
def perform_firmware_update(self, current_printer: str, printer_display_name: str):
    """Perform the firmware update by copying files and restarting."""
    try:
        from utils.printer_config_manager import copy_firmware_files, restore_octoprint_configs
        
        self.logger.info(f"Performing firmware update for {current_printer}")
        
        # Copy firmware files (same as restore_print_settings)
        success = copy_firmware_files(current_printer)
        
        # Also restore OctoPrint configurations
        if success:
            self.logger.info("Restoring OctoPrint configurations...")
            octoprint_success = restore_octoprint_configs(current_printer)
            if not octoprint_success:
                self.logger.warning("Failed to restore OctoPrint configs, but Klipper config was successful")
        
        if success:
            self.logger.info("Firmware files updated successfully, executing printer reset commands")
            
            # Reset printer firmware settings (same as restore_print_settings)
            try:
                self.octoprint_client.gcode(command='M502')  # Load factory defaults
                self.octoprint_client.gcode(command='M500')  # Save settings to EEPROM
                self.octoprint_client.gcode(command='FIRMWARE_RESTART')  # Restart Klipper
                ...
```

Three concrete problems:

| # | Bug | Impact |
|---|-----|--------|
| 1 | `M502` is sent | **`M502` is not defined in any `.cfg` file.** Klipper replies `Unknown command: M502`, which propagates to `showPrinterError()` and pops up a misleading error dialog *while the update is succeeding*. |
| 2 | `restore_octoprint_configs()` is called explicitly | `copy_firmware_files()` already calls `restore_octoprint_configs()` internally. The double call wastes time and can race on file overwrites. |
| 3 | `FIRMWARE_RESTART` is sent without setting `_klipper_restart_in_progress = True` | Every other intentional restart in the controller raises this guard so that the transient *"Failed automated reset of MCU"* / *"Printer is not ready"* messages emitted during restart are suppressed. The firmware-update path skipped this, so users see a critical-error dialog on every successful update. |

### What is correct

`M500` **is** defined in `firmware/CORE_GCODE_MACROS.cfg` as:

```cfg
[gcode_macro M500]
gcode:
    SAVE_CONFIG NO_RESTART=1
```

It flushes Klipper's current **in-memory** calibration state (probe `z_offset`,
bed mesh, PID, babystep applied via `Z_OFFSET_APPLY_PROBE`) to the
`SAVE_CONFIG` block of `printer.cfg` *without* triggering a restart.

`copy_firmware_files()` → `update_printer_cfg()` preserves the **file-level**
`SAVE_CONFIG` block when swapping templates, but it cannot know about
calibration values that were adjusted in this session and never written to disk.
Sending `M500` immediately before `FIRMWARE_RESTART` guarantees the latest
in-memory values land in the new file.

### The fix

Replace the entire `perform_firmware_update()` method with:

```python
def perform_firmware_update(self, current_printer: str, printer_display_name: str):
    """Perform the firmware update by copying files and restarting.
    
    NOTE: M502 is intentionally NOT sent here — it has no definition in Klipper
    and would generate a spurious "Unknown command" error dialog.
    M500 (SAVE_CONFIG NO_RESTART=1) IS sent to flush any unsaved in-memory
    calibration state (probe offset, bed mesh, PID values) to disk before restart.
    copy_firmware_files() also preserves the file-level SAVE_CONFIG section,
    so M500 covers any in-memory adjustments not yet written to file.
    """
    try:
        from utils.printer_config_manager import copy_firmware_files
        
        self.logger.info(f"Performing firmware update for {current_printer}")
        
        # Copy firmware files and OctoPrint configs (restore_octoprint_configs is
        # called internally by copy_firmware_files, no need to call it again here)
        success = copy_firmware_files(current_printer)
        
        if success:
            self.logger.info("Firmware files updated successfully, restarting Klipper")
            
            try:
                # Suppress transient MCU reset errors that FIRMWARE_RESTART generates
                self._klipper_restart_in_progress = True
                self.octoprint_client.gcode(command='M500')  # Flush in-memory calibration to disk
                self.octoprint_client.gcode(command='FIRMWARE_RESTART')  # Restart Klipper
                
                self.logger.info("Firmware update completed successfully")
                
                restart_msg = (
                    f"Firmware updated successfully!\n\n"
                    f"'{printer_display_name}' has been updated to the latest version.\n\n"
                    "The printer will restart now for changes to take effect."
                )
                self.restart_printer_system(restart_msg)
                
            except Exception as e:
                self._klipper_restart_in_progress = False
                self.logger.error(f"Error executing firmware reset commands: {e}")
                dialog.WarningOk(
                    self.main_window, 
                    f"Firmware files updated but failed to restart Klipper: {e}\n"
                    "Please manually restart the printer.",
                    overlay=True
                )
                
        else:
            self.logger.error("Failed to update firmware files")
            dialog.WarningOk(
                self.main_window,
                "Failed to update firmware. Please check the logs for details.",
                overlay=True
            )
            
    except Exception as e:
        self.logger.error(f"Error performing firmware update: {e}")
        dialog.WarningOk(self.main_window, f"Error updating firmware: {e}", overlay=True)
```

### Summary of changes

| Change | Reason |
|--------|--------|
| Removed `restore_octoprint_configs` from the import line | Already invoked inside `copy_firmware_files()` |
| Removed the explicit `restore_octoprint_configs()` call block | Same — duplicate work |
| Removed `M502` | Undefined in Klipper macros → spurious error dialog |
| **Kept `M500`** | Defined as `SAVE_CONFIG NO_RESTART=1` — flushes unsaved in-memory calibration before restart |
| Kept `FIRMWARE_RESTART` | This is what actually loads the new config |
| Added `self._klipper_restart_in_progress = True` before sending gcode | Suppresses the "Failed automated reset of MCU" / "Printer is not ready" cascade that `FIRMWARE_RESTART` always emits |
| Added `self._klipper_restart_in_progress = False` in the inner `except` | Releases the guard if sending gcode fails (so subsequent real errors still surface). On the success path the flag is cleared by the system reboot in `restart_printer_system()`. |
| Tightened the docstring | Documents *why* M502 is excluded and *why* M500 is kept |

### Pre-flight check before applying

Before applying this fix to either repo, confirm both of these are true in
the target codebase:

1. `_klipper_restart_in_progress` already exists as an instance attribute on
   `MainController` (look in `__init__` for `self._klipper_restart_in_progress = False`).
   Both Penrose and ControlCenter already have it.
2. `showPrinterError()` already checks `self._klipper_restart_in_progress` and
   suppresses MCU/Printer-not-ready messages when it's `True`. Both repos already
   do this.

If either is missing, port the relevant blocks from VolterraControlCenter's
`main_controller.py` first.

---

## Fix 2 — `CRITICAL_PRINTER_ERRORS` in `config.py`

This affects **ControlCenter only** — PenroseControlCenter is already
up-to-date with all 18 entries.

### What was wrong

ControlCenter's `CRITICAL_PRINTER_ERRORS` list contains 10 entries and is
missing 8 Klipper MCU shutdown messages that are emitted by Klipper's
`invoke_shutdown` / `try_shutdown` paths. When these occur on a printer
running ControlCenter, the dialog never fires and the print continues until
something else fails.

### The fix

Replace the existing list in `octoprint_ControlCenter/config.py` with:

```python
# Critical printer errors that require immediate attention, can cancel the print using mainController.showPrinterError
# NOTE: These are substring matches — be specific to avoid false positives.
CRITICAL_PRINTER_ERRORS = [
    "Can not update MCU", 
    "Probe triggered prior to movement",
    "PROBING_FAILED",
    "Error during homing move", 
    "still triggered after retract", 
    "'mcu' must be specified", 
    "Unable to connect",
    "Shutdown due to M112",
    "Printer is not ready",
    "not heating at expected rate",
    # Klipper MCU firmware shutdown errors (invoke_shutdown / try_shutdown)
    "Timer too close",
    "ADC out of range",
    "Lost communication with MCU",
    "Missed scheduling of next",
    "Rescheduled timer in the past",
    "Stepper too far in past",
    "Move queue overflow",
    "TMC reports error",
]
```

### Reference for the new entries

| Substring | Klipper source | Why it's critical |
|-----------|---------------|-------------------|
| `Timer too close` | `klippy/mcu.py` | MCU scheduling failure — print state is unrecoverable |
| `ADC out of range` | thermistor / ADC drivers | Sensor has shorted/disconnected → thermal runaway risk |
| `Lost communication with MCU` | `klippy/mcu.py` | USB/UART link dropped — host can't drive printer |
| `Missed scheduling of next` | step compress | Host fell behind real-time deadlines |
| `Rescheduled timer in the past` | step compress | Same family — time has already passed |
| `Stepper too far in past` | step compress | Stepper command queued for an expired moment |
| `Move queue overflow` | toolhead | Move pipeline exhausted — kinematic failure |
| `TMC reports error` | TMC drivers | Stepper driver fault (overtemp, short, OL) |

All eight should be treated as `Yes — genuine HW failure` for the dialog flow.

---

## Apply procedure (per repo)

1. Clone (or `git pull`) the target repo and create a branch:
   ```sh
   git checkout production
   git pull
   git checkout -b fix/firmware-update-and-critical-errors
   ```
2. Apply **Fix 1** by replacing `perform_firmware_update` in
   `octoprint_<name>/controller/main_controller.py`.
3. (ControlCenter only) Apply **Fix 2** in `octoprint_ControlCenter/config.py`.
4. Manual smoke test:
   - Bump the `# Version: X.X` header in `firmware/printer.cfg` so the update
     dialog will trigger on next boot.
   - Boot the device, accept the firmware-update prompt.
   - Confirm: success dialog appears, **no** "Unknown command: M502" dialog,
     **no** "Failed automated reset of MCU" dialog, printer reboots cleanly.
5. Open a PR against `production`.

## Verification checklist

- [ ] `M502` no longer appears anywhere in `perform_firmware_update`.
- [ ] `M500` is still sent before `FIRMWARE_RESTART`.
- [ ] `self._klipper_restart_in_progress = True` is set before the gcode calls.
- [ ] `restore_octoprint_configs` is **not** explicitly imported or called from
      `perform_firmware_update`.
- [ ] (ControlCenter) `CRITICAL_PRINTER_ERRORS` has 18 entries.
- [ ] On-device test: a clean firmware update completes with no spurious error
      dialogs.

---

## Cross-reference

- VolterraControlCenter source of truth: 
  - [octoprint_VolterraControlCenter/controller/main_controller.py](../octoprint_VolterraControlCenter/controller/main_controller.py) (method `perform_firmware_update`)
  - [octoprint_VolterraControlCenter/config.py](../octoprint_VolterraControlCenter/config.py) (`CRITICAL_PRINTER_ERRORS`)
  - [octoprint_VolterraControlCenter/firmware/CORE_GCODE_MACROS.cfg](../octoprint_VolterraControlCenter/firmware/CORE_GCODE_MACROS.cfg) (`M500` / `M503` definitions; M502 absent)
- Related background:
  - [Documentation/ERROR_HANDLING_IMPROVEMENTS.md](ERROR_HANDLING_IMPROVEMENTS.md)
  - [Documentation/FIRMWARE_UPDATE_CHECK.md](FIRMWARE_UPDATE_CHECK.md)
  - [Documentation/KLIPPER_RESTART_WAIT_UTILITY.md](KLIPPER_RESTART_WAIT_UTILITY.md)
