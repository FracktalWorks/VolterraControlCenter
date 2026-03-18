# WebSocket Fix Documentation - UI Update Reliability

## Overview

This document describes critical fixes applied to the OctoPrint WebSocket client (websocket_client.py) that resolve intermittent UI update failures. These fixes are applicable to all printer TouchUI projects that share this websocket architecture.

## Problems Identified

### Problem 1: Signals Emitted from Ephemeral Threads (@run_async on process())

**Root Cause:** The process() method was decorated with @run_async, which spawns a new 	hreading.Thread (Python standard library) for every single WebSocket message received. Since the OctoPrintWebSocket class is a QThread and emits pyqtSignal objects, emitting signals from ephemeral non-Qt threads causes:

- **Silent signal drops** — Qt signals emitted from threads that are not the QObject's owning thread (or the main thread) may not be delivered reliably via queued connections.
- **Race conditions** — Multiple messages arrive concurrently (temperature polling ~1/s, status updates, print progress), each spawning a thread. Threads execute in arbitrary order and can interleave signal emissions.
- **Thread pile-up** — Under heavy message flow, dozens of short-lived threads accumulate, competing for the GIL and Qt's event loop.

**Symptoms:**
- Temperature display freezes or shows stale values
- Print progress bar stops updating mid-print
- Printer status label doesn't reflect current state
- UI occasionally works fine, then randomly stops updating

**Fix:** Remove the @run_async decorator from process(). The WebSocket's on_message callback already runs on the QThread's thread (via un_forever()). Qt's signal-slot mechanism handles cross-thread delivery automatically — no additional threading is needed.

`python
# BEFORE (broken):
@run_async
def process(self, data):
    ...
    self.temperatures_signal.emit(temperatures)  # emitted from random Thread

# AFTER (fixed):
def process(self, data):
    ...
    self.temperatures_signal.emit(temperatures)  # emitted from QThread, delivered via Qt event loop
`

Also remove the now-unused import:
`python
# Remove this line:
from utils.helpers import run_async
`

---

### Problem 2: Direct self.printer_model Access (AttributeError Swallowed Silently)

**Root Cause:** The process() method references self.printer_model to directly call self.printer_model.pelletSensorState(), but printer_model is never assigned as an attribute on the OctoPrintWebSocket instance. This always raises AttributeError, which is caught by the surrounding except Exception and silently ignored.

**Symptoms:**
- Pellet sensor query responses via WebSocket messages are always silently lost
- Pellet sensor state never updates from QUERY_FILAMENT_SENSOR responses

**Fix:** Use the existing ilament_runout_state_signal to emit pellet sensor state, which is already connected to printer_model.pelletSensorState via the controller's signal wiring.

`python
# BEFORE (broken - self.printer_model doesn't exist on websocket):
if self.printer_model:
    self.printer_model.pelletSensorState(sensor_name, is_detected)

# AFTER (fixed - route through existing signal):
self.filament_runout_state_signal.emit(sensor_name, is_detected)
`

---

### Problem 3: Reconnection Permanently Gives Up After 5 Failures

**Root Cause:** The eestablish_connection() method increments econnect_attempts on each attempt and only resets it to 0 on a successful on_open. If the connection drops more than 5 times (e.g., OctoPrint service restarts, network hiccups, USB disconnections), the WebSocket permanently gives up and never reconnects for the lifetime of the application.

**Symptoms:**
- After multiple OctoPrint restarts or network issues, the UI goes completely dead
- No temperature, status, or print progress updates
- Only recoverable by restarting the entire TouchUI application

**Fix:** Instead of giving up, wait 30 seconds and reset the counter to continue retrying indefinitely.

`python
# BEFORE (broken - gives up permanently):
if self.reconnect_attempts > self.max_reconnect_attempts:
    self.logger.error("Max reconnect attempts reached. Giving up.")
    return

# AFTER (fixed - backs off and retries):
if self.reconnect_attempts > self.max_reconnect_attempts:
    self.logger.warning("Max reconnect attempts reached. Waiting 30s before resetting counter and retrying.")
    time.sleep(30)
    self.reconnect_attempts = 1
    self.logger.info("Reconnect counter reset, retrying...")
`

---

## Files Changed

| File | Change |
|------|--------|
| octoprint_client/websocket_client.py | Remove @run_async from process(), remove unused import, fix self.printer_model direct access to use signal, fix reconnection to retry with backoff |

## How to Apply to Other Printer Projects

1. **Search for @run_async on the process() method** in your websocket client file. If it exists, remove it.
2. **Search for rom utils.helpers import run_async** in the websocket client. If un_async is no longer used anywhere in the file, remove the import.
3. **Search for self.printer_model** references inside the websocket client. Any direct model access should be replaced with signal emissions. The websocket should only communicate with the model through Qt signals.
4. **Check the reconnection logic** — ensure it does not permanently give up. Add a backoff-and-retry pattern instead.
5. **Verify the 	emp() helper function** — if it has a bare except: pass, consider adding logging so temperature parsing failures are visible.

## Architecture Note

The correct data flow is:

`
WebSocket (QThread)  -->  pyqtSignal  -->  PrinterModel (QObject)  -->  pyqtSignal  -->  UI Screens (QWidget)
`

All communication between the WebSocket thread and the UI must go through Qt signals. Never directly call methods on objects owned by a different thread. Qt's signal-slot mechanism with Qt.QueuedConnection (the default for cross-thread connections) ensures thread-safe delivery through the event loop.
