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
from flask import render_template, jsonify

# Add FluentNao paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
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

@app.route('/api/v1/swagger.json', methods=['GET'])
def get_swagger_spec():
    """Get OpenAPI/Swagger specification for the API"""
    swagger_spec = {
        "swagger": "2.0",
        "info": {
            "title": "FluentNao API",
            "description": "A REST API for controlling Aldebaran NAO robots via HTTP requests",
            "version": API_VERSION,
            "contact": {
                "name": "Manus AI"
            }
        },
        "host": "0.0.0.0:3000",
        "basePath": "/api/v1",
        "schemes": ["http"],
        "consumes": ["application/json"],
        "produces": ["application/json"],
        "paths": {
            "/status": {
                "get": {
                    "tags": ["System"],
                    "summary": "Get robot and API status",
                    "description": "Retrieve current status of the robot and API",
                    "responses": {
                        "200": {
                            "description": "Status retrieved successfully",
                            "schema": {
                                "$ref": "#/definitions/StatusResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/robot/stiff": {
                "post": {
                    "tags": ["Robot Control"],
                    "summary": "Enable robot stiffness",
                    "description": "Make the robot stiff by enabling motors",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/DurationRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Robot stiffness enabled",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/robot/relax": {
                "post": {
                    "tags": ["Robot Control"],
                    "summary": "Disable robot stiffness",
                    "description": "Make the robot relax by disabling motors",
                    "responses": {
                        "200": {
                            "description": "Robot stiffness disabled",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/robot/rest": {
                "post": {
                    "tags": ["Robot Control"],
                    "summary": "Put robot in rest mode",
                    "description": "Put the robot in rest mode",
                    "responses": {
                        "200": {
                            "description": "Robot in rest mode",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/posture/stand": {
                "post": {
                    "tags": ["Posture Control"],
                    "summary": "Move robot to standing position",
                    "description": "Move the robot to a standing position",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/StandRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Robot moved to standing position",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/posture/sit": {
                "post": {
                    "tags": ["Posture Control"],
                    "summary": "Move robot to sitting position",
                    "description": "Move the robot to a sitting position",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/SitRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Robot moved to sitting position",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/posture/crouch": {
                "post": {
                    "tags": ["Posture Control"],
                    "summary": "Move robot to crouching position",
                    "description": "Move the robot to a crouching position",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/SpeedRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Robot moved to crouching position",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/posture/lie": {
                "post": {
                    "tags": ["Posture Control"],
                    "summary": "Move robot to lying position",
                    "description": "Move the robot to a lying position",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/LieRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Robot moved to lying position",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/arms/preset": {
                "post": {
                    "tags": ["Arm Control"],
                    "summary": "Control arms using preset positions",
                    "description": "Move arms to predefined positions",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/ArmsPresetRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Arms moved to position",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/hands/position": {
                "post": {
                    "tags": ["Hand Control"],
                    "summary": "Control hand opening and closing",
                    "description": "Open or close robot hands",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/HandsRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Hand positions updated",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/head/position": {
                "post": {
                    "tags": ["Head Control"],
                    "summary": "Control head positioning",
                    "description": "Move the robot's head to specified angles",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/HeadPositionRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Head position updated",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/speech/say": {
                "post": {
                    "tags": ["Speech"],
                    "summary": "Make the robot speak",
                    "description": "Make the robot speak text with optional animation",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": True,
                            "schema": {
                                "$ref": "#/definitions/SpeechRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Speech command executed",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/leds/set": {
                "post": {
                    "tags": ["LED Control"],
                    "summary": "Control LED colors",
                    "description": "Set colors for various LED groups",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/LEDsRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "LED colors updated",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/leds/off": {
                "post": {
                    "tags": ["LED Control"],
                    "summary": "Turn off all LEDs",
                    "description": "Turn off all robot LEDs",
                    "responses": {
                        "200": {
                            "description": "All LEDs turned off",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/walk/start": {
                "post": {
                    "tags": ["Walking"],
                    "summary": "Start walking with specified parameters",
                    "description": "Start walking with velocity and speed parameters",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/WalkStartRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Walking started",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/walk/stop": {
                "post": {
                    "tags": ["Walking"],
                    "summary": "Stop current walking motion",
                    "description": "Stop the current walking motion",
                    "responses": {
                        "200": {
                            "description": "Walking stopped",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/walk/preset": {
                "post": {
                    "tags": ["Walking"],
                    "summary": "Use predefined walking patterns",
                    "description": "Execute predefined walking patterns",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/WalkPresetRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Walk preset executed",
                            "schema": {
                                "$ref": "#/definitions/SuccessResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/sensors/sonar": {
                "get": {
                    "tags": ["Sensors"],
                    "summary": "Get sonar sensor readings",
                    "description": "Get current sonar sensor readings",
                    "responses": {
                        "200": {
                            "description": "Sonar readings retrieved",
                            "schema": {
                                "$ref": "#/definitions/SonarResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/config/duration": {
                "post": {
                    "tags": ["Configuration"],
                    "summary": "Set global movement duration",
                    "description": "Set the global duration for robot movements",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/DurationRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Global duration set",
                            "schema": {
                                "$ref": "#/definitions/DurationResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/operations": {
                "get": {
                    "tags": ["Operations"],
                    "summary": "List active operations",
                    "description": "Get list of currently active operations",
                    "responses": {
                        "200": {
                            "description": "Operations retrieved",
                            "schema": {
                                "$ref": "#/definitions/OperationsResponse"
                            }
                        }
                    }
                }
            },
            "/operations/{operation_id}": {
                "get": {
                    "tags": ["Operations"],
                    "summary": "Get status of specific operation",
                    "description": "Get detailed status of a specific operation",
                    "parameters": [
                        {
                            "name": "operation_id",
                            "in": "path",
                            "required": True,
                            "type": "string",
                            "description": "Operation ID"
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Operation status retrieved",
                            "schema": {
                                "$ref": "#/definitions/OperationResponse"
                            }
                        },
                        "404": {
                            "description": "Operation not found",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/animations/execute": {
                "post": {
                    "tags": ["Animations"],
                    "summary": "Execute predefined complex animations",
                    "description": "Execute predefined complex animations with parameters",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": True,
                            "schema": {
                                "$ref": "#/definitions/AnimationExecuteRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Animation executed successfully",
                            "schema": {
                                "$ref": "#/definitions/AnimationResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            },
            "/animations/list": {
                "get": {
                    "tags": ["Animations"],
                    "summary": "Get list of available animations",
                    "description": "Get list of all available animations",
                    "responses": {
                        "200": {
                            "description": "Available animations retrieved",
                            "schema": {
                                "$ref": "#/definitions/AnimationsListResponse"
                            }
                        }
                    }
                }
            },
            "/animations/sequence": {
                "post": {
                    "tags": ["Animations"],
                    "summary": "Execute a sequence of movements",
                    "description": "Execute a sequence of different movement types",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": True,
                            "schema": {
                                "$ref": "#/definitions/SequenceRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Sequence executed successfully",
                            "schema": {
                                "$ref": "#/definitions/SequenceResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid parameters",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        },
                        "502": {
                            "description": "Robot not connected",
                            "schema": {
                                "$ref": "#/definitions/ErrorResponse"
                            }
                        }
                    }
                }
            }
        },
        "definitions": {
            "StatusResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "robot_connected": {"type": "boolean"},
                            "robot_ip": {"type": "string"},
                            "battery_level": {"type": "integer"},
                            "temperature": {"type": "number"},
                            "stiffness_enabled": {"type": "boolean"},
                            "current_posture": {"type": "string"},
                            "active_operations": {"type": "array", "items": {"type": "object"}},
                            "api_version": {"type": "string"}
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "SuccessResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {"type": "object"},
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "ErrorResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "error": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "message": {"type": "string"},
                            "details": {"type": "object"}
                        }
                    },
                    "timestamp": {"type": "string"}
                }
            },
            "DurationRequest": {
                "type": "object",
                "properties": {
                    "duration": {"type": "number"}
                }
            },
            "DurationResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "duration": {"type": "number"}
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "StandRequest": {
                "type": "object",
                "properties": {
                    "speed": {"type": "number"},
                    "variant": {"type": "string"}
                }
            },
            "SitRequest": {
                "type": "object",
                "properties": {
                    "speed": {"type": "number"},
                    "variant": {"type": "string"}
                }
            },
            "SpeedRequest": {
                "type": "object",
                "properties": {
                    "speed": {"type": "number"}
                }
            },
            "LieRequest": {
                "type": "object",
                "properties": {
                    "speed": {"type": "number"},
                    "position": {"type": "string"}
                }
            },
            "ArmsPresetRequest": {
                "type": "object",
                "properties": {
                    "duration": {"type": "number"},
                    "position": {"type": "string"},
                    "arms": {"type": "string"},
                    "offset": {
                        "type": "object",
                        "properties": {
                            "shoulder_pitch": {"type": "number"},
                            "shoulder_roll": {"type": "number"}
                        }
                    }
                }
            },
            "HandsRequest": {
                "type": "object",
                "properties": {
                    "duration": {"type": "number"},
                    "left_hand": {"type": "string"},
                    "right_hand": {"type": "string"}
                }
            },
            "HeadPositionRequest": {
                "type": "object",
                "properties": {
                    "duration": {"type": "number"},
                    "yaw": {"type": "number"},
                    "pitch": {"type": "number"}
                }
            },
            "SpeechRequest": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "blocking": {"type": "boolean"},
                    "animated": {"type": "boolean"}
                }
            },
            "LEDsRequest": {
                "type": "object",
                "properties": {
                    "duration": {"type": "number"},
                    "leds": {
                        "type": "object",
                        "properties": {
                            "eyes": {"type": "string"},
                            "ears": {"type": "string"},
                            "chest": {"type": "string"},
                            "feet": {"type": "string"}
                        }
                    }
                }
            },
            "WalkStartRequest": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "theta": {"type": "number"},
                    "speed": {"type": "number"}
                }
            },
            "WalkPresetRequest": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "duration": {"type": "number"},
                    "speed": {"type": "number"}
                }
            },
            "SonarResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "left": {"type": "number"},
                            "right": {"type": "number"},
                            "units": {"type": "string"},
                            "timestamp": {"type": "string"}
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "OperationsResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "active_operations": {
                                "type": "array",
                                "items": {"type": "object"}
                            }
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "OperationResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {"type": "object"},
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "AnimationExecuteRequest": {
                "type": "object",
                "required": ["animation"],
                "properties": {
                    "animation": {"type": "string"},
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "duration_multiplier": {"type": "number"}
                        }
                    }
                }
            },
            "AnimationResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "animation": {"type": "string"},
                            "parameters": {"type": "object"}
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "AnimationsListResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "animations": {
                                "type": "array",
                                "items": {"type": "string"}
                            }
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "SequenceRequest": {
                "type": "object",
                "required": ["sequence"],
                "properties": {
                    "sequence": {
                        "type": "array",
                        "items": {"type": "object"}
                    },
                    "blocking": {"type": "boolean"}
                }
            },
            "SequenceResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "executed_steps": {
                                "type": "array",
                                "items": {"type": "object"}
                            }
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            }
        }
    }
    return jsonify(swagger_spec)

@app.route('/swagger')
def swagger_ui():
    return render_template('swagger.html')

@app.route('/openapi.json')
def swagger_spec():
    return jsonify(swagger_spec)

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

