#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FluentNao HTTP API Server

A Flask-based REST API server that exposes FluentNao functionality
for controlling Aldebaran NAO robots via HTTP requests.

Compatible with Python 2.7.9 and the pynaoqi SDK.

Author: Dave Snowdon
Date: June 18, 2025
"""

from __future__ import print_function
import os
import sys
import json
import time
import uuid
import threading
from datetime import datetime
from functools import wraps

# Add FluentNao paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

# Flask imports
from flask import Flask, request, jsonify, abort

# FluentNao imports
try:
    import naoutil.naoenv as naoenv
    import naoutil.broker as broker
    from fluentnao.nao import Nao
except ImportError as e:
    print("NAO library imports failed: {}".format(e))
    print("Make sure you're running this in the nao-bridge Docker container")
    sys.exit(1)

# Global variables
app = Flask(__name__)
nao_robot = None
active_operations = {}
operation_lock = threading.Lock()

# Configuration
API_VERSION = "1.0"
DEFAULT_DURATION = 1.5

class APIError(Exception):
    """Custom exception for API errors"""
    def __init__(self, message, code="UNKNOWN_ERROR", status_code=400):
        self.message = message
        self.code = code
        self.status_code = status_code
        super(APIError, self).__init__(message)

class OperationManager(object):
    """Manages asynchronous operations"""
    
    def __init__(self):
        self.operations = {}
        self.lock = threading.Lock()
    
    def create_operation(self, operation_type, description=""):
        """Create a new operation and return its ID"""
        operation_id = str(uuid.uuid4())
        with self.lock:
            self.operations[operation_id] = {
                'id': operation_id,
                'type': operation_type,
                'status': 'pending',
                'progress': 0.0,
                'description': description,
                'started_at': datetime.utcnow().isoformat() + 'Z',
                'completed_at': None,
                'error': None
            }
        return operation_id
    
    def update_operation(self, operation_id, status=None, progress=None, error=None):
        """Update operation status"""
        with self.lock:
            if operation_id in self.operations:
                op = self.operations[operation_id]
                if status:
                    op['status'] = status
                if progress is not None:
                    op['progress'] = progress
                if error:
                    op['error'] = error
                if status in ['completed', 'failed']:
                    op['completed_at'] = datetime.utcnow().isoformat() + 'Z'
    
    def get_operation(self, operation_id):
        """Get operation status"""
        with self.lock:
            return self.operations.get(operation_id)
    
    def get_active_operations(self):
        """Get all active operations"""
        with self.lock:
            return [op for op in self.operations.values() 
                   if op['status'] in ['pending', 'running']]
    
    def cleanup_completed(self, max_age_seconds=300):
        """Remove completed operations older than max_age_seconds"""
        cutoff = time.time() - max_age_seconds
        with self.lock:
            to_remove = []
            for op_id, op in self.operations.items():
                if (op['status'] in ['completed', 'failed'] and 
                    op['completed_at'] and 
                    time.mktime(time.strptime(op['completed_at'], '%Y-%m-%dT%H:%M:%SZ')) < cutoff):
                    to_remove.append(op_id)
            for op_id in to_remove:
                del self.operations[op_id]

# Global operation manager
operation_manager = OperationManager()

def init_robot():
    """Initialize connection to NAO robot"""
    global nao_robot
    
    nao_ip = os.environ.get("NAO_IP")
    if not nao_ip:
        raise APIError("NAO_IP environment variable not set", "ROBOT_NOT_CONFIGURED", 500)
    
    try:
        # Initialize broker and environment
        broker.Broker('fluentnao_api_broker', naoIp=nao_ip, naoPort=9559)
        env = naoenv.make_environment(None)
        
        # Create NAO instance
        nao_robot = Nao(env, None)
        nao_robot.set_duration(DEFAULT_DURATION)
        
        print("Connected to NAO robot at {}".format(nao_ip))
        return True
        
    except Exception as e:
        print("Failed to connect to NAO robot: {}".format(e))
        raise APIError("Failed to connect to robot", "ROBOT_NOT_CONNECTED", 502)

def require_robot(f):
    """Decorator to ensure robot is connected"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if nao_robot is None:
            raise APIError("Robot not connected", "ROBOT_NOT_CONNECTED", 502)
        return f(*args, **kwargs)
    return decorated_function

def validate_duration(duration):
    """Validate duration parameter"""
    if duration is not None:
        if not isinstance(duration, (int, float)) or duration <= 0:
            raise APIError("Duration must be a positive number", "INVALID_PARAMETER")
    return duration

