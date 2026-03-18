# Error Handling Improvements

## Problem

Mid-print, the touchscreen displays **"Printer is not ready, Cancelling Print"** and kills the print with an M112 emergency stop — even when the printer is functioning normally. This is a cascading error caused by the software's own recovery actions triggering secondary errors that are misidentified as critical.

## Root Cause Analysis

### The Error Chain

1. A transient Klipper state change (e.g., brief communication hiccup, heater fluctuation) fires `onKlipperStateChanged()` with an unhealthy state.
2. `onKlipperStateChanged()` calls `refresh_klipper_status()`, which sends `FIRMWARE_RESTART` to recover.
3. `FIRMWARE_RESTART` causes Klipper to transiently emit `"Printer is not ready"` and MCU reset errors.
4. These transient errors arrive via WebSocket and hit `showPrinterError()`.
5. `showPrinterError()` sees `"Printer is not ready"` in `CRITICAL_PRINTER_ERRORS` → cancels print + sends M112.
6. M112 causes Klipper to emit `"Shutdown due to M112"`, which is also in `CRITICAL_PRINTER_ERRORS` → re-enters the error handler.

### Additional Issues Found

- **`"probe"` substring match was too broad** — matched any message containing "probe" (e.g., `"probe accuracy results:"`, `"probe at X,Y"`), causing false critical errors.
- **`"Error loading template"` and `"Must home axis first"` are Klipper `CommandError`s**, not shutdowns — Klipper just rejects that one command and continues. They do not require a firmware restart or print cancellation.
- **No re-entrancy guard** — when `showPrinterError` sends M112 + FIRMWARE_RESTART, the resulting secondary Klipper errors re-enter the handler, causing duplicate dialogs and cascading cancellations.

---

## Changes Required

All changes are in **3 locations** across 2 files.

### 1. `config.py` — CRITICAL_PRINTER_ERRORS List

**Remove** these entries (they are Klipper `CommandError`s, not shutdowns — the print can continue):
- `"Error loading template"` — Jinja2 template syntax error in a macro. Klipper skips that command.
- `"Must home axis first"` — Klipper refuses one move command. Print queue continues.
- `"probe"` — Too broad a substring match. Catches innocent status messages.

**Add** these entries:
- `"Probe triggered prior to movement"` — Actual probe error (probe stuck before move started).
- `"PROBING_FAILED"` — Custom macro probing failure signal.
- `"not heating at expected rate"` — Klipper `verify_heater` shutdown. This is a genuine hardware failure where Klipper calls `invoke_shutdown()`.

**Remove** duplicate:
- `"Unable to connect"` appeared twice.

#### Before

```python
CRITICAL_PRINTER_ERRORS = [
    "Can not update MCU",
    "Error loading template",
    "Must home axis first",
    "probe",
    "Error during homing move",
    "still triggered after retract",
    "'mcu' must be specified",
    "Unable to connect",
    "Shutdown due to M112",
    "Printer is not ready",
    "Unable to connect"
]
```

#### After

```python
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
]
```

#### Reference: What each entry means

| Entry | Klipper Behavior | Can Fire Mid-Print? |
|---|---|---|
| `Can not update MCU` | MCU communication failure (shutdown) | Rare, genuine HW failure |
| `Probe triggered prior to movement` | Probe stuck before move (CommandError) | Only during probing |
| `PROBING_FAILED` | Custom macro probe failure signal | Only during probing |
| `Error during homing move` | Endstop/stepper failure (CommandError) | Only during homing |
| `still triggered after retract` | Probe won't release (CommandError) | Only during probing |
| `'mcu' must be specified` | Config error (startup only) | No |
| `Unable to connect` | Serial/USB disconnected | Genuine HW failure |
| `Shutdown due to M112` | Emergency stop (shutdown) | Re-entrancy guard handles self-inflicted |
| `Printer is not ready` | Klipper not in ready state | Guards handle transient cases |
| `not heating at expected rate` | `verify_heater` failure (shutdown) | Yes — genuine critical |

---

### 2. `main_controller.py` — `__init__` — Add Re-entrancy Guard Flag

Add `self._handling_critical_error = False` alongside the existing `_klipper_restart_in_progress` flag.

#### Before

```python
self._klipper_restart_in_progress = False
self._klipper_restart_grace_timer = QtCore.QTimer(self)
```

#### After

```python
self._klipper_restart_in_progress = False
self._handling_critical_error = False  # Re-entrancy guard for showPrinterError
self._klipper_restart_grace_timer = QtCore.QTimer(self)
```

