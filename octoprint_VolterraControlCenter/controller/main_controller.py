"""Main controller module for OctoPrint Control Center.

This module contains the primary application controller that manages
OctoPrint connections, websocket communications, error handling, and
application startup/shutdown procedures.
"""

"""
To Implement Multiple Material Bays:"
Load each active material bay using update_tool_bay_state
and also set this up in Klipper Variables.
Depending on the state of the KLipper Variables, SYNC_EXTRUDER_MOTION
is set where ever applicable (homing overide, mirror/duplication modes etc)
"""
from ui.main_window import MainWindow
from utils.logger import get_logger
from models.printer_model import PrinterModel
from octoprint_client.websocket_client import OctoPrintWebSocket
from utils.printer_config_manager import get_printer_config_manager
from octoprint_client import octoprint_singleton
from PyQt5 import QtCore
import time
import subprocess
import os
from utils.helpers import run_async
from utils import dialog
from ui.loading_screen.loading_screen import LoadingScreen
from config import ip, apiKey, CRITICAL_PRINTER_ERRORS, IGNORED_PRINTER_ERRORS


logger = get_logger(__name__)

class ThreadConnectionCheck(QtCore.QThread):
    """Check if OctoPrint is online and responding.
    
    This thread runs during startup to ensure connectivity before enabling UI features.
    """
    # Define signals for connection status and progress
    loaded_signal = QtCore.pyqtSignal()
    startup_error_signal = QtCore.pyqtSignal()
    progress_signal = QtCore.pyqtSignal(int, str)  # Progress percentage and message
    virtual_fallback_signal = QtCore.pyqtSignal(str)  # Emitted when falling back to virtual printer

    def __init__(self, ip=None, api_key=None, virtual=False):
        """Initialize the connection check thread.
        
        Args:
            ip: IP address of the OctoPrint server.
            api_key: API key for authentication.
            virtual: Whether to use virtual mode.
        """
        super(ThreadConnectionCheck, self).__init__()
        self.ip = ip
        self.api_key = api_key
        self.MKSPort = None
        self.virtual = virtual
        self.shutdown_flag = False
        self.logger = get_logger(self.__class__.__name__)
        self.logger.info("Initialized ThreadConnectionCheck with IP: {}, API Key: {}, Virtual Mode: {}".format(
            self.ip, self.api_key, self.virtual))

    def run(self):
        """Run the connectivity check to verify OctoPrint is accessible.
        
        Attempts to connect to OctoPrint with a 60-second timeout. If connection
        fails, emits startup_error_signal. On success, emits loaded_signal.
        """        
        self.shutdown_flag = False
        uptime = 0
        
        self.logger.info("Running OctoPrint connectivity check")
        self.progress_signal.emit(10, "Starting OctoPrint connection check...")
        
        # Keep trying until OctoPrint connects or timeout
        while True:
            try:
                # If we've been trying for more than 60 seconds, give up
                if uptime > 60:
                    self.shutdown_flag = True
                    self.logger.error("OctoPrint connection timeout after 60 seconds")
                    self.progress_signal.emit(0, "Connection timeout - Please check OctoPrint service")
                    self.startup_error_signal.emit()
                    break
                
                # Update progress based on time elapsed
                progress = min(20 + (uptime * 40 / 60), 60)  # Progress from 20% to 60% over 60 seconds
                self.progress_signal.emit(int(progress), f"Connecting hardware, attempt {uptime + 1}/60")
                # Attempt to connect to OctoPrint
                octoprint_singleton.initialize(self.ip, self.api_key)
                
                # If we're not in virtual mode, try to connect to the printer
                if not self.virtual:
                    try:
                        self.progress_signal.emit(75, "Connecting to Klipper ...")
                        # First try to connect to the Klipper printer
                        octoprint_singleton.get_client().connectPrinter(port="/tmp/printer", baudrate=115200)
                        self.logger.info("Connected to Klipper printer on /tmp/printer")
                        self.progress_signal.emit(85, "Connected to Klipper printer")
                    except Exception as e:
                        # If that fails, try to connect in virtual mode
                        self.logger.warning(f"Failed to connect to Klipper printer: {e}")
                        self.progress_signal.emit(80, "Falling back to virtual printer...")
                        # Attempt to connect in virtual mode
                        try:
                            octoprint_singleton.get_client().connectPrinter(port="VIRTUAL", baudrate=115200)
                            self.logger.info("Connected to printer in VIRTUAL mode")
                            self.progress_signal.emit(85, "Connected to virtual printer")
                            # Notify UI thread to show fallback dialog
                            self.virtual_fallback_signal.emit("There was an issue conencting to Klipper, conencted to Virtual Printer instead for diagnosis")
                        except Exception as e:
                            self.logger.error(f"Failed to connect to printer in VIRTUAL mode: {e}")
                            self.progress_signal.emit(85, "Printer connection failed - continuing...")

                # If we got here, connection was successful
                self.progress_signal.emit(90, "OctoPrint connection successful")
                break
                
            except Exception as e:
                # Wait 1 second before trying again
                time.sleep(1)
                uptime += 1
                self.logger.warning(f"OctoPrint connection attempt failed: {e}")
                
        # If we didn't set the shutdown flag, we were successful
        if not self.shutdown_flag:
            self.logger.info("OctoPrint connectivity check successful")
            self.progress_signal.emit(95, "Connection check completed")
            self.loaded_signal.emit()

