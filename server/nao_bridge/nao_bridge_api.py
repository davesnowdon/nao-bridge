#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
HTTP API Server

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
import base64
import time
import uuid
import threading
from datetime import datetime
from functools import wraps
from PIL import Image
import StringIO

from animations import execute_animation, get_available_animations

# Add FluentNao paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

# Flask imports
from flask import Flask, request, jsonify, abort, send_file, Response

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

@app.after_request
def after_request(response):
    """Add CORS headers to all responses"""
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

@app.route('/api/v1/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    """Handle preflight OPTIONS requests for all API routes"""
    response = jsonify({})
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# Configuration
API_VERSION = "1.0"
DEFAULT_DURATION = 1.5
# Camera resolution mappings
RESOLUTION_MAP = {
    'qqqqvga': 8,  # 40x30
    'qqvga': 7,    # 80x60
    'qqqvga': 0,   # 160x120
    'qvga': 1,     # 320x240
    'vga': 2,      # 640x480
    'hvga': 3,     # 1280x960
}

CAMERA_MAP = {
    'top': 0,
    'bottom': 1,
}

RGB_COLORSPACE = 11

VALID_CHAINS = ['Head', 'Body', 'LArm', 'RArm', 'LLeg', 'RLeg']

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
            'current_posture': nao_robot.get_posture(),
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

@app.route('/api/v1/robot/joints/<chain>/angles', methods=['GET'])
@require_robot
def get_joint_angles(chain):
    """
    Get current joint angles for a specified chain.
    Chain can be one of: Head, Body, LArm, RArm, LLeg, RLeg
    """
    try:
        chain = str(chain)
        if chain not in VALID_CHAINS:
            raise APIError("Invalid chain: {}. Must be one of: {}".format(chain, ', '.join(VALID_CHAINS)), "INVALID_PARAMETER")

        # Use ALMotion proxy to get joint names and angles for the chain
        joint_names = nao_robot.env.motion.getBodyNames(chain)
        if not joint_names:
            raise APIError("No joints found for chain: {}".format(chain), "JOINT_ERROR")

        angles = nao_robot.env.motion.getAngles(joint_names, True)
        joint_data = dict(zip(joint_names, angles))
        return create_response(
            {'chain': chain, 'joints': joint_data},
            "Joint angles for chain '{}' retrieved".format(chain)
        )
    except Exception as e:
        raise APIError("Failed to get joint angles: {}".format(e), "JOINT_ERROR")

@app.route('/api/v1/robot/joints/<chain>/names', methods=['GET'])
@require_robot
def get_joint_names(chain):
    """
    Get joint names for a specified chain.
    Chain can be one of: Head, Body, LArm, RArm, LLeg, RLeg
    """
    try:
        chain = str(chain)
        if chain not in VALID_CHAINS:
            raise APIError("Invalid chain: {}. Must be one of: {}".format(chain, ', '.join(VALID_CHAINS)), "INVALID_PARAMETER")

        # Use ALMotion proxy to get joint names for the chain
        joint_names = nao_robot.env.motion.getBodyNames(chain)
        if not joint_names:
            raise APIError("No joints found for chain: {}".format(chain), "JOINT_ERROR")

        return create_response(
            {'chain': chain, 'joint_names': joint_names},
            "Joint names for chain '{}' retrieved".format(chain)
        )
    except Exception as e:
        raise APIError("Failed to get joint names: {}".format(e), "JOINT_ERROR")

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

def get_camera_image(nao_env, camera_id=0, resolution=1, colorspace=11, fps=5):
    """Core image capture function using current API"""
    subscriber_handle = nao_env.videoDevice.subscribeCamera("api_client", camera_id, resolution, colorspace, fps)
    try:
        image_container = nao_env.videoDevice.getImageRemote(subscriber_handle)
        if image_container is None:
            raise RuntimeError("Failed to get image from camera")
        
        return {
            'width': image_container[0],
            'height': image_container[1],
            'channels': image_container[2],
            'image_data': image_container[6]
        }
    finally:
        nao_env.videoDevice.unsubscribe(subscriber_handle)

def convert_to_jpeg(image_data, width, height, channels, quality=85):
    """Convert raw RGB data to JPEG bytes"""
    # Convert raw bytes to PIL Image
    if channels == 3:  # RGB
        mode = 'RGB'
    elif channels == 1:  # Grayscale
        mode = 'L'
    else:
        raise ValueError("Unsupported number of channels: {}".format(channels))
    
    # Create PIL Image from raw data
    img = Image.frombuffer(mode, (width, height), image_data, 'raw', mode, 0, 1)
    
    # Convert to JPEG
    output = StringIO.StringIO()
    img.save(output, format='JPEG', quality=quality)
    jpeg_data = output.getvalue()
    output.close()
    
    return jpeg_data

@app.route('/api/v1/vision/<camera>/<resolution>', methods=['GET'])
@require_robot
def vision_camera(camera, resolution):
    """Get camera image as JPEG"""
    try:
        # Validate camera parameter
        if camera not in CAMERA_MAP:
            raise APIError("Invalid camera: {}. Must be 'top' or 'bottom'".format(camera), "INVALID_PARAMETER", 400)
        
        # Validate resolution parameter
        if resolution not in RESOLUTION_MAP:
            raise APIError("Invalid resolution: {}. Must be one of: {}".format(resolution, ', '.join(RESOLUTION_MAP.keys())), "INVALID_PARAMETER", 400)
        
        camera_id = CAMERA_MAP[camera]
        resolution_id = RESOLUTION_MAP[resolution]
        
        # Set active camera if different from default
        if camera_id != 0:
            nao_robot.env.videoDevice.setActiveCamera(camera_id)
        
        # Capture image
        image_data = get_camera_image(nao_robot.env, camera_id, resolution_id, RGB_COLORSPACE)
        format_param = request.args.get('format', 'jpeg').lower()
        
        if format_param == 'jpeg':
            # Convert raw RGB data to JPEG
            print("Converting raw RGB data to JPEG")
            jpeg_data = convert_to_jpeg(
                image_data['image_data'],
                image_data['width'],
                image_data['height'],
                image_data['channels']
            )
            
            # Return JPEG response
            response = Response(jpeg_data)
            response.headers['Content-Type'] = 'image/jpeg'
            response.headers['X-Image-Width'] = str(image_data['width'])
            response.headers['X-Image-Height'] = str(image_data['height'])
            response.headers['X-Image-Channels'] = str(image_data['channels'])
            return response
            
            
        elif format_param == 'json':
            # Return JSON with base64 encoded image
            print("Returning JSON with base64 encoded image")
            return create_response({
                'camera': camera,
                'resolution': resolution,
                'colorspace': RGB_COLORSPACE,
                'width': image_data['width'],
                'height': image_data['height'],
                'channels': image_data['channels'],
                'image_data': base64.b64encode(image_data['image_data']),
                'encoding': 'base64'
            }, "Image captured successfully")

        elif format_param == 'raw':
            # Return raw binary data
            print("Returning raw image data")
            response = Response(image_data['image_data'])
            response.headers['Content-Type'] = 'application/octet-stream'
            response.headers['X-Image-Width'] = str(image_data['width'])
            response.headers['X-Image-Height'] = str(image_data['height'])
            response.headers['X-Image-Channels'] = str(image_data['channels'])
            return response
            
        else:
            raise APIError("Invalid format: {}. Must be 'jpeg', 'json', or 'raw'".format(format_param), "INVALID_PARAMETER", 400)
    
    except APIError:
        raise
        
    except Exception as e:
        raise APIError("Failed to capture image: {}".format(e), "VISION_ERROR")

@app.route('/api/v1/vision/resolutions', methods=['GET'])
def get_available_resolutions():
    """Get list of available camera resolutions"""
    resolutions = []
    for name, id_val in RESOLUTION_MAP.items():
        # Map back to dimensions for clarity
        dimensions = {
            8: "40x30", 7: "80x60", 0: "160x120",
            1: "320x240", 2: "640x480", 3: "1280x960"
        }
        resolutions.append({
            'name': name,
            'id': id_val,
            'dimensions': dimensions[id_val]
        })
    
    return create_response({
        'resolutions': sorted(resolutions, key=lambda x: x['id']),
        'cameras': list(CAMERA_MAP.keys()),
        'colorspaces': ['rgb', 'yuv', 'bgr']
    }, "Available camera options")

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

@app.route('/api/v1/behaviour/execute', methods=['POST'])
@require_robot
def execute_behaviour():
    """Execute a behavior on the robot"""
    try:
        data = request.get_json() or {}
        behaviour = str(data.get('behaviour'))
        blocking = data.get('blocking', True)
        
        if not behaviour:
            raise APIError("Behaviour name is required", "INVALID_PARAMETER")
        
        print("Executing behaviour: {}".format(behaviour))
        print("Blocking: {}".format(blocking))
        
        # Execute the behaviour using the NAO robot's behavior manager
        if blocking:
            nao_robot.env.behaviourManager.runBehavior(behaviour)
        else:
            nao_robot.env.behaviourManager.startBehavior(behaviour)
        
        return create_response(
            {'behaviour': behaviour, 'blocking': blocking},
            "Behaviour '{}' executed successfully".format(behaviour)
        )
        
    except Exception as e:
        raise APIError("Failed to execute behaviour: {}".format(e), "BEHAVIOUR_ERROR")

@app.route('/api/v1/behaviour/<behaviour_type>', methods=['GET'])
@require_robot
def list_behaviours(behaviour_type):
    """Get list of all installed behaviours on the robot"""
    try:
        if behaviour_type == 'installed':
            # Get all installed behaviours from the behavior manager
            behaviours = nao_robot.env.behaviourManager.getInstalledBehaviors()
        elif behaviour_type == 'default':
            behaviours = nao_robot.env.behaviourManager.getDefaultBehaviors()
        elif behaviour_type == 'running':
            behaviours = nao_robot.env.behaviourManager.getRunningBehaviors()
        else:
            raise APIError("Invalid behaviour type: {}".format(behaviour_type), "INVALID_PARAMETER")

        # Convert to list if it's not already
        if not isinstance(behaviours, list):
            behaviours = list(behaviours)
        
        return create_response(
            {'behaviours': behaviours},
            "Available behaviours retrieved"
        )
        
    except Exception as e:
        raise APIError("Failed to get behaviours: {}".format(e), "BEHAVIOUR_ERROR")

@app.route('/api/v1/behaviour/default', methods=['POST'])
@require_robot
def set_behaviour_default():
    """Set a behaviour as default"""
    try:
        data = request.get_json() or {}
        behaviour_name = str(data.get('behaviour'))
        default = data.get('default', True)

        if not behaviour_name:
            raise APIError("Behaviour name is required", "INVALID_PARAMETER")

        if default:
            nao_robot.env.behaviourManager.addDefaultBehavior(behaviour_name)
        else:
            nao_robot.env.behaviourManager.removeDefaultBehavior(behaviour_name)
        
        return create_response(
            {'behaviour': behaviour_name},
            "Behaviour '{}' set as {} default".format(behaviour_name, "a" if default else "not a")
        )
    except Exception as e:
        raise APIError("Failed to set behaviour default: {}".format(e), "BEHAVIOUR_ERROR")

@app.route('/api/v1/animations/execute', methods=['POST'])
@require_robot
def execute_named_animation():
    """Execute predefined complex animations"""
    try:
        data = request.get_json() or {}
        animation = data.get('animation')
        parameters = data.get('parameters', {})

        print("Executing animation: {}".format(animation))
        print("Parameters: {}".format(parameters))
        
        if not animation:
            raise APIError("Animation name is required", "INVALID_PARAMETER")
            
        # Handle duration multiplier
        duration_multiplier = parameters.get('duration_multiplier', 1.0)
        if duration_multiplier != 1.0:
            current_duration = nao_robot.globalDuration
            nao_robot.set_duration(current_duration * duration_multiplier)
            
        # Execute the animation
        execute_animation(nao_robot, animation, parameters)
            
        # Reset duration if it was modified
        if duration_multiplier != 1.0:
            nao_robot.set_duration(current_duration)
            
        return create_response(
            {'animation': animation, 'parameters': parameters},
            "Animation '{}' executed successfully".format(animation)
        )
            
    except ValueError as e:
        raise APIError(str(e), "INVALID_ANIMATION")
    except Exception as e:
        raise APIError("Failed to execute animation: {}".format(e), "ANIMATION_ERROR")
    
@app.route('/api/v1/animations/list', methods=['GET'])
def list_animations():
    """Get list of available animations"""
    try:
        animations = get_available_animations()
        return create_response(
            {'animations': animations},
            "Available animations retrieved"
        )
    except Exception as e:
        raise APIError("Failed to get animations: {}".format(e), "ANIMATION_ERROR")
    
@app.route('/api/v1/animations/sequence', methods=['POST'])
@require_robot
def execute_sequence():
    """Execute a sequence of movements"""
    try:
        data = request.get_json() or {}
        sequence = data.get('sequence', [])
        blocking = data.get('blocking', True)
            
        if not sequence:
            raise APIError("Sequence is required", "INVALID_PARAMETER")
            
        executed_steps = []
            
        for i, step in enumerate(sequence):
            step_type = step.get('type')
            action = step.get('action')
                
            try:
                if step_type == 'posture':
                    _execute_posture_step(nao_robot, step)
                elif step_type == 'speech':
                    _execute_speech_step(nao_robot, step)
                elif step_type == 'arms':
                    _execute_arms_step(nao_robot, step)
                elif step_type == 'hands':
                    _execute_hands_step(nao_robot, step)
                elif step_type == 'head':
                    _execute_head_step(nao_robot, step)
                elif step_type == 'leds':
                    _execute_leds_step(nao_robot, step)
                elif step_type == 'wait':
                    duration = step.get('duration', 1.0)
                    nao_robot.wait(duration)
                else:
                    raise APIError("Unknown step type: {}".format(step_type), "INVALID_PARAMETER")
                    
                executed_steps.append({
                    'step': i + 1,
                    'type': step_type,
                    'action': action,
                    'status': 'completed'
                })
                    
            except Exception as e:
                executed_steps.append({
                    'step': i + 1,
                    'type': step_type,
                    'action': action,
                    'status': 'failed',
                    'error': str(e)
                })
                if blocking:
                    raise APIError(
                        "Sequence failed at step {}: {}".format(i + 1, e),
                        "SEQUENCE_ERROR"
                    )
            
        return create_response(
            {'executed_steps': executed_steps},
            "Sequence executed successfully"
        )
        
    except Exception as e:
        raise APIError("Failed to execute sequence: {}".format(e), "SEQUENCE_ERROR")

def _execute_posture_step(nao_robot, step):
    """Execute a posture step in a sequence"""
    action = step.get('action')
    duration = step.get('duration')
    speed = step.get('speed', 0.5)
    
    if duration:
        nao_robot.set_duration(duration)
    
    if action == 'stand':
        nao_robot.stand(speed)
    elif action == 'sit':
        nao_robot.sit(speed)
    elif action == 'crouch':
        nao_robot.crouch(speed)
    else:
        raise ValueError("Unknown posture action: {}".format(action))

def _execute_speech_step(nao_robot, step):
    """Execute a speech step in a sequence"""
    action = step.get('action')
    text = step.get('text', '')
    blocking = step.get('blocking', False)
    animated = step.get('animated', False)
    
    if action == 'say':
        if animated:
            nao_robot.animate_say(text)
        elif blocking:
            nao_robot.say_and_block(text)
        else:
            nao_robot.say(text)
    else:
        raise ValueError("Unknown speech action: {}".format(action))

def _execute_arms_step(nao_robot, step):
    """Execute an arms step in a sequence"""
    action = step.get('action')
    duration = step.get('duration')
    
    if duration:
        nao_robot.set_duration(duration)
    
    if action == 'preset':
        position = step.get('position', 'up')
        arms = step.get('arms', 'both')
        
        if position == 'up':
            if arms in ['both', 'left']:
                nao_robot.arms.left_up()
            if arms in ['both', 'right']:
                nao_robot.arms.right_up()
        elif position == 'down':
            if arms in ['both', 'left']:
                nao_robot.arms.left_down()
            if arms in ['both', 'right']:
                nao_robot.arms.right_down()
        elif position == 'forward':
            if arms in ['both', 'left']:
                nao_robot.arms.left_forward()
            if arms in ['both', 'right']:
                nao_robot.arms.right_forward()
        elif position == 'out':
            nao_robot.arms.out()
        
        nao_robot.go()
    else:
        raise ValueError("Unknown arms action: {}".format(action))

def _execute_hands_step(nao_robot, step):
    """Execute a hands step in a sequence"""
    action = step.get('action')
    duration = step.get('duration')
    
    if duration:
        nao_robot.set_duration(duration)
    
    if action == 'position':
        left_hand = step.get('left_hand')
        right_hand = step.get('right_hand')
        
        if left_hand == 'open':
            nao_robot.hands.left_open()
        elif left_hand == 'close':
            nao_robot.hands.left_close()
        
        if right_hand == 'open':
            nao_robot.hands.right_open()
        elif right_hand == 'close':
            nao_robot.hands.right_close()
        
        nao_robot.go()
    else:
        raise ValueError("Unknown hands action: {}".format(action))

def _execute_head_step(nao_robot, step):
    """Execute a head step in a sequence"""
    action = step.get('action')
    duration = step.get('duration')
    
    if duration:
        nao_robot.set_duration(duration)
    
    if action == 'position':
        yaw = step.get('yaw', 0)
        pitch = step.get('pitch', 0)
        
        if yaw > 0:
            nao_robot.head.right(0, yaw)
        elif yaw < 0:
            nao_robot.head.left(0, abs(yaw))
        
        if pitch > 0:
            nao_robot.head.up(0, pitch)
        elif pitch < 0:
            nao_robot.head.down(0, abs(pitch))
        
        nao_robot.go()
    else:
        raise ValueError("Unknown head action: {}".format(action))

def _execute_leds_step(nao_robot, step):
    """Execute a LEDs step in a sequence"""
    action = step.get('action')
    duration = step.get('duration')
    
    if duration:
        nao_robot.set_duration(duration)
    
    if action == 'set':
        leds = step.get('leds', {})
        
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
    elif action == 'off':
        nao_robot.leds.off()
        nao_robot.go()
    else:
        raise ValueError("Unknown LEDs action: {}".format(action))