def validate_range(value, min_val, max_val, name):
    """Validate numeric parameter range"""
    if value < min_val or value > max_val:
        raise APIError(
            "{} must be between {} and {}".format(name, min_val, max_val),
            "INVALID_PARAMETER"
        )
    return value

def create_response(data=None, message="Success", operation_id=None):
    """Create standardized API response"""
    response = {
        'success': True,
        'data': data or {},
        'message': message,
        'timestamp': datetime.utcnow().isoformat() + 'Z'
    }
    if operation_id:
        response['operation_id'] = operation_id
    return jsonify(response)

def create_error_response(error):
    """Create standardized error response"""
    response = {
        'success': False,
        'error': {
            'code': getattr(error, 'code', 'UNKNOWN_ERROR'),
            'message': str(error),
            'details': {}
        },
        'timestamp': datetime.utcnow().isoformat() + 'Z'
    }
    return jsonify(response), getattr(error, 'status_code', 500)

@app.errorhandler(APIError)
def handle_api_error(error):
    """Handle custom API errors"""
    return create_error_response(error)

@app.errorhandler(400)
def handle_bad_request(error):
    """Handle bad request errors"""
    api_error = APIError("Bad request", "BAD_REQUEST", 400)
    return create_error_response(api_error)

@app.errorhandler(404)
def handle_not_found(error):
    """Handle not found errors"""
    api_error = APIError("Endpoint not found", "NOT_FOUND", 404)
    return create_error_response(api_error)

@app.errorhandler(500)
def handle_internal_error(error):
    """Handle internal server errors"""
    api_error = APIError("Internal server error", "INTERNAL_ERROR", 500)
    return create_error_response(api_error)

# API Routes

@app.route('/api/v1/status', methods=['GET'])
@require_robot
def get_status():
    """Get robot and API status"""
    try:
        # Get basic robot info
        battery_level = nao_robot.get_battery_level()
        
        data = {
            'robot_connected': nao_robot is not None,
            'robot_ip': os.environ.get("NAO_IP", "unknown"),
            'battery_level': battery_level,
            'awake': nao_robot.is_awake(),
            'autonomous_life_state': nao_robot.autonomous_life_state(),
            'stiffness_enabled': True,  # Would need to check actual state
            'current_posture': 'Unknown',  # Would need to query robot
            'active_operations': operation_manager.get_active_operations(),
            'api_version': API_VERSION
        }
        
        return create_response(data, "Status retrieved successfully")
        
    except Exception as e:
        raise APIError("Failed to get robot status: {}".format(e), "STATUS_ERROR")

@app.route('/api/v1/robot/stiff', methods=['POST'])
@require_robot
def robot_stiff():
    """Enable robot stiffness"""
    try:
        data = request.get_json() or {}
        duration = validate_duration(data.get('duration'))
        
        if duration:
            nao_robot.set_duration(duration)
        
        nao_robot.stiff()
        
        return create_response(message="Robot stiffness enabled")
        
    except Exception as e:
        raise APIError("Failed to enable stiffness: {}".format(e), "STIFFNESS_ERROR")

@app.route('/api/v1/robot/relax', methods=['POST'])
@require_robot
def robot_relax():
    """Disable robot stiffness"""
    try:
        nao_robot.relax()
        return create_response(message="Robot stiffness disabled")
        
    except Exception as e:
        raise APIError("Failed to disable stiffness: {}".format(e), "STIFFNESS_ERROR")

@app.route('/api/v1/robot/rest', methods=['POST'])
@require_robot
def robot_rest():
    """Put robot in rest mode"""
    try:
        nao_robot.rest()
        return create_response(message="Robot in rest mode")
        
    except Exception as e:
        raise APIError("Failed to rest robot: {}".format(e), "REST_ERROR")

@app.route('/api/v1/robot/wake', methods=['POST'])
@require_robot
def robot_wake():
    """Wake up robot"""
    try:
        nao_robot.wake()
        return create_response(message="Robot woke up")
        
    except Exception as e:
        raise APIError("Failed to wake up robot: {}".format(e), "WAKE_ERROR")

@app.route('/api/v1/robot/autonomous_life/state', methods=['POST'])
@require_robot
def robot_autonomous_life_state():
    """Set autonomous life state. Valid values are: 'disabled', 'solitary', 'interactive', 'safeguard'"""
    valid_states = ['disabled', 'solitary', 'interactive', 'safeguard']
    try:
        data = request.get_json() or {}
        state = str(data.get('state', 'disabled')).strip()
        if state not in valid_states:
            raise APIError("Invalid autonomous life state: {}".format(state), "INVALID_PARAMETER")
        nao_robot.autonomous_life_set_state(state)
        return create_response(message="Autonomous life state set to: {}".format(state))
    except Exception as e:
        raise APIError("Failed to set autonomous life state: {}".format(e), "AUTONOMOUS_LIFE_ERROR")

