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
from nao_bridge_api import *
from swagger import register_swagger_routes

# Register Swagger routes
register_swagger_routes(app, API_VERSION)

if __name__ == '__main__':
    print("FluentNao HTTP API Server v{} (Extended)".format(API_VERSION))
    print("Initializing robot connection...")
    
    try:
        init_robot()
        print("Robot connected successfully!")
        print("Starting API server on http://0.0.0.0:3000")
        
        app.run(host='0.0.0.0', port=3000, debug=False)
        
    except Exception as e:
        print("Failed to start server: {}".format(e))
        sys.exit(1)