---

### 3. `main_controller.py` — `onKlipperStateChanged()` — Guard During Active Prints

Add a guard that skips automatic `FIRMWARE_RESTART` recovery when the printer is actively printing or paused. Without this, a transient Klipper state change during printing triggers the full error cascade.

#### Add this block after the startup grace period check

```python
# Never trigger automatic FIRMWARE_RESTART recovery during active prints.
# Sending FIRMWARE_RESTART while printing causes Klipper to emit
# "Printer is not ready", which showPrinterError treats as a critical
# error and cancels the print + sends M112.
if self.printer_model.printer_status in ["Printing", "Paused"]:
    self.logger.warning(f"Klipper state '{state}' is unhealthy but printer is {self.printer_model.printer_status} - skipping automatic recovery to avoid disrupting print")
    return
```

---

### 4. `main_controller.py` — `refresh_klipper_status()` — Set Restart Flag

At the start of `refresh_klipper_status()`, set `self._klipper_restart_in_progress = True` so that transient errors from our own `FIRMWARE_RESTART` commands are suppressed. Reset it in the `finally` block.

#### Add at the start of the try block

```python
self._klipper_restart_in_progress = True
```

#### Add in the finally block

```python
finally:
    self.klipper_status_refresh_running = False
    self._klipper_restart_in_progress = False
```

---

### 5. `main_controller.py` — `showPrinterError()` — Full Updated Method

This is the most critical change. The updated method adds:

1. **Re-entrancy guard** (`_handling_critical_error`) — prevents secondary errors from M112/FIRMWARE_RESTART from re-entering and causing duplicate cancellations.
2. **Klipper restart suppression** (`_klipper_restart_in_progress`) — suppresses MCU errors and "Printer is not ready" during intentional restarts.
3. **"Printer is not ready" status awareness** — suppresses this error when the printer is idle (not Starting/Printing/Paused).

#### Full updated method

```python
def showPrinterError(self, msg='Printer error, Check Terminal', overlay=False):
    self.logger.info("MainController.showPrinterError started")
    cleaned_msg = msg.strip()
    while cleaned_msg.startswith('!'):
        cleaned_msg = cleaned_msg[1:].lstrip()
    self.logger.error(f"Printer error received: {msg}")
    self.logger.debug(f"Cleaned message for processing: {cleaned_msg}")

    # Re-entrancy guard: if we are already handling a critical error,
    # suppress secondary errors (e.g., "Shutdown due to M112" from our own M112,
    # or "Printer is not ready" from our own FIRMWARE_RESTART).
    if self._handling_critical_error:
        self.logger.debug(f"Suppressing re-entrant error during critical error handling: {cleaned_msg}")
        return

    # Suppress transient errors during intentional Klipper restarts
    # (FIRMWARE_RESTART causes MCU reset errors and "Printer is not ready" transiently)
    if self._klipper_restart_in_progress:
        if ("Failed automated reset of MCU" in cleaned_msg
                or "MCU" in cleaned_msg
                or "Printer is not ready" in cleaned_msg):
            self.logger.debug(f"Suppressing transient error during Klipper restart: {cleaned_msg}")
            return

    # Check if this is a "Printer is not ready" error and printer is in expected states
    if "Printer is not ready" in cleaned_msg:
        if self.printer_model.printer_status not in ["Starting", "Printing", "Paused"]:
            self.logger.debug(f"Suppressing 'Printer is not ready' error because printer status is '{self.printer_model.printer_status}'")
            return

    for ignore_item in IGNORED_PRINTER_ERRORS:
        if ignore_item in cleaned_msg:
            self.logger.debug(f"Ignoring error message for UI display: {cleaned_msg}")
            return
    if self.octoprint_client:
        try:
            if any(error in cleaned_msg for error in CRITICAL_PRINTER_ERRORS):
                self.logger.error("CRITICAL ERROR SHUTDOWN NEEDED")
                if self.printer_model.printer_status in ["Starting", "Printing", "Paused"]:
                    # Set re-entrancy guard before sending M112/FIRMWARE_RESTART
                    # to suppress the cascade of errors they generate
                    self._handling_critical_error = True
                    try:
                        self.octoprint_client.cancelPrint()
                        self.octoprint_client.gcode(command='M112')
                        try:
                            self.octoprint_client.connectPrinter(port="/tmp/printer", baudrate=115200)
                        except Exception:
                            self.octoprint_client.connectPrinter(port="VIRTUAL", baudrate=115200)
                        self.octoprint_client.gcode(command='FIRMWARE_RESTART')
                        self.octoprint_client.gcode(command='RESTART')
                    finally:
                        self._handling_critical_error = False
                    if not self.filamentTriggerDialogShown:
                        self.filamentTriggerDialogShown = True
                        if dialog.WarningOk(self.main_window, cleaned_msg + ", Cancelling Print.", overlay=overlay):
                            self.filamentTriggerDialogShown = False
                    self.logger.error("CRITICAL ERROR SHUTDOWN DONE")
                else:
                    if not self.filamentTriggerDialogShown:
                        self.filamentTriggerDialogShown = True
                        self.octoprint_client.gcode(command='FIRMWARE_RESTART')
                        self.octoprint_client.gcode(command='RESTART')
                        if dialog.WarningOk(self.main_window, cleaned_msg, overlay=overlay):
                            self.filamentTriggerDialogShown = False
            else:
                if not self.filamentTriggerDialogShown:
                    self.filamentTriggerDialogShown = True
                    if dialog.WarningOk(self.main_window, cleaned_msg, overlay=overlay):
                        self.filamentTriggerDialogShown = False
        except Exception as e:
            self._handling_critical_error = False
            self.logger.error(f"Error in MainController.showPrinterError: {e}")
            dialog.WarningOk(self.main_window, f"Error in MainController.showPrinterError: {e}", overlay=True)
```