@app.route('/api/v1/posture/stand', methods=['POST'])
@require_robot
def posture_stand():
    """Move robot to standing position"""
    try:
        data = request.get_json() or {}
        speed = data.get('speed', 0.5)
        variant = data.get('variant', 'Stand')
        
        speed = validate_range(speed, 0.1, 1.0, "Speed")
        
        if variant == 'StandInit':
            nao_robot.stand_init(speed)
        elif variant == 'StandZero':
            nao_robot.stand_zero(speed)
        else:
            nao_robot.stand(speed)
        
        return create_response(message="Robot moved to standing position")
        
    except Exception as e:
        raise APIError("Failed to stand: {}".format(e), "POSTURE_ERROR")

@app.route('/api/v1/posture/sit', methods=['POST'])
@require_robot
def posture_sit():
    """Move robot to sitting position"""
    try:
        data = request.get_json() or {}
        speed = data.get('speed', 0.5)
        variant = data.get('variant', 'Sit')
        
        speed = validate_range(speed, 0.1, 1.0, "Speed")
        
        if variant == 'SitRelax':
            nao_robot.sit_relax(speed)
        else:
            nao_robot.sit(speed)
        
        return create_response(message="Robot moved to sitting position")
        
    except Exception as e:
        raise APIError("Failed to sit: {}".format(e), "POSTURE_ERROR")

@app.route('/api/v1/posture/crouch', methods=['POST'])
@require_robot
def posture_crouch():
    """Move robot to crouching position"""
    try:
        data = request.get_json() or {}
        speed = data.get('speed', 0.5)
        speed = validate_range(speed, 0.1, 1.0, "Speed")
        
        nao_robot.crouch(speed)
        
        return create_response(message="Robot moved to crouching position")
        
    except Exception as e:
        raise APIError("Failed to crouch: {}".format(e), "POSTURE_ERROR")

@app.route('/api/v1/posture/lie', methods=['POST'])
@require_robot
def posture_lie():
    """Move robot to lying position"""
    try:
        data = request.get_json() or {}
        speed = data.get('speed', 0.5)
        position = data.get('position', 'back')
        
        speed = validate_range(speed, 0.1, 1.0, "Speed")
        
        if position == 'belly':
            nao_robot.lying_belly(speed)
        else:
            nao_robot.lying_back(speed)
        
        return create_response(message="Robot moved to lying position")
        
    except Exception as e:
        raise APIError("Failed to lie down: {}".format(e), "POSTURE_ERROR")

@app.route('/api/v1/arms/preset', methods=['POST'])
@require_robot
def arms_preset():
    """Control arms using preset positions"""
    try:
        data = request.get_json() or {}
        duration = validate_duration(data.get('duration'))
        position = data.get('position', 'up')
        arms = data.get('arms', 'both')
        offset = data.get('offset', {})
        
        if duration:
            nao_robot.set_duration(duration)
        
        # Apply offsets if provided
        shoulder_pitch_offset = offset.get('shoulder_pitch', 0)
        shoulder_roll_offset = offset.get('shoulder_roll', 0)
        
        # Execute arm movement based on position and arms selection
        if position == 'up':
            if arms in ['both', 'left']:
                nao_robot.arms.left_up(0, shoulder_pitch_offset, shoulder_roll_offset)
            if arms in ['both', 'right']:
                nao_robot.arms.right_up(0, shoulder_pitch_offset, shoulder_roll_offset)
        elif position == 'down':
            if arms in ['both', 'left']:
                nao_robot.arms.left_down(0, shoulder_pitch_offset, shoulder_roll_offset)
            if arms in ['both', 'right']:
                nao_robot.arms.right_down(0, shoulder_pitch_offset, shoulder_roll_offset)
        elif position == 'forward':
            if arms in ['both', 'left']:
                nao_robot.arms.left_forward(0, shoulder_pitch_offset, shoulder_roll_offset)
            if arms in ['both', 'right']:
                nao_robot.arms.right_forward(0, shoulder_pitch_offset, shoulder_roll_offset)
        elif position == 'out':
            nao_robot.arms.out(0, shoulder_pitch_offset)
        elif position == 'back':
            nao_robot.arms.back(0, shoulder_pitch_offset)
        else:
            raise APIError("Invalid arm position: {}".format(position), "INVALID_PARAMETER")
        
        nao_robot.go()
        
        return create_response(message="Arms moved to {} position".format(position))
        
    except Exception as e:
        raise APIError("Failed to move arms: {}".format(e), "MOVEMENT_ERROR")

