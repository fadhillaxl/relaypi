#!/usr/bin/env python3
"""
FastAPI Relay Controller for 4-Port Relay Board
Based on the relay control logic from script1.py
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Set
import RPi.GPIO as GPIO
import time
import asyncio
import logging
import json
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Relay configuration
RELAY_PINS = [2, 3, 4, 17]
RELAY_NAMES = {
    1: {"pin": 2, "name": "Relay 1"},
    2: {"pin": 3, "name": "Relay 2"},
    3: {"pin": 4, "name": "Relay 3"},
    4: {"pin": 17, "name": "Relay 4"}
}

# Global state tracking
relay_states = {i: False for i in range(1, 5)}  # False = OFF (HIGH), True = ON (LOW)
emergency_stop = False
gpio_initialized = False

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)

    async def send_personal_message(self, message: str, websocket: WebSocket):
        try:
            await websocket.send_text(message)
        except:
            self.disconnect(websocket)

    async def broadcast(self, message: str):
        disconnected = set()
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except:
                disconnected.add(connection)
        
        # Remove disconnected connections
        for connection in disconnected:
            self.disconnect(connection)

manager = ConnectionManager()

class RelayState(BaseModel):
    relay_id: int = Field(..., ge=1, le=4, description="Relay ID (1-4)")
    state: bool = Field(..., description="Relay state (True=ON, False=OFF)")

class RelayControl(BaseModel):
    relay_id: int = Field(..., ge=1, le=4, description="Relay ID (1-4)")
    duration: Optional[float] = Field(None, ge=0.1, le=3600, description="Duration in seconds (optional)")

class SequenceStep(BaseModel):
    relay_id: int = Field(..., ge=1, le=4, description="Relay ID (1-4)")
    state: bool = Field(..., description="Relay state (True=ON, False=OFF)")
    duration: float = Field(..., ge=0.1, le=60, description="Duration in seconds")

class RelaySequence(BaseModel):
    steps: List[SequenceStep] = Field(..., min_items=1, max_items=20)
    repeat: int = Field(1, ge=1, le=10, description="Number of repetitions")

def init_gpio():
    """Initialize GPIO settings"""
    global gpio_initialized
    try:
        GPIO.setmode(GPIO.BCM)
        
        # Setup all relay pins as outputs and set to HIGH (OFF state)
        for pin in RELAY_PINS:
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.HIGH)  # HIGH = OFF for relay
            
        gpio_initialized = True
        logger.info("GPIO initialized successfully")
        
        # Initialize relay states
        for i in range(1, 5):
            relay_states[i] = False  # All relays OFF
            
    except Exception as e:
        logger.error(f"Failed to initialize GPIO: {e}")
        raise

def cleanup_gpio():
    """Cleanup GPIO settings"""
    global gpio_initialized
    try:
        if gpio_initialized:
            GPIO.cleanup()
            gpio_initialized = False
            logger.info("GPIO cleaned up successfully")
    except Exception as e:
        logger.error(f"Error during GPIO cleanup: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle"""
    # Startup
    init_gpio()
    yield
    # Shutdown
    cleanup_gpio()

