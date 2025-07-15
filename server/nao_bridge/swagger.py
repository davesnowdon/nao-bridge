#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FluentNao API Swagger Specification

Contains the OpenAPI/Swagger specification for the FluentNao API.
This module provides the complete API documentation in OpenAPI format.

Author: Dave Snowdon
Date: June 18, 2025
"""

from __future__ import print_function
from flask import jsonify, render_template

def get_swagger_spec(api_version):
    """Get OpenAPI/Swagger specification for the API"""
    swagger_spec = {
        "swagger": "2.0",
        "info": {
            "title": "NAO bridge",
            "description": "A REST API for controlling Aldebaran NAO robots via HTTP requests",
            "version": api_version,
            "contact": {
                "name": "Dave Snowdon"
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
            "/robot/wake": {
                "post": {
                    "tags": ["Robot Control"],
                    "summary": "Wake up robot",
                    "description": "Wake up the robot from rest mode",
                    "responses": {
                        "200": {
                            "description": "Robot woke up",
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
            "/robot/autonomous_life/state": {
                "post": {
                    "tags": ["Robot Control"],
                    "summary": "Set autonomous life state",
                    "description": "Set the autonomous life state of the robot. Valid values are: 'disabled', 'solitary', 'interactive', 'safeguard'",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": False,
                            "schema": {
                                "$ref": "#/definitions/AutonomousLifeRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Autonomous life state set",
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
            "/robot/joints/{chain}/angles": {
                "get": {
                    "tags": ["Robot Control"],
                    "summary": "Get current joint angles for a specified chain",
                    "description": "Get current joint angles for a specified chain. Chain can be one of: Head, Body, LArm, RArm, LLeg, RLeg",
                    "parameters": [
                        {
                            "name": "chain",
                            "in": "path",
                            "required": True,
                            "type": "string",
                            "enum": ["Head", "Body", "LArm", "RArm", "LLeg", "RLeg"],
                            "description": "Joint chain to retrieve angles for"
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Joint angles for chain retrieved",
                            "schema": {
                                "$ref": "#/definitions/JointAnglesResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid chain parameter",
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
            "/robot/joints/{chain}/names": {
                "get": {
                    "tags": ["Robot Control"],
                    "summary": "Get joint names for a specified chain",
                    "description": "Get joint names for a specified chain. Chain can be one of: Head, Body, LArm, RArm, LLeg, RLeg",
                    "parameters": [
                        {
                            "name": "chain",
                            "in": "path",
                            "required": True,
                            "type": "string",
                            "enum": ["Head", "Body", "LArm", "RArm", "LLeg", "RLeg"],
                            "description": "Joint chain to retrieve names for"
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Joint names for chain retrieved",
                            "schema": {
                                "$ref": "#/definitions/JointNamesResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid chain parameter",
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
            "/vision/{camera}/{resolution}": {
                "get": {
                    "tags": ["Vision"],
                    "summary": "Get camera image",
                    "description": "Capture and return an image from the specified NAO camera",
                    "parameters": [
                        {
                            "name": "camera",
                            "in": "path",
                            "required": True,
                            "type": "string",
                            "enum": ["top", "bottom"],
                            "description": "Camera to use: 'top' for forward camera, 'bottom' for downward camera"
                        },
                        {
                            "name": "resolution",
                            "in": "path",
                            "required": True,
                            "type": "string",
                            "enum": ["qqqqvga", "qqvga", "qqqvga", "qvga", "vga", "hvga"],
                            "description": "Image resolution (qqqqvga=40x30, qqvga=80x60, qqqvga=160x120, qvga=320x240, vga=640x480, hvga=1280x960)"
                        },
                        {
                            "name": "format",
                            "in": "query",
                            "required": False,
                            "type": "string",
                            "enum": ["jpeg", "json", "raw"],
                            "default": "jpeg",
                            "description": "Response format: 'jpeg' for JPEG image data, 'json' for JSON with base64 encoded image, 'raw' for raw image data"
                        }
                    ],
                    "produces": ["image/jpeg", "application/json", "application/octet-stream"],
                    "responses": {
                        "200": {
                            "description": "Camera image retrieved",
                            "schema": {
                                "oneOf": [
                                    {
                                        "type": "file",
                                        "format": "binary",
                                        "description": "Raw image data when format=raw"
                                    },
                                    {
                                        "$ref": "#/definitions/VisionResponse",
                                        "description": "JSON response when format=json"
                                    },
                                    {
                                        "type": "file",
                                        "format": "binary",
                                        "description": "JPEG image data when format=jpeg"
                                    }
                                ]
                            }
                        },
                        "400": {
                            "description": "Invalid camera, resolution, or format parameter",
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
            "/vision/resolutions": {
                "get": {
                    "tags": ["Vision"],
                    "summary": "Get available camera resolutions",
                    "description": "Get list of available camera resolutions and options",
                    "responses": {
                        "200": {
                            "description": "Available camera options retrieved",
                            "schema": {
                                "$ref": "#/definitions/VisionResolutionsResponse"
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
            },
            "/behaviour/execute": {
                "post": {
                    "tags": ["Behaviours"],
                    "summary": "Execute a behavior on the robot",
                    "description": "Execute a behavior using the robot's behavior manager",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": True,
                            "schema": {
                                "$ref": "#/definitions/BehaviourExecuteRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Behaviour executed successfully",
                            "schema": {
                                "$ref": "#/definitions/BehaviourResponse"
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
            "/behaviour/{behaviour_type}": {
                "get": {
                    "tags": ["Behaviours"],
                    "summary": "Get list of behaviours",
                    "description": "Get list of all installed, default, or running behaviours on the robot",
                    "parameters": [
                        {
                            "name": "behaviour_type",
                            "in": "path",
                            "required": True,
                            "type": "string",
                            "enum": ["installed", "default", "running"],
                            "description": "Type of behaviours to retrieve: 'installed' for all installed behaviours, 'default' for default behaviours, 'running' for currently running behaviours"
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Available behaviours retrieved",
                            "schema": {
                                "$ref": "#/definitions/BehavioursListResponse"
                            }
                        },
                        "400": {
                            "description": "Invalid behaviour type",
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
            "/behaviour/default": {
                "post": {
                    "tags": ["Behaviours"],
                    "summary": "Set a behaviour as default",
                    "description": "Add or remove a behaviour from the default behaviours list",
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": True,
                            "schema": {
                                "$ref": "#/definitions/BehaviourDefaultRequest"
                            }
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Behaviour default status updated",
                            "schema": {
                                "$ref": "#/definitions/BehaviourResponse"
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
                            "current_posture": {"type": "string"},
                            "active_operations": {"type": "array", "items": {"type": "object"}},
                            "api_version": {"type": "string"},
                            "autonomous_life_state": {"type": "string"},
                            "awake": {"type": "boolean"}
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
            "AutonomousLifeRequest": {
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["disabled", "solitary", "interactive", "safeguard"],
                        "default": "disabled"
                    }
                }
            },
            "StandRequest": {
                "type": "object",
                "properties": {
                    "speed": {"type": "number", "minimum": 0.1, "maximum": 1.0, "default": 0.5},
                    "variant": {
                        "type": "string",
                        "enum": ["Stand", "StandInit", "StandZero"],
                        "default": "Stand"
                    }
                }
            },
            "SitRequest": {
                "type": "object",
                "properties": {
                    "speed": {"type": "number", "minimum": 0.1, "maximum": 1.0, "default": 0.5},
                    "variant": {
                        "type": "string",
                        "enum": ["Sit", "SitRelax"],
                        "default": "Sit"
                    }
                }
            },
            "SpeedRequest": {
                "type": "object",
                "properties": {
                    "speed": {"type": "number", "minimum": 0.1, "maximum": 1.0, "default": 0.5}
                }
            },
            "LieRequest": {
                "type": "object",
                "properties": {
                    "speed": {"type": "number", "minimum": 0.1, "maximum": 1.0, "default": 0.5},
                    "position": {
                        "type": "string",
                        "enum": ["back", "belly"],
                        "default": "back"
                    }
                }
            },
            "ArmsPresetRequest": {
                "type": "object",
                "properties": {
                    "duration": {"type": "number"},
                    "position": {
                        "type": "string",
                        "enum": ["up", "down", "forward", "out", "back"],
                        "default": "up"
                    },
                    "arms": {
                        "type": "string",
                        "enum": ["both", "left", "right"],
                        "default": "both"
                    },
                    "offset": {
                        "type": "object",
                        "properties": {
                            "shoulder_pitch": {"type": "number", "default": 0},
                            "shoulder_roll": {"type": "number", "default": 0}
                        }
                    }
                }
            },
            "HandsRequest": {
                "type": "object",
                "properties": {
                    "duration": {"type": "number"},
                    "left_hand": {
                        "type": "string",
                        "enum": ["open", "close"]
                    },
                    "right_hand": {
                        "type": "string",
                        "enum": ["open", "close"]
                    }
                }
            },
            "HeadPositionRequest": {
                "type": "object",
                "properties": {
                    "duration": {"type": "number"},
                    "yaw": {"type": "number", "minimum": -120, "maximum": 120, "default": 0},
                    "pitch": {"type": "number", "minimum": -40, "maximum": 30, "default": 0}
                }
            },
            "SpeechRequest": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "blocking": {"type": "boolean", "default": False},
                    "animated": {"type": "boolean", "default": False}
                }
            },
            "LEDsRequest": {
                "type": "object",
                "properties": {
                    "duration": {"type": "number"},
                    "leds": {
                        "type": "object",
                        "properties": {
                            "eyes": {"type": "string", "description": "Hex color code (e.g., '#FF0000')"},
                            "ears": {"type": "string", "description": "Hex color code (e.g., '#FF0000')"},
                            "chest": {"type": "string", "description": "Hex color code (e.g., '#FF0000')"},
                            "feet": {"type": "string", "description": "Hex color code (e.g., '#FF0000')"}
                        }
                    }
                }
            },
            "WalkStartRequest": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "minimum": -1.0, "maximum": 1.0, "default": 0.0},
                    "y": {"type": "number", "minimum": -1.0, "maximum": 1.0, "default": 0.0},
                    "theta": {"type": "number", "minimum": -1.0, "maximum": 1.0, "default": 0.0},
                    "speed": {"type": "number", "minimum": 0.1, "maximum": 1.0, "default": 0.5}
                }
            },
            "WalkPresetRequest": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["forward", "backward", "turn_left", "turn_right"],
                        "default": "forward"
                    },
                    "duration": {"type": "number", "default": 3.0},
                    "speed": {"type": "number", "minimum": 0.1, "maximum": 1.0, "default": 1.0}
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
            },
            "VisionResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "camera": {"type": "string"},
                            "resolution": {"type": "string"},
                            "colorspace": {"type": "integer"},
                            "width": {"type": "integer"},
                            "height": {"type": "integer"},
                            "channels": {"type": "integer"},
                            "image_data": {"type": "string", "description": "Base64 encoded image data"},
                            "encoding": {"type": "string"}
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "VisionResolutionsResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "resolutions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "id": {"type": "integer"},
                                        "dimensions": {"type": "string"}
                                    }
                                }
                            },
                            "cameras": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "colorspaces": {
                                "type": "array",
                                "items": {"type": "string"}
                            }
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "BehaviourExecuteRequest": {
                "type": "object",
                "required": ["behaviour"],
                "properties": {
                    "behaviour": {"type": "string", "description": "Name of the behaviour to execute"},
                    "blocking": {"type": "boolean", "default": True, "description": "Whether to block until behaviour completes"}
                }
            },
            "BehaviourResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "behaviour": {"type": "string"},
                            "blocking": {"type": "boolean"}
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "BehavioursListResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "behaviours": {
                                "type": "array",
                                "items": {"type": "string"}
                            }
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "BehaviourDefaultRequest": {
                "type": "object",
                "required": ["behaviour"],
                "properties": {
                    "behaviour": {"type": "string", "description": "Name of the behaviour to set as default"},
                    "default": {"type": "boolean", "default": True, "description": "Whether to set the behaviour as default (true) or remove it from defaults (false)"}
                }
            },
            "JointAnglesResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "chain": {"type": "string", "description": "The joint chain name"},
                            "joints": {
                                "type": "object",
                                "description": "Dictionary mapping joint names to their current angles in radians",
                                "additionalProperties": {"type": "number"}
                            }
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "JointNamesResponse": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "chain": {"type": "string", "description": "The joint chain name"},
                            "joint_names": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Array of joint names in the chain"
                            }
                        }
                    },
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            }
        }
    }
    return swagger_spec

def register_swagger_routes(app, api_version):
    """Register Swagger-related routes with the Flask app"""
    
    @app.route('/api/v1/swagger.json', methods=['GET'])
    def get_swagger_spec_route():
        """Get OpenAPI/Swagger specification for the API"""
        return jsonify(get_swagger_spec(api_version))
    
    @app.route('/swagger')
    def swagger_ui():
        """Serve Swagger UI"""
        return render_template('swagger.html')
    
    @app.route('/openapi.json')
    def swagger_spec_route():
        """Get OpenAPI specification (alternative endpoint)"""
        return jsonify(get_swagger_spec(api_version)) 