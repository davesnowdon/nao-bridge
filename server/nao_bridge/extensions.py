#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FluentNao API Extensions

Additional endpoints for complex animations and sequences.
This module extends the main API with advanced functionality.

Author: Dave Snowdon
Date: June 18, 2025
"""

from __future__ import print_function
import sys
import os

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from flask import request, jsonify
from animations import execute_animation, get_available_animations

def register_animation_routes(app, nao_robot, create_response, create_error_response, APIError, require_robot, validate_duration):
    """Register animation-related routes with the Flask app"""
    
    @app.route('/api/v1/animations/execute', methods=['POST'])
    @require_robot
    def execute_named_animation():
        """Execute predefined complex animations"""
        try:
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