---

## Error Flow Diagram (After Fixes)

```
Klipper Error/State Change arrives via WebSocket
        │
        ▼
  ┌─────────────────────────────┐
  │ Is _handling_critical_error? │──YES──▶ SUPPRESS (re-entrancy)
  └─────────────┬───────────────┘
                │ NO
                ▼
  ┌─────────────────────────────────┐
  │ Is _klipper_restart_in_progress │
  │ AND msg is MCU/"not ready"?     │──YES──▶ SUPPRESS (transient)
  └─────────────┬───────────────────┘
                │ NO
                ▼
  ┌─────────────────────────────────┐
  │ Is "Printer is not ready" AND   │
  │ status NOT in Starting/         │
  │ Printing/Paused?                │──YES──▶ SUPPRESS (idle noise)
  └─────────────┬───────────────────┘
                │ NO
                ▼
  ┌──────────────────────────┐
  │ Is msg in IGNORED list?  │──YES──▶ SUPPRESS
  └─────────────┬────────────┘
                │ NO
                ▼
  ┌──────────────────────────────┐
  │ Is msg in CRITICAL list?     │──NO──▶ Show warning dialog only
  └─────────────┬────────────────┘
                │ YES
                ▼
  ┌──────────────────────────────┐
  │ Is printer Printing/Paused/  │──NO──▶ FIRMWARE_RESTART + dialog
  │ Starting?                    │
  └─────────────┬────────────────┘
                │ YES
                ▼
  Set _handling_critical_error = True
  cancelPrint() → M112 → reconnect → FIRMWARE_RESTART
  Set _handling_critical_error = False
  Show "..., Cancelling Print." dialog
```

## Klipper State Change Flow (After Fixes)

```
onKlipperStateChanged(state)
        │
        ▼
  ┌──────────────────────────┐
  │ Startup grace period     │
  │ (< 60 seconds)?         │──YES──▶ IGNORE
  └─────────────┬────────────┘
                │ NO
                ▼
  ┌──────────────────────────────┐
  │ Printer is Printing/Paused?  │──YES──▶ SKIP recovery (log warning)
  └─────────────┬────────────────┘
                │ NO
                ▼
  ┌──────────────────────────────┐
  │ State unhealthy AND refresh  │
  │ not already running?         │──YES──▶ refresh_klipper_status()
  └──────────────────────────────┘         (sets _klipper_restart_in_progress)
```

## Notes for Penrose / Base ControlCenter

- **Penrose** has the same architecture and the same bug. Apply all changes identically.
- **Base ControlCenter** may not have Chamber/Filament/Ring heaters — the `"not heating at expected rate"` entry is still safe to add (it only triggers if Klipper actually emits that message).
- The `"probe"` → specific probe error entries change should be adapted based on each project's probe-related macros. `"PROBING_FAILED"` is a custom macro signal — verify it exists in each project's firmware config.
- The WebSocket fixes (removing `@run_async` from `process()`, reconnection retry) documented in `WEBSOCKET_UI_UPDATE_FIXES.md` are prerequisites — apply those first.