# Create FastAPI app with lifespan management
app = FastAPI(
    title="4-Port Relay Controller API",
    description="REST API for controlling a 4-port relay board on Raspberry Pi",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

async def broadcast_status():
    """Broadcast current relay status to all WebSocket connections"""
    try:
        # Build relays data step by step to avoid JSON serialization issues
        relays_data = {}
        for relay_id in RELAY_NAMES.keys():
            relays_data[str(relay_id)] = {
                "name": RELAY_NAMES[relay_id]["name"],
                "pin": RELAY_NAMES[relay_id]["pin"],
                "state": relay_states[relay_id],
                "status": "ON" if relay_states[relay_id] else "OFF"
            }
        
        status_data = {
            "timestamp": time.time(),
            "relays": relays_data,
            "emergency_stop": emergency_stop,
            "gpio_initialized": gpio_initialized
        }
        await manager.broadcast(json.dumps(status_data))
    except Exception as e:
        logger.error(f"Error in broadcast_status: {e}")

def set_relay_state(relay_id: int, state: bool) -> bool:
    """Set individual relay state"""
    if not gpio_initialized:
        raise HTTPException(status_code=500, detail="GPIO not initialized")
    
    if relay_id not in RELAY_NAMES:
        raise HTTPException(status_code=400, detail=f"Invalid relay ID: {relay_id}")
    
    try:
        pin = RELAY_NAMES[relay_id]["pin"]
        # For relay boards: LOW = ON, HIGH = OFF
        gpio_state = GPIO.LOW if state else GPIO.HIGH
        GPIO.output(pin, gpio_state)
        relay_states[relay_id] = state
        
        logger.info(f"Relay {relay_id} set to {'ON' if state else 'OFF'}")
        
        # Broadcast status change to WebSocket clients
        asyncio.create_task(broadcast_status())
        
        return True
        
    except Exception as e:
        logger.error(f"Error setting relay {relay_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to control relay: {e}")

@app.get("/")
async def root():
    """API Information"""
    return {
        "name": "4-Port Relay Controller API",
        "version": "1.0.0",
        "description": "FastAPI-based relay controller for 4-port relay board",
        "features": [
            "RESTful API for relay control",
            "Real-time WebSocket status updates",
            "CORS enabled for cross-origin requests",
            "Emergency stop functionality",
            "Relay sequencing and pulsing"
        ],
        "endpoints": {
            "status": "GET /status - Get current relay status",
            "status_ws": "WS /status/ws - Real-time relay status via WebSocket",
            "relay_on": "POST /relay/on - Turn relay on",
            "relay_off": "POST /relay/off - Turn relay off", 
            "relay_toggle": "POST /relay/toggle - Toggle relay state",
            "all_on": "POST /relay/all/on - Turn all relays on",
            "all_off": "POST /relay/all/off - Turn all relays off",
            "pulse": "POST /relay/pulse - Pulse relay",
            "sequence": "POST /relay/sequence - Run relay sequence",
            "emergency_stop": "POST /emergency/stop - Emergency stop all relays"
        },
        "websocket_usage": {
            "url": "ws://localhost:8002/status/ws",
            "description": "Connect to receive real-time relay status updates",
            "message_format": "JSON with timestamp, relays, emergency_stop, and gpio_initialized fields"
        },
        "cors": {
            "enabled": True,
            "description": "Cross-Origin Resource Sharing enabled for all origins",
            "note": "Web applications can access this API from any domain"
        }
    }

@app.get("/status")
async def get_status() -> Dict:
    """Get current status of all relays"""
    return {
        "timestamp": time.time(),
        "relays": {
            str(relay_id): {
                "name": RELAY_NAMES[relay_id]["name"],
                "pin": RELAY_NAMES[relay_id]["pin"],
                "state": relay_states[relay_id],
                "status": "ON" if relay_states[relay_id] else "OFF"
            }
            for relay_id in RELAY_NAMES.keys()
        },
        "emergency_stop": emergency_stop,
        "gpio_initialized": gpio_initialized
    }

@app.websocket("/status/ws")
async def websocket_status(websocket: WebSocket):
    await manager.connect(websocket)
    
    # Send initial status
    try:
        # Debug: Let's build this step by step to identify the issue
        relays_data = {}
        for relay_id in RELAY_NAMES.keys():
            logger.info(f"Processing relay_id: {relay_id}, type: {type(relay_id)}")
            logger.info(f"RELAY_NAMES[{relay_id}]: {RELAY_NAMES[relay_id]}")
            logger.info(f"relay_states[{relay_id}]: {relay_states[relay_id]}")
            
            relays_data[str(relay_id)] = {
                "name": RELAY_NAMES[relay_id]["name"],
                "pin": RELAY_NAMES[relay_id]["pin"],
                "state": relay_states[relay_id],
                "status": "ON" if relay_states[relay_id] else "OFF"
            }
        
        initial_status = {
            "timestamp": time.time(),
            "relays": relays_data,
            "emergency_stop": emergency_stop,
            "gpio_initialized": gpio_initialized
        }
        
        logger.info(f"Initial status structure: {initial_status}")
        await manager.send_personal_message(json.dumps(initial_status), websocket)
    except Exception as e:
        logger.error(f"Error in WebSocket initial status: {e}")
        await manager.send_personal_message(json.dumps({"error": str(e)}), websocket)
    
    try:
        while True:
            # Keep connection alive and handle any incoming messages
            data = await websocket.receive_text()
            # Echo back any received messages (optional)
            await manager.send_personal_message(f"Echo: {data}", websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.post("/relay/on")
async def turn_relay_on(relay: RelayControl):
    """Turn a specific relay ON"""
    set_relay_state(relay.relay_id, True)
    
    # If duration is specified, turn off after delay
    if relay.duration:
        async def turn_off_after_delay():
            await asyncio.sleep(relay.duration)
            set_relay_state(relay.relay_id, False)
        
        # Run in background
        asyncio.create_task(turn_off_after_delay())
        
        return {
            "message": f"Relay {relay.relay_id} turned ON for {relay.duration} seconds",
            "relay_id": relay.relay_id,
            "state": True,
            "duration": relay.duration
        }
    
    return {
        "message": f"Relay {relay.relay_id} turned ON",
        "relay_id": relay.relay_id,
        "state": True
    }

class RelayOff(BaseModel):
    relay_id: int = Field(..., ge=1, le=4, description="Relay ID (1-4)")

@app.post("/relay/off")
async def turn_relay_off(relay: RelayOff):
    """Turn a specific relay OFF"""
    set_relay_state(relay.relay_id, False)
    
    return {
        "message": f"Relay {relay.relay_id} turned OFF",
        "relay_id": relay.relay_id,
        "state": False
    }

class RelayToggle(BaseModel):
    relay_id: int = Field(..., ge=1, le=4, description="Relay ID (1-4)")

@app.post("/relay/toggle")
async def toggle_relay(relay: RelayToggle):
    """Toggle a specific relay state"""
    current_state = relay_states[relay.relay_id]
    new_state = not current_state
    set_relay_state(relay.relay_id, new_state)
    
    return {
        "message": f"Relay {relay.relay_id} toggled to {'ON' if new_state else 'OFF'}",
        "relay_id": relay.relay_id,
        "previous_state": current_state,
        "new_state": new_state
    }

@app.post("/relay/pulse")
async def pulse_relay(relay: RelayControl):
    """Pulse a relay (turn ON then OFF after duration)"""
    if not relay.duration:
        raise HTTPException(status_code=400, detail="Duration is required for pulse operation")
    
    # Turn relay ON
    set_relay_state(relay.relay_id, True)
    
    # Wait for duration
    await asyncio.sleep(relay.duration)
    
    # Turn relay OFF
    set_relay_state(relay.relay_id, False)
    
    return {
        "message": f"Relay {relay.relay_id} pulsed for {relay.duration} seconds",
        "relay_id": relay.relay_id,
        "duration": relay.duration
    }

@app.post("/sequence")
async def run_sequence(sequence: RelaySequence, background_tasks: BackgroundTasks):
    """Run a sequence of relay operations"""
    
    async def execute_sequence():
        try:
            for repeat in range(sequence.repeat):
                logger.info(f"Starting sequence iteration {repeat + 1}/{sequence.repeat}")
                
                for step in sequence.steps:
                    set_relay_state(step.relay_id, step.state)
                    await asyncio.sleep(step.duration)
                
                logger.info(f"Completed sequence iteration {repeat + 1}/{sequence.repeat}")
                
        except Exception as e:
            logger.error(f"Error in sequence execution: {e}")
    
    # Run sequence in background
    background_tasks.add_task(execute_sequence)
    
    return {
        "message": f"Sequence started with {len(sequence.steps)} steps, {sequence.repeat} repetitions",
        "steps": len(sequence.steps),
        "repetitions": sequence.repeat,
        "estimated_duration": sum(step.duration for step in sequence.steps) * sequence.repeat
    }

@app.post("/all/on")
async def turn_all_on():
    """Turn all relays ON"""
    for relay_id in RELAY_NAMES.keys():
        set_relay_state(relay_id, True)
    
    return {
        "message": "All relays turned ON",
        "relays": list(RELAY_NAMES.keys())
    }

@app.post("/all/off")
async def turn_all_off():
    """Turn all relays OFF"""
    for relay_id in RELAY_NAMES.keys():
        set_relay_state(relay_id, False)
    
    return {
        "message": "All relays turned OFF",
        "relays": list(RELAY_NAMES.keys())
    }

@app.post("/emergency/stop")
async def emergency_stop():
    """Emergency stop - turn all relays OFF immediately"""
    try:
        for relay_id in RELAY_NAMES.keys():
            set_relay_state(relay_id, False)
        
        return {
            "message": "EMERGENCY STOP - All relays turned OFF",
            "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"Emergency stop failed: {e}")
        raise HTTPException(status_code=500, detail="Emergency stop failed")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)