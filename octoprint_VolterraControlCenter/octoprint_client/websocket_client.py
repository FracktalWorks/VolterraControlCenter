"""
WebSocket Client for OctoPrint

This module handles real-time communication with OctoPrint via WebSockets
"""
import json
import re
import random
import threading
import time
import uuid
import websocket
import requests
from PyQt5.QtCore import QThread, pyqtSignal

from utils.logger import get_logger
from config import IGNORED_PRINTER_ERRORS


class OctoPrintWebSocket(QThread):
    """
    WebSocket client for OctoPrint that connects to the WebSocket API
    and emits signals when events occur
    """
    # Define signals for UI updates
    # z_home_offset_signal = pyqtSignal(str) ... deprecated, uses probe_offset
    temperatures_signal = pyqtSignal(dict) #! done
    status_signal = pyqtSignal(str) #! done
    print_status_signal = pyqtSignal('PyQt_PyObject') #! done
    # Use class-level logger for all logging
    update_started_signal = pyqtSignal('PyQt_PyObject') #! done
    update_log_signal = pyqtSignal('PyQt_PyObject') #! done
    update_log_result_signal = pyqtSignal('PyQt_PyObject') #! done
    update_failed_signal = pyqtSignal('PyQt_PyObject') #! done
    connected_signal = pyqtSignal() #! done
    # firmware_updater_signal = pyqtSignal(dict) ... likely not to be used, but can be added later
    tool_offset_signal = pyqtSignal(str) # done
    active_extruder_signal = pyqtSignal(str) # done
    z_probe_offset_signal = pyqtSignal(str) # done
    z_probing_failed_signal = pyqtSignal() # done
    printer_error_signal = pyqtSignal(str) # done
    # New: Klipper state updates (e.g., 'ready', 'shutdown')
    klipper_state_signal = pyqtSignal(str)

    # New: Print lifecycle events
    print_cancelled_signal = pyqtSignal(dict)
    print_started_signal = pyqtSignal(dict)
    print_resumed_signal = pyqtSignal(dict)
    print_paused_signal = pyqtSignal(dict)
    print_complete_signal = pyqtSignal(dict)


    # Filament sensor related signals
    filament_runout_sensor_triggered_signal = pyqtSignal(str) #! done
    filament_jam_sensor_triggered_signal = pyqtSignal(str) #! done
    filament_runout_state_signal = pyqtSignal(str, bool) #! done
    
    # Position update signals
    current_position_updated_signal = pyqtSignal(dict)  # {'x': float, 'y': float, 'z': float}
    
    # Probe accuracy results signal for MVP architecture
    probe_accuracy_signal = pyqtSignal(str)  # Raw probe accuracy message string
    
    # Ring heater power signal
    ring_heater_power_signal = pyqtSignal(int)  # Power level 0-255

    def __init__(self, ip="0.0.0.0:5000", api_key=None):
        """
        Initialize the WebSocket client
        :param ip: IP address of the OctoPrint server
        :param api_key: API key for OctoPrint
        """
        super(OctoPrintWebSocket, self).__init__()
        self.logger = get_logger(self.__class__.__name__)
        self.ip = ip
        self.api_key = api_key
        self.ws = None
        self.heartbeat_timer = None
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.is_reconnecting = False  # Track reconnection state
        self.connection_active = False  # Track if connection is active

        self.logger.info(f"OctoPrintWebSocket initializing with IP: {self.ip}, API Key: {'***' + self.api_key[-4:] if self.api_key else 'None'}")
        self._initialize_websocket()

    def _initialize_websocket(self):
        """Initialize or reinitialize the WebSocket connection"""
        try:
            # Close existing connection if any
            self._cleanup_websocket()
            
            url = f"ws://{self.ip}/sockjs/{random.randrange(0, stop=999):0>3d}/{uuid.uuid4()}/websocket"
            self.logger.info(f"Creating WebSocket connection to: {url}")
            self.ws = websocket.WebSocketApp(
                url,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
                on_open=self.on_open
            )
            self.logger.info("WebSocket app created successfully")
        except Exception as e:
            self.logger.error(f"Error initializing WebSocket: {e}")
            raise

    def _cleanup_websocket(self):
        """Properly cleanup existing websocket connection and timers"""
        try:
            # Cancel heartbeat timer
            if self.heartbeat_timer is not None:
                self.heartbeat_timer.cancel()
                self.heartbeat_timer = None
                self.logger.debug("Heartbeat timer cancelled during cleanup")
            
            # Close existing websocket connection
            if self.ws is not None:
                try:
                    self.ws.close()
                    self.logger.debug("Existing WebSocket connection closed")
                except Exception as e:
                    self.logger.warning(f"Error closing existing WebSocket: {e}")
                self.ws = None
            
            self.connection_active = False
            
        except Exception as e:
            self.logger.error(f"Error during websocket cleanup: {e}")

    def run(self):
        self.logger.info("WebSocket thread starting...")
        try:
            self.logger.info("Starting WebSocket run_forever() loop")
            self.ws.run_forever()
            self.logger.info("WebSocket run_forever() completed")
        except Exception as e:
            self.logger.error("Error in QtWebsocket.run: {}".format(e))
        finally:
            # Ensure connection state is properly set when thread exits
            self.connection_active = False
            self.logger.info("WebSocket thread ended")

    def reset_heartbeat_timer(self):
        """Reset the heartbeat timer - only called when connection is active"""
        try:
            # Only reset if we have an active connection
            if not self.connection_active:
                self.logger.debug("Skipping heartbeat reset - connection not active")
                return
                
            if self.heartbeat_timer is not None:
                self.heartbeat_timer.cancel()
                self.logger.debug("Previous heartbeat timer cancelled")

            self.heartbeat_timer = threading.Timer(120, self.reestablish_connection)  # 120 seconds = 2 minutes
            self.heartbeat_timer.start()
            self.logger.debug("Heartbeat timer reset - 120 seconds")
        except Exception as e:
            self.logger.error("Error in QtWebsocket.reset_heartbeat_timer: {}".format(e))

    def reestablish_connection(self):
        """Reestablish WebSocket connection with proper thread management"""
        # Prevent multiple simultaneous reconnection attempts
        if self.is_reconnecting:
            self.logger.debug("Reconnection already in progress, skipping")
            return
            
        self.logger.info("Reestablishing WebSocket connection...")
        self.is_reconnecting = True
        
        try:
            self.reconnect_attempts += 1
            self.logger.info(f"Reconnection attempt {self.reconnect_attempts}/{self.max_reconnect_attempts}")
            
            if self.reconnect_attempts > self.max_reconnect_attempts:
                self.logger.warning("Max reconnect attempts reached. Waiting 30s before resetting counter and retrying.")
                time.sleep(30)
                self.reconnect_attempts = 1
                self.logger.info("Reconnect counter reset, retrying...")

            # Properly terminate current thread if it's still running
            if self.isRunning():
                self.logger.info("Waiting for current thread to finish...")
                try:
                    # First try to close the websocket gracefully
                    if self.ws:
                        self.ws.close()
                    # Wait for thread to finish with timeout
                    if not self.wait(5000):  # 5 second timeout
                        self.logger.warning("Thread did not finish within timeout, forcing termination")
                        self.terminate()
                        self.wait(2000)  # Wait for terminate to complete
                except Exception as e:
                    self.logger.error(f"Error stopping current thread: {e}")

            self.logger.info("Reinitializing WebSocket...")
            self._initialize_websocket()
            
            # Create new thread instance for reconnection
            # We cannot reuse QThread objects - need to start this thread
            self.logger.info("Starting new WebSocket connection...")
            self.start()
            self.logger.info("Reconnection attempt {} initiated.".format(self.reconnect_attempts))
            
        except Exception as e:
            self.logger.error("Error in QtWebsocket.reestablish_connection: {}".format(e))
        finally:
            self.is_reconnecting = False

    def send(self, data):
        """
        Send data to the WebSocket
        :param data: Data to send
        """
        self.logger.info(f"Sending data via WebSocket: {data}")
        try:
            payload = '["' + json.dumps(data).replace('"', '\\"') + '"]'
            self.logger.debug(f"WebSocket payload: {payload}")
            self.ws.send(payload)
            self.logger.debug("Data sent successfully")
        except Exception as e:
            self.logger.error(f"Error sending data via WebSocket: {e}")

    def authenticate(self):
        """
        Authenticate with the OctoPrint server
        """
        self.logger.info("Authenticating WebSocket connection")
        try:
            # perform passive login to retrieve username and session key for API key
            url = f'http://{self.ip}/api/login'
            self.logger.info(f"Attempting passive login to: {url}")
            headers = {'content-type': 'application/json', 'X-Api-Key': self.api_key}
            payload = {"passive": True}
            
            self.logger.debug("Sending authentication request...")
            response = requests.post(url, data=json.dumps(payload), headers=headers)
            self.logger.info(f"Authentication response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                self.logger.info(f"Authentication successful for user: {data.get('name', 'unknown')}")
                
                # prepare auth payload
                auth_message = {"auth": f"{data['name']}:{data['session']}"}
                self.logger.debug("Sending WebSocket auth message...")
                
                # send it
                self.send(auth_message)
            else:
                self.logger.error(f"Authentication failed with status {response.status_code}: {response.text}")
        except Exception as e:
            self.logger.error(f"Error authenticating WebSocket: {e}")
            raise

    def on_message(self, ws, message):
        """
        Handle messages from the WebSocket
        :param ws: WebSocket instance
        :param message: Message from the WebSocket
        """
        self.logger.debug(f"WebSocket message received: {message[:100]}{'...' if len(message) > 100 else ''}")
        
        message_type = message[0]
        if message_type == "h":
            # "heartbeat" message
            self.logger.debug("Received heartbeat message")
            self.reset_heartbeat_timer()
            return
        elif message_type == "o":
            # "open" message
            self.logger.info("WebSocket connection opened")
            return
        elif message_type == "c":
            # "close" message
            self.logger.warning("Received close message from WebSocket")
            return

        message_body = message[1:]
        if not message_body:
            self.logger.debug("Empty message body received")
            return
            
        try:
            data = json.loads(message_body)[0]
            self.logger.debug(f"Parsed message data keys: {list(data.keys()) if isinstance(data, dict) else 'not dict'}")
        except (json.JSONDecodeError, IndexError) as e:
            self.logger.error(f"Failed to parse message body: {e}")
            return

        if message_type == "m":
            data = [data, ]

        if message_type == "a":
            self.logger.debug("Processing message data...")
            self.process(data)

    def on_open(self, ws):
        """
        Handle WebSocket connection open
        :param ws: WebSocket instance
        """
        self.logger.info("WebSocket connection opened successfully")
        self.connection_active = True
        self.reconnect_attempts = 0  # Reset reconnect counter on successful connection
        self.is_reconnecting = False  # Clear reconnecting flag
        
        # Start heartbeat timer now that connection is established
        self.reset_heartbeat_timer()
        
        self.logger.info("Starting authentication process...")
        try:
            self.authenticate()
        except Exception as e:
            self.logger.error(f"Authentication failed during connection open: {e}")

    def on_close(self, ws, *args, **kwargs):
        self.logger.warning(f"WebSocket connection closed. Args: {args}, Kwargs: {kwargs}")
        self.connection_active = False
        
        # Cancel heartbeat timer since connection is closed
        if self.heartbeat_timer is not None:
            self.heartbeat_timer.cancel()
            self.heartbeat_timer = None
            
        # Only attempt reconnection if we're not already reconnecting
        if not self.is_reconnecting:
            self.logger.info("Attempting to reconnect...")
            self.reestablish_connection()
        else:
            self.logger.debug("Reconnection already in progress, skipping")

    def on_error(self, ws, error):
        self.logger.error("Error in WebSocket connection: {}".format(error))
        self.connection_active = False
        
        # Cancel heartbeat timer since connection has error
        if self.heartbeat_timer is not None:
            self.heartbeat_timer.cancel()
            self.heartbeat_timer = None
            
        # Only attempt reconnection if we're not already reconnecting
        if not self.is_reconnecting:
            self.logger.info("Error occurred, attempting to reconnect...")
            self.reestablish_connection()
        else:
            self.logger.debug("Reconnection already in progress, skipping")

    def process(self, data):
        """
        Process data from the WebSocket.
        Called from the websocket thread — signals are cross-thread
        and delivered via Qt's event loop, so no extra thread needed.
        :param data: Data from the WebSocket
        """
        try:
            self.logger.debug("Processing WebSocket data...")
            
            # Validate input data
            if not data or not isinstance(data, (dict, list)):
                self.logger.warning(f"Invalid data received: {type(data)}")
                return
            
            if "event" in data:
                try:
                    self.logger.info(f"Event received: {data['event'].get('type', 'unknown')}")
                    if data["event"]["type"] == "Connected":
                        self.logger.info("Emitting connected_signal")
                        self.connected_signal.emit()
                        self.logger.info("Connected to OctoPrint server")
                    elif data["event"]["type"] == "PrintCancelled":
                        self.logger.info("Emitting print_cancelled_signal")
                        try:
                            self.print_cancelled_signal.emit(data["event"])  # pass entire event payload
                        except Exception as e:
                            self.logger.error(f"Error emitting print_cancelled_signal: {e}")
                    elif data["event"]["type"] == "PrintStarted":
                        self.logger.info("Emitting print_started_signal")
                        try:
                            self.print_started_signal.emit(data["event"])  # pass entire event payload
                        except Exception as e:
                            self.logger.error(f"Error emitting print_started_signal: {e}")
                    elif data["event"]["type"] == "PrintResumed":
                        self.logger.info("Emitting print_resumed_signal")
                        try:
                            self.print_resumed_signal.emit(data["event"])  # pass entire event payload
                        except Exception as e:
                            self.logger.error(f"Error emitting print_resumed_signal: {e}")
                    elif data["event"]["type"] == "PrintPaused":
                        self.logger.info("Emitting print_paused_signal")
                        try:
                            self.print_paused_signal.emit(data["event"])  # pass entire event payload
                        except Exception as e:
                            self.logger.error(f"Error emitting print_paused_signal: {e}")
                    elif data["event"]["type"] == "PrintDone":
                        self.logger.info("Emitting print_complete_signal")
                        try:
                            self.print_complete_signal.emit(data["event"])  # pass entire event payload
                        except Exception as e:
                            self.logger.error(f"Error emitting print_complete_signal: {e}")
                except Exception as e:
                    self.logger.error(f"Error processing event data: {e}")
            
            if "plugin" in data:
                try:
                    plugin_name = data["plugin"]["plugin"]
                    self.logger.info(f"Plugin message received from: {plugin_name}")

                    if plugin_name == 'klipper':
                        # Extract plugin data; emit error and state events
                        plugin_data = data["plugin"].get("data")
                        if isinstance(plugin_data, dict):
                            # Error messages from Klipper
                            if plugin_data.get('subtype') == 'error':
                                error_message = plugin_data.get('payload', plugin_data.get('title', str(plugin_data)))
                                self.logger.error(f"Klipper error detected: {error_message}")
                                try:
                                    self.printer_error_signal.emit(str(error_message).strip())
                                except Exception as e:
                                    self.logger.error(f"Error emitting printer_error_signal: {e}")

                            # Check for probe accuracy results in both title and payload
                            for key in ('title', 'payload'):
                                text = plugin_data.get(key)
                                if not isinstance(text, str):
                                    continue
                                
                                # Check for probe accuracy results first
                                if 'probe accuracy results:' in text.lower():
                                    self.logger.info(f"Probe accuracy results detected in {key}: {text.strip()}")
                                    try:
                                        self.probe_accuracy_signal.emit(text.strip())
                                    except Exception as e:
                                        self.logger.error(f"Error emitting probe_accuracy_signal: {e}")
                                    # Don't continue here, still check for state messages
                                
                                # Check for Klipper state messages
                                m = re.search(r"klipper\s*state:\s*([^\n\r]+)", text, flags=re.IGNORECASE)
                                if m:
                                    state = m.group(1).strip()
                                    norm = state.lower()
                                    self.logger.info(f"Klipper state parsed from {key}: {norm}")
                                    try:
                                        self.klipper_state_signal.emit(norm)
                                        print(f"Klipper state emitted: {norm}")
                                    except Exception as e:
                                        self.logger.error(f"Error emitting klipper_state_signal: {e}")
                                    break

                    if plugin_name == 'JuliaFirmwareUpdater':
                        self.logger.info("Emitting firmware_updater_signal")
                        # Note: firmware_updater_signal not defined in this class
                        # self.firmware_updater_signal.emit(data["plugin"]["data"])

                    elif plugin_name == 'softwareupdate':
                        update_type = data["plugin"]["data"]["type"]
                        self.logger.info(f"Software update message type: {update_type}")
                        
                        if update_type == "updating":
                            self.logger.info("Emitting update_started_signal")
                            try:
                                self.update_started_signal.emit(data["plugin"]["data"]["data"])
                            except Exception as e:
                                self.logger.error(f"Error emitting update_started_signal: {e}")
                        elif update_type == "loglines":
                            self.logger.debug("Emitting update_log_signal")
                            try:
                                self.update_log_signal.emit(data["plugin"]["data"]["data"]["loglines"])
                            except Exception as e:
                                self.logger.error(f"Error emitting update_log_signal: {e}")
                        elif update_type == "restarting":
                            self.logger.info("Emitting update_log_result_signal")
                            try:
                                self.update_log_result_signal.emit(data["plugin"]["data"]["data"]["results"])
                            except Exception as e:
                                self.logger.error(f"Error emitting update_log_result_signal: {e}")
                        elif update_type == "update_failed":
                            self.logger.warning("Emitting update_failed_signal")
                            try:
                                self.update_failed_signal.emit(data["plugin"]["data"]["data"])
                            except Exception as e:
                                self.logger.error(f"Error emitting update_failed_signal: {e}")
                except Exception as e:
                    self.logger.error(f"Error processing plugin data: {e}")

            if "current" in data:
                try:
                    self.logger.debug("Processing current state data...")
                    
                    # Process messages
                    if data["current"]["messages"]:
                        self.logger.debug(f"Processing {len(data['current']['messages'])} messages")
                        for item in data["current"]["messages"]:
                            try:
                                self.logger.debug(f"Processing message: {item}")
                                
                                # Filament sensor messages
                                if 'Filament Runout Detected ' in item:  # "Filament Runout on T0/T1"
                                    tool = item[item.index('T') + 1:].split(' ', 1)[0]
                                    self.logger.info(f"Filament runout triggered on tool {tool}")
                                    try:
                                        self.filament_runout_sensor_triggered_signal.emit(tool)
                                    except Exception as e:
                                        self.logger.error(f"Error emitting filament_runout_sensor_triggered_signal: {e}")

                                elif 'Filament Jam Detected ' in item:  # "Filament Jam on T0/T1"
                                    tool = item[item.index('T') + 1:].split(' ', 1)[0]
                                    self.logger.info(f"Filament jam triggered on tool {tool}")
                                    try:
                                        self.filament_jam_sensor_triggered_signal.emit(tool)
                                    except Exception as e:
                                        self.logger.error(f"Error emitting filament_jam_sensor_triggered_signal: {e}")
                                
                                elif 'filament detected' in item:
                                    sensor = item[item.index('T') + 1:].split(' ', 1)[0]
                                    self.logger.info(f"Filament detected in {sensor}")
                                    try:
                                        self.filament_runout_state_signal.emit(sensor, True)
                                    except Exception as e:
                                        self.logger.error(f"Error emitting filament_runout_state_signal: {e}")
                                
                                elif 'filament not detected' in item:
                                    sensor = item[item.index('T') + 1:].split(' ', 1)[0]
                                    self.logger.info(f"Filament not Detected in {sensor}")
                                    try:
                                        self.filament_runout_state_signal.emit(sensor, False)
                                    except Exception as e:
                                        self.logger.error(f"Error emitting filament_runout_state_signal: {e}")

                                # Position parsing
                                elif 'Count' in item or 'Coord' in item:
                                    # Parse full position from M114 response
                                    # Handles both formats:
                                    # - "Count X:123.45 Y:67.89 Z:0.12" (Marlin)
                                    # - "Count Coord(x=300.0, y=10.0, z=10.0, e=0.0)" (Klipper)
                                    try:
                                        position = {}
                                        
                                        # Check if it's Klipper format with Coord()
                                        if 'Coord(' in item:
                                            # Klipper format: "Count Coord(x=300.0, y=10.0, z=10.0, e=0.0)"
                                            coord_match = re.search(r'Coord\(([^)]+)\)', item)
                                            if coord_match:
                                                coord_str = coord_match.group(1)
                                                # Parse x=value, y=value, z=value
                                                for match in re.finditer(r'([xyz])=([\d.-]+)', coord_str):
                                                    axis = match.group(1)
                                                    value = float(match.group(2))
                                                    # Round to 3 decimal places
                                                    position[axis] = round(value, 3)

                                        else:
                                            # Marlin format: "Count X:123.45 Y:67.89 Z:0.12"
                                            # Extract X position
                                            if 'x' in item.lower():
                                                try:
                                                    x_start = item.lower().index('x') + 1
                                                    if x_start < len(item) and item[x_start] == ':':
                                                        x_start += 1
                                                    x_end = x_start
                                                    # Add safety check to prevent infinite loops
                                                    max_chars = min(20, len(item) - x_start)  # Limit search to reasonable range
                                                    chars_checked = 0
                                                    while (x_end < len(item) and chars_checked < max_chars and 
                                                           (item[x_end].isdigit() or item[x_end] in '.-')):
                                                        x_end += 1
                                                        chars_checked += 1
                                                    if x_end > x_start:
                                                        position['x'] = round(float(item[x_start:x_end]), 3)
                                                except (ValueError, IndexError) as e:
                                                    self.logger.warning(f"Error parsing X position from '{item}': {e}")
                                            
                                            # Extract Y position  
                                            if 'y' in item.lower():
                                                try:
                                                    y_start = item.lower().index('y') + 1
                                                    if y_start < len(item) and item[y_start] == ':':
                                                        y_start += 1
                                                    y_end = y_start
                                                    # Add safety check to prevent infinite loops
                                                    max_chars = min(20, len(item) - y_start)  # Limit search to reasonable range
                                                    chars_checked = 0
                                                    while (y_end < len(item) and chars_checked < max_chars and 
                                                           (item[y_end].isdigit() or item[y_end] in '.-')):
                                                        y_end += 1
                                                        chars_checked += 1
                                                    if y_end > y_start:
                                                        position['y'] = round(float(item[y_start:y_end]), 3)
                                                except (ValueError, IndexError) as e:
                                                    self.logger.warning(f"Error parsing Y position from '{item}': {e}")
                                            
                                            # Extract Z position
                                            if 'z' in item.lower():
                                                try:
                                                    z_start = item.lower().index('z') + 1
                                                    if z_start < len(item) and item[z_start] == ':':
                                                        z_start += 1
                                                    z_end = z_start
                                                    # Add safety check to prevent infinite loops
                                                    max_chars = min(20, len(item) - z_start)  # Limit search to reasonable range
                                                    chars_checked = 0
                                                    while (z_end < len(item) and chars_checked < max_chars and 
                                                           (item[z_end].isdigit() or item[z_end] in '.-')):
                                                        z_end += 1
                                                        chars_checked += 1
                                                    if z_end > z_start:
                                                        position['z'] = round(float(item[z_start:z_end]), 3)
                                                except (ValueError, IndexError) as e:
                                                    self.logger.warning(f"Error parsing Z position from '{item}': {e}")
                                        
                                        # Emit full position update if we got any coordinates
                                        if position:
                                            self.logger.debug(f"Position update: {position}")
                                            try:
                                                self.current_position_updated_signal.emit(position)
                                            except Exception as e:
                                                self.logger.error(f"Error emitting position signal: {e}")
                                                
                                    except Exception as e:
                                        self.logger.error(f"Error parsing position from '{item}': {e}")

                                # Tool offset messages
                                elif 'M218' in item:
                                    tool_offset_data = item[item.index('M218'):]
                                    self.logger.info(f"Tool offset data: {tool_offset_data}")
                                    try:
                                        self.tool_offset_signal.emit(tool_offset_data)
                                    except Exception as e:
                                        self.logger.error(f"Error emitting tool_offset_signal: {e}")
                                    
                                # Active extruder changes
                                elif 'Active Extruder' in item:  # can get through the positionUpdate event
                                    extruder = item[-1]
                                    self.logger.info(f"Active extruder changed to: {extruder}")
                                    try:
                                        self.active_extruder_signal.emit(extruder)
                                    except Exception as e:
                                        self.logger.error(f"Error emitting active_extruder_signal: {e}")

                                # Probe offset messages
                                elif 'M851' in item:
                                    probe_offset = item[item.index('Z') + 1:].split(' ', 1)[0]
                                    self.logger.info(f"Z probe offset: {probe_offset}")
                                    try:
                                        self.z_probe_offset_signal.emit(probe_offset)
                                    except Exception as e:
                                        self.logger.error(f"Error emitting z_probe_offset_signal: {e}")
                                    
                                # Probing failed messages
                                elif 'PROBING_FAILED' in item:
                                    self.logger.warning("Z probing failed!")
                                    try:
                                        self.z_probing_failed_signal.emit()
                                    except Exception as e:
                                        self.logger.error(f"Error emitting z_probing_failed_signal: {e}")
                                
                                # Ring heater power level updates (M143 responses)
                                elif 'Ring heater set to' in item:
                                    try:
                                        # Parse "Ring heater set to XX.X% (YYY/255, max ZZ%)"
                                        match = re.search(r'\((\d+)/255', item)
                                        if match:
                                            power_level = int(match.group(1))
                                            self.logger.debug(f"Ring heater power: {power_level}/255")
                                            self.ring_heater_power_signal.emit(power_level)
                                    except Exception as e:
                                        self.logger.error(f"Error parsing ring heater power: {e}")

                                # Error messages - Check for errors - emit all errors, let showPrinterError decide what to show
                                elif item.startswith('!!') or item.startswith('Error'):
                                    self.logger.error(f"Printer error detected: {item}")
                                    try:
                                        self.printer_error_signal.emit(item)
                                    except Exception as e:
                                        self.logger.error(f"Error emitting printer_error_signal: {e}")
                                        
                            except Exception as e:
                                self.logger.error(f"Error processing message '{item}': {e}")

                        # Note: Probe accuracy results are now handled in Klipper plugin section above

                except Exception as e:
                    self.logger.error(f"Error processing current state data: {e}")

                # Process printer status
                try:
                    if data["current"]["state"]["text"]:
                        status = data["current"]["state"]["text"]
                        self.logger.debug(f"Printer status update: {status}")
                        try:
                            self.status_signal.emit(status)
                        except Exception as e:
                            self.logger.error(f"Error emitting status_signal: {e}")
                except Exception as e:
                    self.logger.error(f"Error processing printer status: {e}")

                # Process file/job information
                try:
                    file_info = {"job": data["current"]["job"], "progress": data["current"]["progress"]}
                    if file_info['job'] and file_info['job']['file']['name'] is not None:
                        filename = file_info['job']['file']['name']
                        progress = file_info.get('progress', {}).get('completion', 0)
                        self.logger.info(f"Print status update - File: {filename}, Progress: {progress}%")
                        try:
                            self.print_status_signal.emit(file_info)
                        except Exception as e:
                            self.logger.error(f"Error emitting print_status_signal: {e}")
                    else:
                        self.logger.debug("Emitting empty print status")
                        try:
                            self.print_status_signal.emit({"job": None, "progress": None})
                        except Exception as e:
                            self.logger.error(f"Error emitting empty print_status_signal: {e}")
                except Exception as e:
                    self.logger.error(f"Error processing file/job information: {e}")

                # Process temperature data
                try:
                    def temp(data, tool, temp):
                        try:
                            if tool in data["current"]["temps"][0]:
                                return data["current"]["temps"][0][tool][temp]
                        except:
                            pass
                        return 0

                    if data["current"]["temps"] and len(data["current"]["temps"]) > 0:
                        try:
                            temperatures = {
                                'tool0Actual': temp(data, "tool0", "actual"),
                                'tool0Target': temp(data, "tool0", "target"),
                                'tool1Actual': temp(data, "tool1", "actual"),
                                'tool1Target': temp(data, "tool1", "target"),
                                'bedActual': temp(data, "bed", "actual"),
                                'bedTarget': temp(data, "bed", "target"),
                                'chamberActual': temp(data, "chamber", "actual"),
                                'chamberTarget': temp(data, "chamber", "target"),
                                'filamentActual': temp(data, "filament", "actual"),
                                'filamentTarget': temp(data, "filament", "target")
                            }
                            self.logger.debug(f"Temperature update: Tool0: {temperatures['tool0Actual']}°C/{temperatures['tool0Target']}°C, "
                                            f"Tool1: {temperatures['tool1Actual']}°C/{temperatures['tool1Target']}°C, "
                                            f"Bed: {temperatures['bedActual']}°C/{temperatures['bedTarget']}°C, "
                                            f"Chamber: {temperatures['chamberActual']}°C/{temperatures['chamberTarget']}°C, "
                                            f"Filament: {temperatures['filamentActual']}°C/{temperatures['filamentTarget']}°C")
                            try:
                                self.temperatures_signal.emit(temperatures)
                            except Exception as e:
                                self.logger.error(f"Error emitting temperatures_signal: {e}")
                        except KeyError as e:
                            self.logger.warning(f"Error parsing temperature data: {e}")
                except Exception as e:
                    self.logger.error(f"Error processing temperature data: {e}")
                        
        except Exception as e:
            self.logger.error(f"Error processing WebSocket data: {e}")
            self.logger.exception("Full traceback:")

    def stop_websocket(self):
        """Properly stop the websocket connection and cleanup resources"""
        self.logger.info("Stopping WebSocket connection...")
        try:
            self.connection_active = False
            self.is_reconnecting = False
            
            # Cancel heartbeat timer
            if self.heartbeat_timer is not None:
                self.heartbeat_timer.cancel()
                self.heartbeat_timer = None
                self.logger.debug("Heartbeat timer cancelled")
            
            # Close websocket connection
            if self.ws is not None:
                try:
                    self.ws.close()
                    self.logger.debug("WebSocket connection closed")
                except Exception as e:
                    self.logger.warning(f"Error closing WebSocket: {e}")
            
            # Stop the thread if running
            if self.isRunning():
                self.logger.debug("Waiting for thread to finish...")
                if not self.wait(5000):  # 5 second timeout
                    self.logger.warning("Thread did not finish within timeout, forcing termination")
                    self.terminate()
                    self.wait(2000)
                    
            self.logger.info("WebSocket stopped successfully")
            
        except Exception as e:
            self.logger.error(f"Error stopping WebSocket: {e}")
            