#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FluentNao API Server with Extensions

Main server file that includes all endpoints and extensions.
This is the complete API server implementation.

Author: Dave Snowdon
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
from swagger import register_swagger_routes

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

# Register Swagger routes
register_swagger_routes(app, API_VERSION)

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
        print("  GET  /api/v1/swagger.json")
        print("  GET  /swagger")
        print("")
        
        app.run(host='0.0.0.0', port=3000, debug=False)
        
    except Exception as e:
        print("Failed to start server: {}".format(e))
        sys.exit(1)