@app.route('/api/v1/hands/position', methods=['POST'])
@require_robot
def hands_position():
    """Control hand opening and closing"""
    try:
        data = request.get_json() or {}
        duration = validate_duration(data.get('duration'))
        left_hand = data.get('left_hand')
        right_hand = data.get('right_hand')
        
        if duration:
            nao_robot.set_duration(duration)
        
        if left_hand == 'open':
            nao_robot.hands.left_open()
        elif left_hand == 'close':
            nao_robot.hands.left_close()
        
        if right_hand == 'open':
            nao_robot.hands.right_open()
        elif right_hand == 'close':
            nao_robot.hands.right_close()
        
        nao_robot.go()
        
        return create_response(message="Hand positions updated")
        
    except Exception as e:
        raise APIError("Failed to control hands: {}".format(e), "MOVEMENT_ERROR")

@app.route('/api/v1/head/position', methods=['POST'])
@require_robot
def head_position():
    """Control head positioning"""
    try:
        data = request.get_json() or {}
        duration = validate_duration(data.get('duration'))
        yaw = data.get('yaw', 0)
        pitch = data.get('pitch', 0)
        
        # Validate head movement ranges (approximate)
        yaw = validate_range(yaw, -120, 120, "Head yaw")
        pitch = validate_range(pitch, -40, 30, "Head pitch")
        
        if duration:
            nao_robot.set_duration(duration)
        
        if yaw != 0:
            if yaw > 0:
                nao_robot.head.right(0, yaw)
            else:
                nao_robot.head.left(0, abs(yaw))
        
        if pitch != 0:
            if pitch > 0:
                nao_robot.head.up(0, pitch)
            else:
                nao_robot.head.down(0, abs(pitch))
        
        nao_robot.go()
        
        return create_response(message="Head position updated")
        
    except Exception as e:
        raise APIError("Failed to move head: {}".format(e), "MOVEMENT_ERROR")

@app.route('/api/v1/speech/say', methods=['POST'])
@require_robot
def speech_say():
    """Make the robot speak"""
    try:
        data = request.get_json() or {}
        print(data)
        text = str(data.get('text', '')).strip()
        blocking = data.get('blocking', False)
        animated = data.get('animated', False)
        
        if not text:
            raise APIError("Text is required", "INVALID_PARAMETER")
        
        if animated:
            nao_robot.animate_say(text)
        elif blocking:
            nao_robot.say_and_block(text)
        else:
            nao_robot.say(text)
        
        return create_response(message="Speech command executed")
        
    except Exception as e:
        raise APIError("Failed to speak: {}".format(e), "SPEECH_ERROR")

@app.route('/api/v1/leds/set', methods=['POST'])
@require_robot
def leds_set():
    """Control LED colors"""
    try:
        data = request.get_json() or {}
        duration = validate_duration(data.get('duration'))
        leds = data.get('leds', {})
        
        if duration:
            nao_robot.set_duration(duration)
        
        # Convert hex colors to integer values
        def hex_to_int(hex_color):
            if hex_color.startswith('#'):
                hex_color = hex_color[1:]
            return int(hex_color, 16)
        
        if 'eyes' in leds:
            nao_robot.leds.eyes(hex_to_int(leds['eyes']))
        if 'ears' in leds:
            nao_robot.leds.ears(hex_to_int(leds['ears']))
        if 'chest' in leds:
            nao_robot.leds.chest(hex_to_int(leds['chest']))
        if 'feet' in leds:
            nao_robot.leds.feet(hex_to_int(leds['feet']))
        
        nao_robot.go()
        
        return create_response(message="LED colors updated")
        
    except Exception as e:
        raise APIError("Failed to set LEDs: {}".format(e), "LED_ERROR")

@app.route('/api/v1/leds/off', methods=['POST'])
@require_robot
def leds_off():
    """Turn off all LEDs"""
    try:
        nao_robot.leds.off()
        nao_robot.go()
        
        return create_response(message="All LEDs turned off")
        
    except Exception as e:
        raise APIError("Failed to turn off LEDs: {}".format(e), "LED_ERROR")

