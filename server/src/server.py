#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FluentNao API Server with Extensions

Main server file that includes all endpoints and extensions.
This is the complete API server implementation.

Author: Manus AI
Date: June 18, 2025
"""

from __future__ import print_function
import os
import sys

# Add FluentNao paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'main', 'python'))
sys.path.insert(0, os.path.dirname(__file__))

# Import the main API server
from fluentnao_api import *
from extensions import register_animation_routes

# Register additional routes
register_animation_routes(
    app, 
    lambda: nao_robot,  # Pass robot as a function to avoid None issues
    create_response, 
    create_error_response, 
    APIError, 
    require_robot, 
    validate_duration
)

# Fix the lambda issue by updating the registration
def get_nao_robot():
    return nao_robot

# Re-register with proper robot access
@app.route('/api/v1/animations/execute', methods=['POST'])
@require_robot
def execute_named_animation():
    """Execute predefined complex animations"""
    try:
        from animations import execute_animation
        
        data = request.get_json() or {}
        animation = data.get('animation')
        parameters = data.get('parameters', {})
        
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
        from animations import get_available_animations
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
        from extensions import _execute_posture_step, _execute_speech_step, _execute_arms_step, _execute_hands_step, _execute_head_step, _execute_leds_step
        
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

if __name__ == '__main__':
    print("FluentNao HTTP API Server v{} (Extended)".format(API_VERSION))
    print("Initializing robot connection...")
    
    try:
        init_robot()
        print("Robot connected successfully!")
        print("Starting API server on http://0.0.0.0:3000")
        print("Available endpoints:")
        print("  GET  /api/v1/status")
        print("  POST /api/v1/robot/stiff")
        print("  POST /api/v1/robot/relax")
        print("  POST /api/v1/posture/stand")
        print("  POST /api/v1/posture/sit")
        print("  POST /api/v1/arms/preset")
        print("  POST /api/v1/hands/position")
        print("  POST /api/v1/head/position")
        print("  POST /api/v1/speech/say")
        print("  POST /api/v1/leds/set")
        print("  POST /api/v1/leds/off")
        print("  POST /api/v1/walk/start")
        print("  POST /api/v1/walk/stop")
        print("  POST /api/v1/walk/preset")
        print("  GET  /api/v1/sensors/sonar")
        print("  POST /api/v1/config/duration")
        print("  GET  /api/v1/operations")
        print("  POST /api/v1/animations/execute")
        print("  GET  /api/v1/animations/list")
        print("  POST /api/v1/animations/sequence")
        print("")
        
        app.run(host='0.0.0.0', port=3000, debug=False)
        
    except Exception as e:
        print("Failed to start server: {}".format(e))
        sys.exit(1)