class MainController(QtCore.QObject):

    """Main controller for the OctoPrint Control Center application.
    
    Manages the connection to the OctoPrint server and handles startup
    initialization, error recovery, and websocket communications.
    """

    # Define signals
    klipper_error_signal = QtCore.pyqtSignal(str)  # Signal to show error dialog from main thread
    klipper_restart_complete_signal = QtCore.pyqtSignal(bool, str)  # Signal emitted when Klipper restart completes (success, message)

    # =========================================================================
    # SECTION: Initialization / Startup Lifecycle
    # =========================================================================

    def __init__(self):
        """Initialize controller state, model and main window."""
        super(MainController, self).__init__()
        self.logger = get_logger(__name__)
        self.logger.info("Initializing MainController")
        self.filamentTriggerDialogShown = False
        self.startup_completed = False  # Track if startup initialization has been completed
        self.printer_model = PrinterModel()
        self.octoprint_client = None
        self.main_window = MainWindow(controller=self, printer_model=self.printer_model)
        self.klipper_status_refresh_running = False  # Track if STATUS refresh is already running
        self.startup_time = time.time()  # Track when the controller was initialized
        
        # Klipper restart grace period - suppresses transient MCU errors during intentional restarts
        self._klipper_restart_in_progress = False
        self._handling_critical_error = False  # Re-entrancy guard for showPrinterError
        self._klipper_restart_grace_timer = QtCore.QTimer(self)
        self._klipper_restart_grace_timer.setSingleShot(True)
        self._klipper_restart_grace_timer.timeout.connect(self._end_klipper_restart_grace_period)
        
        # Connect signal to slot for showing Klipper error dialog in main thread
        self.klipper_error_signal.connect(self.showKlipperErrorDialog)

    def start(self):
        """Kick off application startup and async connectivity check."""
        self.logger.info("Starting application")
        try:
            self.updateLoadingProgress(5, "Initializing application...")
            self.connection_check = ThreadConnectionCheck(ip=ip, api_key=apiKey, virtual=False)
            self.connection_check.loaded_signal.connect(self.handleStartupSuccess)
            self.connection_check.startup_error_signal.connect(self.handleStartupError)
            self.connection_check.progress_signal.connect(self.updateLoadingProgress)
            self.connection_check.virtual_fallback_signal.connect(self.handleVirtualFallback)
            self.connection_check.start()
        except Exception as e:
            self.logger.error(f"Error during startup: {e}")
            dialog.WarningOk(self.main_window, f"Error during startup: {e}", overlay=True)
            self.main_window.close()

    def updateLoadingProgress(self, progress, message):
        """Proxy progress updates to loading screen."""
        self.main_window.loading_screen.update_progress(progress, message)

    def handleStartupSuccess(self):
        """Complete startup after connectivity check passes."""
        self.logger.info("OctoPrint connection successful")
        try:
            self.updateLoadingProgress(96, "Initializing client...")
            self.octoprint_client = octoprint_singleton.get_client()
            self.updateLoadingProgress(97, "Loading user interface...")
            self.main_window.loadUI(minimalUI=False)
            self.updateLoadingProgress(98, "Initializing websocket connection...")
            self.initialize_websocket()
            self.updateLoadingProgress(99, "Checking Klipper configuration...")
            self.checkKlipperPrinterCFG()
            # Check for firmware updates during startup if enabled
            self.check_firmware_update()
            self.updateLoadingProgress(100, "Startup complete!")
            self.main_window.switch_to_home_screen()
        except Exception as e:
            self.logger.error(f"Error during startup success handling: {e}")
            self.handleStartupError()

    def handleVirtualFallback(self, message):
        """Show dialog if physical Klipper connection failed and virtual printer used."""
        try:
            self.logger.warning("Virtual printer fallback engaged: %s", message)
            dialog.WarningOk(self.main_window, message, overlay=True)
        except Exception as e:
            self.logger.error(f"Error showing virtual fallback dialog: {e}")

    def handleStartupError(self):
        """Display recovery options when OctoPrint isn't reachable."""
        self.logger.info("OctoPrint connection failed")
        try:
            self.updateLoadingProgress(0, "Connection failed - showing options...")
            if dialog.WarningYesNo(self.main_window, "Server Error, Restore failsafe settings?", overlay=True):
                self.logger.info("Restoring Failsafe Settings")
                self.updateLoadingProgress(25, "Restoring failsafe settings...")
                try:
                    subprocess.run(["sudo", "rm", "-rf", "/home/pi/.octoprint/users.yaml"], check=True)
                    subprocess.run(["sudo", "rm", "-rf", "/home/pi/.octoprint/config.yaml"], check=True)
                    subprocess.run(["sudo", "cp", "-f", "config/users.yaml", "/home/pi/.octoprint/users.yaml"], check=True)
                    subprocess.run(["sudo", "cp", "-f", "config/config.yaml", "/home/pi/.octoprint/config.yaml"], check=True)
                except subprocess.CalledProcessError as e:
                    self.logger.error(f"Failed to restore failsafe settings: {e}")
                    dialog.WarningOk(self.main_window, f"Failed to restore settings: {e}", overlay=True)
                    return
                self.updateLoadingProgress(50, "Restarting OctoPrint service...")
                try:
                    subprocess.run(["sudo", "systemctl", "restart", "octoprint"], check=True)
                except subprocess.CalledProcessError as e:
                    self.logger.error(f"Failed to restart OctoPrint service: {e}")
                    dialog.WarningOk(self.main_window, f"Failed to restart service: {e}", overlay=True)
                self.updateLoadingProgress(10, "Retrying connection...")
                self.connection_check.start()
            else:
                self.logger.info("User chose not to restore failsafe settings")
                self.main_window.loadUI(minimalUI=True)
        except Exception as e:
            self.logger.error(f"Error in handleStartupError: {e}")
            dialog.WarningOk(self.main_window, f"Error in startup error handling: {e}", overlay=True)

    # =========================================================================
    # SECTION: Websocket / Connection Management
    # =========================================================================

    def initialize_websocket(self):
        """Start websocket and bind signals to model/controller."""
        self.octoprint_websocket = OctoPrintWebSocket(ip=ip, api_key=apiKey)
        
        self.octoprint_websocket.start()
        # Model signal wiring
        self.octoprint_websocket.temperatures_signal.connect(self.printer_model.updateTemperature)
        self.octoprint_websocket.status_signal.connect(self.printer_model.updateStatus)
        self.octoprint_websocket.current_position_updated_signal.connect(self.printer_model.update_current_position)
        self.octoprint_websocket.print_status_signal.connect(self.printer_model.updatePrintStatus)
        self.octoprint_websocket.update_started_signal.connect(self.printer_model.softwareUpdateProgress)
        self.octoprint_websocket.update_log_signal.connect(self.printer_model.softwareUpdateProgressLog)
        self.octoprint_websocket.update_log_result_signal.connect(self.printer_model.softwareUpdateResult)
        self.octoprint_websocket.update_failed_signal.connect(self.printer_model.updateFailed)
        self.octoprint_websocket.tool_offset_signal.connect(self.printer_model.setToolOffset)
        self.octoprint_websocket.active_extruder_signal.connect(self.printer_model.setActiveExtruder)
        self.octoprint_websocket.z_probe_offset_signal.connect(self.printer_model.updateEEPROMProbeOffset)
        self.octoprint_websocket.klipper_state_signal.connect(self.printer_model.update_klipper_state)
        self.octoprint_websocket.filament_runout_state_signal.connect(self.printer_model.filamentRunoutState)
        # Probe accuracy results signal for MVP architecture
        self.octoprint_websocket.probe_accuracy_signal.connect(self.printer_model.handle_probe_accuracy_result)
        # Ring heater power signal
        self.octoprint_websocket.ring_heater_power_signal.connect(self.printer_model.update_ring_heater_power)
        # Controller signal wiring
        self.octoprint_websocket.connected_signal.connect(self.onPrinterConnected)
        # Connect to printer model status updates to detect first "Operational" status during startup
        self.printer_model.status_updated.connect(self.onPrinterStatusUpdated)
        # Connect to klipper state changes to monitor for unhealthy states
        self.printer_model.klipper_state_changed.connect(self.onKlipperStateChanged)
        self.octoprint_websocket.filament_runout_sensor_triggered_signal.connect(self.filamentRunoutSensorTriggered)
        self.octoprint_websocket.filament_jam_sensor_triggered_signal.connect(self.filamentJamSensorTriggered)
        self.printer_model.filament_runout_state.connect(self.onFilamentRunoutState)
        self.octoprint_websocket.z_probing_failed_signal.connect(self.showProbingFailed)
        self.octoprint_websocket.printer_error_signal.connect(self.showPrinterError)
        # Print lifecycle events
        self.octoprint_websocket.print_cancelled_signal.connect(self.onPrintCancelled)
        self.octoprint_websocket.print_started_signal.connect(self.onPrintStarted)
        self.octoprint_websocket.print_resumed_signal.connect(self.onPrintResumed)
        self.octoprint_websocket.print_paused_signal.connect(self.onPrintPaused)
        self.octoprint_websocket.print_complete_signal.connect(self.onPrintCompleted)


    def checkKlipperPrinterCFG(self):
        """Validate printer.cfg; attempt restore or cleanup backups."""
        if not self.octoprint_client:
            return
        try:
            manager = get_printer_config_manager()
            if not manager.is_config_valid():
                self.logger.error("Printer Config File Corrupted or Not Found, Attempting to restore Backup")
                if manager.restore_backup_config():
                    self.logger.info("Printer Config File Restored from backup")
                    return
                dialog.WarningOk(self.main_window, "Printer Config File corrupted. Contact Fracktal support or raise a ticket at care.fracktal.in")
                if self.printer_model.printer_status in ["Printing", "Paused"]:
                    self.octoprint_client.cancelPrint()
                    self.coolDownAction()
            else:
                self.logger.info("Printer Config File OK")
                manager.cleanup_old_backups()
                
        except Exception as e:
            self.logger.error(f"Error in MainController.checkKlipperPrinterCFG: {e}")
            dialog.WarningOk(self.main_window, f"Error in MainController.checkKlipperPrinterCFG: {e}", overlay=True)

    def check_firmware_update(self):
        """Check if firmware update is available and prompt user if enabled."""
        try:
            # Check if firmware update checking is enabled
            if not self.printer_model.firmware_update_check_enabled:
                self.logger.debug("Firmware update check disabled by user preference")
                return
                
            from utils.printer_config_manager import (
                is_firmware_update_available,
                get_current_config_version,
                get_firmware_config_version,
                get_current_printer_selection,
                get_printer_display_name,
                copy_firmware_files,
                restore_octoprint_configs
            )
            
            # Check if update is available
            if not is_firmware_update_available():
                self.logger.debug("No firmware update available")
                return
                
            current_version = get_current_config_version()
            firmware_version = get_firmware_config_version()
            current_printer = get_current_printer_selection()
            
            if not current_printer:
                self.logger.warning("No printer configured, skipping firmware update check")
                return
                
            printer_display_name = get_printer_display_name(current_printer)
            
            self.logger.info(f"Firmware update available: v{current_version} -> v{firmware_version}")
            
            # Show update dialog
            update_msg = (
                f"Firmware Update Available!\n\n"
                f"Current version: {current_version or 'Unknown'}\n"
                f"New version: {firmware_version}\n\n"
                f"This will update the configuration for '{printer_display_name}' and restart the printer.\n\n"
                f"Do you want to update now?"
            )
            
            if dialog.WarningYesNo(self.main_window, update_msg, overlay=True):
                self.logger.info("User accepted firmware update")
                self.perform_firmware_update(current_printer, printer_display_name)
            else:
                self.logger.info("User declined firmware update")
                
        except Exception as e:
            self.logger.error(f"Error checking firmware update: {e}")

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

    def restart_printer_system(self, msg="Printer configuration applied successfully!\n\nThe printer needs to restart for changes to take effect.", overlay=True):
        """Show restart dialog and restart the printer system when OK is pressed."""
        try:
            # Use WarningOk which only has an OK button - when clicked, restart immediately
            if dialog.WarningOk(self.main_window, msg, overlay=overlay):
                self.logger.info("User confirmed printer restart - restarting now")
                # Restart the printer system
                os.system('sudo reboot now')
                return True
            return False
        except Exception as e:
            self.logger.error(f"Error during printer restart: {e}")
            dialog.WarningOk(self.main_window, f"Error during restart: {e}", overlay=True)
            return False

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

    # =========================================================================
    # SECTION: Print Restore Management
    # =========================================================================

    def sync_print_restore_settings(self):
        """Sync print restore settings from OctoPrint to local preferences."""
        if not self.octoprint_client:
            return
        try:
            settings = self.octoprint_client.getPrintRestoreSettings()
            if settings and self.printer_model.update_print_restore_settings_from_octoprint(settings):
                # Update UI button states if control screen is available
                try:
                    if hasattr(self.main_window, 'control_screen') and self.main_window.control_screen:
                        control_screen = self.main_window.control_screen
                        # Update print restore button state
                        control_screen.togglePrintRestoreButton.setChecked(self.printer_model.print_restore_enabled)
                        # Update auto resume button state and enabled/disabled state
                        control_screen.toggleAutoResumeButton.setChecked(self.printer_model.auto_resume_enabled)
                        control_screen.toggleAutoResumeButton.setEnabled(self.printer_model.print_restore_enabled)
                        self.logger.info("Updated UI buttons from synced print restore settings")
                except Exception as e:
                    self.logger.warning(f"Failed to update UI buttons after sync: {e}")
                # Update UI state to ensure consistency
                self.update_print_restore_ui_state()
            else:
                self.logger.warning("Failed to sync print restore settings from OctoPrint")
        except Exception as e:
            self.logger.error(f"Error syncing print restore settings: {e}")

    def update_print_restore_ui_state(self):
        """Update UI button states for print restore functionality."""
        try:
            if hasattr(self.main_window, 'control_screen') and self.main_window.control_screen:
                control_screen = self.main_window.control_screen
                # Ensure auto resume button is disabled if print restore is disabled
                print_restore_enabled = self.printer_model.print_restore_enabled
                control_screen.toggleAutoResumeButton.setEnabled(print_restore_enabled)
                # If print restore gets disabled from OctoPrint web UI, disable auto resume too
                if not print_restore_enabled and self.printer_model.auto_resume_enabled:
                    control_screen.toggleAutoResumeButton.setChecked(False)
                    self.printer_model.set_auto_resume_pref(False, persist=True)
                    self.logger.info("Auto-resume disabled because print restore was disabled")
        except Exception as e:
            self.logger.error(f"Error updating print restore UI state: {e}")

    # =========================================================================
    # SECTION: Filament Sensor Management
    # =========================================================================

    def apply_filament_sensor_state(self):
        """Enable/disable sensors per preferences & active print state."""
        if not self.octoprint_client:
            return
        try:
            runout_pref = getattr(self.printer_model, 'filament_runout_sensor_persistent_state', False)
            jam_pref = getattr(self.printer_model, 'filament_jam_sensor_persistent_state', False)
            runout_state = 1 if runout_pref else 0
            jam_state = 1 if jam_pref else 0
            self.octoprint_client.gcode(command=f'SET_FILAMENT_RUNOUT_SENSOR S={runout_state}')
            self.octoprint_client.gcode(command=f'SET_FILAMENT_JAM_SENSOR S={jam_state}')
            self.logger.info(f"Applied filament sensor state: runout={runout_state} jam={jam_state}")
        except Exception as e:
            self.logger.error(f"Failed applying filament sensor state: {e}")

    def filamentRunoutSensorTriggered(self, tool):
        self.logger.info(f"Filament runout sensor triggered for tool: {tool}")
        try:
            # Only respond if printer is actively printing and sensor preference is enabled
            if self.printer_model.printer_status != "Printing":
                self.logger.debug(f"Ignoring filament runout trigger - printer not printing (status: {self.printer_model.printer_status})")
                return
                
            if not getattr(self.printer_model, 'filament_runout_sensor_persistent_state', False):
                self.logger.debug("Ignoring filament runout trigger - sensor preference disabled")
                return
            
            self.octoprint_client.pausePrint()
            if not self.filamentTriggerDialogShown:
                self.filamentTriggerDialogShown = True
                dialog.WarningOk(
                    self.main_window, 
                    f"Filament runout detected on {tool.upper()}!\n\nPrint has been paused. Please load new filament and resume printing.",
                    overlay=True
                )
                self.filamentTriggerDialogShown = False
        except Exception as e:
            self.logger.error(f"Error showing filament runout dialog: {e}")

    def filamentJamSensorTriggered(self, tool):
        self.logger.info(f"Filament jam sensor triggered for tool: {tool}")
        try:
            # Only respond if printer is actively printing and sensor preference is enabled
            if self.printer_model.printer_status != "Printing":
                self.logger.debug(f"Ignoring filament jam trigger - printer not printing (status: {self.printer_model.printer_status})")
                return
                
            if not getattr(self.printer_model, 'filament_jam_sensor_persistent_state', False):
                self.logger.debug("Ignoring filament jam trigger - sensor preference disabled")
                return
            
            self.octoprint_client.pausePrint()
            if not self.filamentTriggerDialogShown:
                self.filamentTriggerDialogShown = True
                dialog.WarningOk(
                    self.main_window,
                    f"Filament jam detected on {tool.upper()}!\n\nPrint has been paused. Please clear the jam and resume printing.",
                    overlay=True
                )
                self.filamentTriggerDialogShown = False
        except Exception as e:
            self.logger.error(f"Error showing filament jam dialog: {e}")

    def onFilamentRunoutState(self, sensor, state):
        self.logger.info(f"Filament runout state changed: {sensor} is {'present' if state else 'not present'}")

    # =========================================================================
    # SECTION: Print Lifecycle Events
    # =========================================================================

    def validate_gcode_compatibility(self, filename):
        """
        Validate GCODE file compatibility with current printer configuration.
        Returns (is_compatible, mismatches) where mismatches is a list of issues.
        """
        if not self.octoprint_client or not filename:
            return True, []

        try:
            # Extract metadata from GCODE file
            gcode_metadata = self.octoprint_client.getGcodeMetadata(filename)
            if not gcode_metadata:
                self.logger.warning(f"Could not extract metadata from {filename}, skipping validation")
                return True, []

            # Get current printer configuration
            current_config = self.printer_model.get_current_tool_config()
            
            mismatches = []
            
            # Check tool0 nozzle
            if gcode_metadata.get('nozzle_t0') is not None:
                expected_nozzle = gcode_metadata['nozzle_t0']
                current_nozzle = current_config['tool0']['nozzle']
                # Skip validation if GCODE has N/A (tool not used)
                if expected_nozzle != 'N/A' and current_nozzle != 'Unknown':
                    # Convert both to float for comparison to handle different string formats
                    try:
                        expected_size = float(expected_nozzle)
                        current_size = float(current_nozzle)
                        if expected_size != current_size:
                            mismatches.append(f"Tool0 nozzle mismatch: GCODE expects {expected_nozzle}mm, printer has {current_nozzle}mm")
                    except ValueError:
                        # Fallback to string comparison if conversion fails
                        if expected_nozzle != current_nozzle:
                            mismatches.append(f"Tool0 nozzle mismatch: GCODE expects {expected_nozzle}mm, printer has {current_nozzle}mm")

            # Check tool1 nozzle
            if gcode_metadata.get('nozzle_t1') is not None:
                expected_nozzle = gcode_metadata['nozzle_t1']
                current_nozzle = current_config['tool1']['nozzle']
                # Skip validation if GCODE has N/A (tool not used)
                if expected_nozzle != 'N/A' and current_nozzle != 'Unknown':
                    # Convert both to float for comparison to handle different string formats
                    try:
                        expected_size = float(expected_nozzle)
                        current_size = float(current_nozzle)
                        if expected_size != current_size:
                            mismatches.append(f"Tool1 nozzle mismatch: GCODE expects {expected_nozzle}mm, printer has {current_nozzle}mm")
                    except ValueError:
                        # Fallback to string comparison if conversion fails
                        if expected_nozzle != current_nozzle:
                            mismatches.append(f"Tool1 nozzle mismatch: GCODE expects {expected_nozzle}mm, printer has {current_nozzle}mm")

            # Check tool0 material
            if gcode_metadata.get('material_t0') is not None:
                expected_material = gcode_metadata['material_t0']
                current_material = current_config['tool0']['material']
                # Skip validation if GCODE has N/A (no material/tool not used)
                if expected_material != 'N/A' and current_material is not None:
                    # Use partial string matching - check if current material is contained in expected material string
                    if not self._materials_compatible(current_material, expected_material):
                        mismatches.append(f"Tool0 material mismatch: GCODE expects {expected_material}, printer has {current_material}")

            # Check tool1 material
            if gcode_metadata.get('material_t1') is not None:
                expected_material = gcode_metadata['material_t1']
                current_material = current_config['tool1']['material']
                # Skip validation if GCODE has N/A (no material/tool not used)
                if expected_material != 'N/A' and current_material is not None:
                    # Use partial string matching - check if current material is contained in expected material string
                    if not self._materials_compatible(current_material, expected_material):
                        mismatches.append(f"Tool1 material mismatch: GCODE expects {expected_material}, printer has {current_material}")

            is_compatible = len(mismatches) == 0
            self.logger.info(f"GCODE compatibility check for {filename}: {'PASS' if is_compatible else 'FAIL'}")
            if mismatches:
                self.logger.warning(f"Compatibility issues found: {mismatches}")
            
            return is_compatible, mismatches

        except Exception as e:
            self.logger.error(f"Error validating GCODE compatibility: {e}")
            return True, []  # Default to compatible on error

    def _materials_compatible(self, current_material, expected_material):
        """
        Check if current material is compatible with expected material from GCODE.
        Uses case-insensitive partial matching to handle cases where:
        - Current: "PLA+" vs Expected: "Fracktal Works PLA+"
        - Current: "Generic ABS" vs Expected: "Generic ABS"
        
        Args:
            current_material (str): Material currently loaded in printer
            expected_material (str): Material expected by GCODE file
            
        Returns:
            bool: True if materials are compatible, False otherwise
        """
        if not current_material or not expected_material:
            return False
            
        # Convert to lowercase for case-insensitive comparison
        current_lower = current_material.lower().strip()
        expected_lower = expected_material.lower().strip()
        
        # Direct match
        if current_lower == expected_lower:
            return True
            
        # Check if current material is contained in expected material string
        # Example: "pla+" in "fracktal works pla+"
        if current_lower in expected_lower:
            return True
            
        # Check if expected material is contained in current material string
        # Example: "generic abs" in "generic abs premium"
        if expected_lower in current_lower:
            return True
            
        return False

    def onPrinterConnected(self):
        """Handle printer connection event (placeholder for future use).
        
        Note: This function is currently not used as it can miss the connection
        event if the printer is already connected when the websocket starts.
        The actual startup logic is handled in onStartupCompleted() which is triggered
        when the printer status first changes to "Operational".
        """
        self.logger.info("MainController.onPrinterConnected - placeholder for future use")
        pass

    def onPrinterStatusUpdated(self, status):
        """Handle printer status updates and trigger startup logic on first 'Operational' status."""
        try:
            if not self.startup_completed and status == "Operational":
                self.logger.info("Printer status changed to 'Operational' for the first time - triggering startup")
                self.startup_completed = True
                self.onStartupCompleted()
        except Exception as e:
            self.logger.error(f"Error in onPrinterStatusUpdated: {e}")

    def onKlipperStateChanged(self, state):
        """Handle Klipper state changes and trigger STATUS refresh if state is unhealthy."""
        try:
            valid_states = ['ready', 'operational', 'idle', 'unknown']
            self.logger.info(f"Klipper state changed to: {state}")
            
            # Check if at least 60 seconds have passed since startup
            time_since_startup = time.time() - self.startup_time
            
            if time_since_startup < 60:
                self.logger.info(f"Ignoring unhealthy Klipper state during startup grace period ({int(time_since_startup)}s elapsed, need 60s)")
                return
            
            # Never trigger automatic FIRMWARE_RESTART recovery during active prints.
            # Sending FIRMWARE_RESTART while printing causes Klipper to emit
            # "Printer is not ready", which showPrinterError treats as a critical
            # error and cancels the print + sends M112.
            if self.printer_model.printer_status in ["Printing", "Paused"]:
                self.logger.warning(f"Klipper state '{state}' is unhealthy but printer is {self.printer_model.printer_status} - skipping automatic recovery to avoid disrupting print")
                return
            
            # Check if state is not in valid states and refresh is not already running
            if state.lower() not in valid_states and not self.klipper_status_refresh_running:
                self.logger.warning(f"Klipper state '{state}' is not healthy, triggering STATUS refresh")
                self.refresh_klipper_status()
        except Exception as e:
            self.logger.error(f"Error in onKlipperStateChanged: {e}")

    def showKlipperErrorDialog(self, error_msg):
        """
        Show Klipper error dialog in the main GUI thread.
        This is a slot connected to klipper_error_signal for thread-safe dialog display.
        
        Args:
            error_msg: The error message to display
        """
        try:
            self.logger.info("Showing Klipper error dialog in main thread")
            dialog.WarningOk(self.main_window, error_msg, overlay=False)
        except Exception as e:
            self.logger.error(f"Error showing Klipper error dialog: {e}")

    @run_async
    def refresh_klipper_status(self):
        """
        Asynchronously send STATUS gcode to OctoPrint to refresh Klipper state.
        Sends FIRMWARE_RESTART before each STATUS attempt, waits 10 seconds, then sends STATUS.
        Repeats up to 5 times until Klipper state becomes valid.
        Shows error dialog if state remains unhealthy after all attempts.
        """
        if not self.octoprint_client:
            self.logger.warning("Cannot refresh Klipper status - OctoPrint client not initialized")
            return
        
        try:
            self.klipper_status_refresh_running = True
            # Suppress transient errors (MCU reset, "Printer is not ready") from our own FIRMWARE_RESTARTs
            self._klipper_restart_in_progress = True
            valid_states = ['ready', 'operational', 'idle', 'unknown']
            self.logger.info("Starting Klipper STATUS refresh cycle (up to 5 attempts)")
            
            state_recovered = False
            final_state = 'unknown'
            
            for attempt in range(1, 6):
                try:
                    # Send FIRMWARE_RESTART before each attempt
                    self.logger.info(f"Attempt {attempt}/5: Sending FIRMWARE_RESTART command")
                    self.octoprint_client.gcode(command='FIRMWARE_RESTART')
                    
                    # Wait 10 seconds for Klipper to restart
                    self.logger.info(f"Waiting 10 seconds for Klipper to restart...")
                    time.sleep(10)
                    
                    # Now send STATUS command
                    self.logger.debug(f"Sending STATUS command (attempt {attempt}/5)")
                    self.octoprint_client.gcode(command='STATUS')
                    
                    # Wait a moment for status to be processed
                    time.sleep(2)
                    
                    # Check if Klipper state is now valid
                    current_state = getattr(self.printer_model, 'klipper_state', 'unknown')
                    self.logger.debug(f"Current Klipper state after attempt {attempt}: {current_state}")
                    
                    if current_state.lower() in valid_states:
                        self.logger.info(f"Klipper state recovered to '{current_state}' after {attempt} attempt(s)")
                        state_recovered = True
                        final_state = current_state
                        break
                    else:
                        self.logger.warning(f"Klipper state still unhealthy: '{current_state}'")
                        final_state = current_state
                        
                except Exception as e:
                    self.logger.error(f"Error during recovery attempt {attempt}/5: {e}")
                    # Continue trying even if one attempt fails
            
            self.logger.info("Completed Klipper STATUS refresh cycle")
            
            # Emit signal if state is still unhealthy after all attempts
            # The signal will be handled in the main thread to show the dialog safely
            if not state_recovered:
                self.logger.error(f"Klipper state '{final_state}' remains unhealthy after 5 refresh attempts")
                error_msg = (
                    f"Klipper Connection Error\n\n"
                    f"The printer's Klipper firmware is reporting an unhealthy state: '{final_state}'\n\n"
                    f"Attempted automatic recovery but the issue persists.\n\n"
                    f"Please check the OctoPrint Web UI for detailed error messages and debug information."
                )
                # Emit signal to show dialog in main thread (thread-safe)
                self.klipper_error_signal.emit(error_msg)
                    
        except Exception as e:
            self.logger.error(f"Error in refresh_klipper_status: {e}")
        finally:
            self.klipper_status_refresh_running = False
            self._klipper_restart_in_progress = False

    def onStartupCompleted(self):
        """Handle startup operations when printer first becomes operational."""
        self.logger.info("MainController.onStartupCompleted started")
        if self.octoprint_client:
            try:
                self.octoprint_client.gcode(command='SET_FILAMENT_RUNOUT_SENSOR S=0')
                self.octoprint_client.gcode(command='SET_FILAMENT_JAM_SENSOR S=0')
                # Reload printer configuration from Klipper after connection is established
                self.printer_model.reload_printer_configuration()
                
                status_response = self.octoprint_client.gcode(command='status')
                if status_response:
                    self.logger.debug(f"Printer status response: {status_response}")
                try:
                    response = self.octoprint_client.isFailureDetected()
                    if response.get("canRestore"):
                        self.printRestoreMessageBox(response.get("file"))
                except (KeyError, TypeError, AttributeError) as e:
                    self.logger.warning(f"Failed to check for print failure: {e}")
                except Exception as e:
                    self.logger.error(f"Unexpected error checking print failure: {e}")
                try:
                    self.sync_print_restore_settings()
                except Exception as e:
                    self.logger.warning(f"Failed applying initial filament sensor state: {e}")
            except Exception as e:
                self.logger.error(f"Error in MainController.onStartupCompleted: {e}")
                dialog.WarningOk(self.main_window, f"Error in MainController.onStartupCompleted: {e}", overlay=True)

    def onPrintStarted(self, event):
        try:
            self.logger.info("MainController.onPrintStarted invoked")
            
            # Check GCODE compatibility if enabled and we have file information
            # According to OctoPrint docs, PrintStarted event structure is: {'type': 'PrintStarted', 'payload': {...}}
            if (self.printer_model.print_compatibility_check_enabled and event):
                # Extract filename from the correct event structure
                filename = None
                if isinstance(event, dict) and 'payload' in event:
                    filename = event['payload'].get('name')
                
                if filename:
                    self.logger.info(f"Validating GCODE compatibility for file: {filename}")
                    is_compatible, mismatches = self.validate_gcode_compatibility(filename)
                    
                    if not is_compatible:
                        # Pause the print immediately (if not already paused)
                        if self.octoprint_client:
                            try:
                                job_info = self.octoprint_client.getJobInformation()
                                if job_info and job_info.get('state') not in ['Paused', 'Pausing']:
                                    self.octoprint_client.pausePrint()
                            except Exception as e:
                                self.logger.error(f"Error pausing print: {e}")
                                # Try to pause anyway
                                self.octoprint_client.pausePrint()
                        
                        # Show dialog with mismatch information
                        mismatch_text = "Print configuration mismatch detected:\n\n" + "\n".join(mismatches)
                        mismatch_text += "\n\nWould you like to continue printing anyway?"
                        
                        if dialog.WarningYesNo(self.main_window, mismatch_text, overlay=True):
                            # User wants to continue - resume the print
                            self.logger.info("User chose to continue print despite mismatches")
                            if self.octoprint_client:
                                self.octoprint_client.startPrint()  # Resume from pause
                        else:
                            # User wants to cancel - cancel the print
                            self.logger.info("User chose to cancel print due to mismatches")
                            if self.octoprint_client:
                                self.octoprint_client.cancelPrint()
                            return
                else:
                    self.logger.warning("PrintStarted event received but no filename found in event data")
            elif not self.printer_model.print_compatibility_check_enabled:
                self.logger.info("Print compatibility check is disabled, skipping validation")
            
            # Apply filament sensor state if print continues
            self.apply_filament_sensor_state()
            
            # Navigate to home screen when print starts
            self.logger.info("Navigating to home screen after print started")
            self.main_window.switch_to_home_screen()
            
        except Exception as e:
            self.logger.error(f"Error in onPrintStarted: {e}")

    def onPrintResumed(self, event):
        try:
            self.logger.info("MainController.onPrintResumed invoked")
            self.apply_filament_sensor_state()
        except Exception as e:
            self.logger.error(f"Error in onPrintResumed: {e}")

    def onPrintPaused(self, event):
        try:
            self.logger.info("MainController.onPrintPaused invoked")
            if self.octoprint_client:
                # Immediately disable filament sensors when print is paused
                self.octoprint_client.gcode(command='SET_FILAMENT_RUNOUT_SENSOR S=0')
                self.octoprint_client.gcode(command='SET_FILAMENT_JAM_SENSOR S=0')
        except Exception as e:
            self.logger.error(f"Error in onPrintPaused: {e}")

    def onPrintCancelled(self, event):
        try:
            self.logger.info("MainController.onPrintCancelled invoked")
            if self.octoprint_client:
                # Disable filament sensors immediately
                self.octoprint_client.gcode(command='SET_FILAMENT_RUNOUT_SENSOR S=0')
                self.octoprint_client.gcode(command='SET_FILAMENT_JAM_SENSOR S=0')
                # Cool down the printer
                self.coolDownAction()
        except Exception as e:
            self.logger.error(f"Error in onPrintCancelled: {e}")

    def onPrintCompleted(self, event):
        try:
            self.logger.info("MainController.onPrintCompleted invoked")
            if self.octoprint_client:
                # Disable filament sensors immediately
                self.octoprint_client.gcode(command='SET_FILAMENT_RUNOUT_SENSOR S=0')
                self.octoprint_client.gcode(command='SET_FILAMENT_JAM_SENSOR S=0')
                # Cool down the printer
                self.coolDownAction()
        except Exception as e:
            self.logger.error(f"Error in onPrintCompleted: {e}")
        #self.octoprint_websocket.print_paused_signal.connect(self.onPrintPaused)

    # =========================================================================
    # SECTION: Error & Recovery Dialogs
    # =========================================================================

    def showProbingFailed(self, msg='Probing Failed, Calibrate bed again or check for hardware issue', overlay=True):
        self.logger.info("MainController.showProbingFailed started")
        if self.octoprint_client:
            try:
                if dialog.WarningOk(self.main_window, msg, overlay=overlay):
                    self.octoprint_client.cancelPrint()
                    return True
                return False
            except Exception as e:
                self.logger.error(f"Error in MainController.showProbingFailed: {e}")
                dialog.WarningOk(self.main_window, f"Error in MainController.showProbingFailed: {e}", overlay=True)

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

    def printRestoreMessageBox(self, file):
        self.logger.info("MainController.printRestoreMessageBox started")
        if self.octoprint_client:
            try:
                if dialog.WarningYesNo(self.main_window, file + " Did not finish, would you like to restore?"):
                    response = self.octoprint_client.restore(restore=True)
                    dialog.WarningOk(self.main_window, response.get("status", "Unknown status"))
            except Exception as e:
                self.logger.error(f"Error in MainController.printRestoreMessageBox: {e}")
                dialog.WarningOk(self.main_window, f"Error in MainController.printRestoreMessageBox: {e}", overlay=True)

    # =========================================================================
    # SECTION: Utility / Convenience Actions
    # =========================================================================

    def coolDownAction(self):
        self.logger.info("MainController.coolDownAction started")
        try:
            self.octoprint_client.gcode(command='M107')
            self.octoprint_client.setToolTemperature({"tool0": 0, "tool1": 0})
            self.octoprint_client.setBedTemperature(0)
            # Turn off Chamber heater
            self.octoprint_client.gcode(command='CHAMBER_OFF')
            # Turn off Ring heater
            self.octoprint_client.gcode(command='M144')
            # Turn off Filament/Spool heater
            self.octoprint_client.gcode(command='M142 S0')
            self.main_window.control_screen.toolTempSpinBox.setProperty("value", 0)
            self.main_window.control_screen.bedTempSpinBox.setProperty("value", 0)
        except Exception as e:
            self.logger.error(f"Error in MainController.coolDownAction: {e}")
            dialog.WarningOk(self.main_window, f"Error in MainController.coolDownAction: {e}", overlay=True)

    def cleanup(self):
        """Cleanup resources when shutting down the application"""
        self.logger.info("MainController cleanup started")
        try:
            # Stop websocket connection properly
            if hasattr(self, 'octoprint_websocket') and self.octoprint_websocket:
                self.logger.info("Stopping WebSocket connection...")
                self.octoprint_websocket.stop_websocket()
                
            self.logger.info("MainController cleanup completed")
        except Exception as e:
            self.logger.error(f"Error during MainController cleanup: {e}")