@app.route('/api/v1/walk/start', methods=['POST'])
@require_robot
def walk_start():
    """Start walking with specified parameters"""
    try:
        data = request.get_json() or {}
        x = data.get('x', 0.0)
        y = data.get('y', 0.0)
        theta = data.get('theta', 0.0)
        speed = data.get('speed', 0.5)
        
        # Validate walking parameters
        x = validate_range(x, -1.0, 1.0, "X velocity")
        y = validate_range(y, -1.0, 1.0, "Y velocity")
        theta = validate_range(theta, -1.0, 1.0, "Theta velocity")
        speed = validate_range(speed, 0.1, 1.0, "Speed")
        
        nao_robot.prep_walk()
        nao_robot.walk(x, y, theta, speed)
        
        return create_response(message="Walking started")
        
    except Exception as e:
        raise APIError("Failed to start walking: {}".format(e), "WALK_ERROR")

@app.route('/api/v1/walk/stop', methods=['POST'])
@require_robot
def walk_stop():
    """Stop current walking motion"""
    try:
        nao_robot.stop_walking()
        nao_robot.unprep_walk()
        
        return create_response(message="Walking stopped")
        
    except Exception as e:
        raise APIError("Failed to stop walking: {}".format(e), "WALK_ERROR")

@app.route('/api/v1/walk/preset', methods=['POST'])
@require_robot
def walk_preset():
    """Use predefined walking patterns"""
    try:
        data = request.get_json() or {}
        action = data.get('action', 'forward')
        duration = data.get('duration', 3.0)
        speed = data.get('speed', 1.0)
        
        duration = validate_duration(duration)
        speed = validate_range(speed, 0.1, 1.0, "Speed")
        
        if action == 'forward':
            nao_robot.walk_forward(speed, duration)
        elif action == 'backward':
            nao_robot.walk_back(speed, duration)
        elif action == 'turn_left':
            nao_robot.turn_left(speed, duration)
        elif action == 'turn_right':
            nao_robot.turn_right(speed, duration)
        else:
            raise APIError("Invalid walk action: {}".format(action), "INVALID_PARAMETER")
        
        return create_response(message="Walk {} executed".format(action))
        
    except Exception as e:
        raise APIError("Failed to execute walk preset: {}".format(e), "WALK_ERROR")

@app.route('/api/v1/sensors/sonar', methods=['GET'])
@require_robot
def sensors_sonar():
    """Get sonar sensor readings"""
    try:
        nao_robot.prep_sonar()
        readings = nao_robot.read_sonar()
        
        data = {
            'left': readings[0],
            'right': readings[1],
            'units': 'meters',
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        }
        
        return create_response(data, "Sonar readings retrieved")
        
    except Exception as e:
        raise APIError("Failed to read sonar: {}".format(e), "SENSOR_ERROR")

@app.route('/api/v1/config/duration', methods=['POST'])
@require_robot
def config_duration():
    """Set global movement duration"""
    try:
        data = request.get_json() or {}
        duration = data.get('duration', DEFAULT_DURATION)
        duration = validate_duration(duration)
        
        nao_robot.set_duration(duration)
        
        return create_response(
            {'duration': duration}, 
            "Global duration set to {} seconds".format(duration)
        )
        
    except Exception as e:
        raise APIError("Failed to set duration: {}".format(e), "CONFIG_ERROR")

@app.route('/api/v1/operations', methods=['GET'])
def get_operations():
    """List active operations"""
    try:
        active_ops = operation_manager.get_active_operations()
        return create_response({'active_operations': active_ops}, "Operations retrieved")
        
    except Exception as e:
        raise APIError("Failed to get operations: {}".format(e), "OPERATION_ERROR")

@app.route('/api/v1/operations/<operation_id>', methods=['GET'])
def get_operation(operation_id):
    """Get status of specific operation"""
    try:
        operation = operation_manager.get_operation(operation_id)
        if not operation:
            raise APIError("Operation not found", "NOT_FOUND", 404)
        
        return create_response(operation, "Operation status retrieved")
        
    except Exception as e:
        raise APIError("Failed to get operation: {}".format(e), "OPERATION_ERROR")

if __name__ == '__main__':
    print("FluentNao HTTP API Server v{}".format(API_VERSION))
    print("Initializing robot connection...")
    
    try:
        init_robot()
        print("Robot connected successfully!")
        print("Starting API server on http://0.0.0.0:3000")
        app.run(host='0.0.0.0', port=3000, debug=False)
        
    except Exception as e:
        print("Failed to start server: {}".format(e))
        sys.exit(1)

