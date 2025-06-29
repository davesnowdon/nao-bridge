#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FluentNao API Example Client

Example client demonstrating how to use the FluentNao HTTP API.
This shows various API calls and usage patterns.

Author: Dave Snowdon
Date: June 18, 2025
"""

from __future__ import print_function
import json
import time

try:
    # Python 2
    import urllib2
    from urllib import urlencode
    
    def make_request(url, method='GET', data=None, headers=None):
        """Make HTTP request using urllib2 (Python 2)"""
        if headers is None:
            headers = {'Content-Type': 'application/json'}
        
        if data and isinstance(data, dict):
            data = json.dumps(data)
        
        req = urllib2.Request(url, data=data, headers=headers)
        req.get_method = lambda: method
        
        try:
            response = urllib2.urlopen(req)
            return json.loads(response.read())
        except urllib2.HTTPError as e:
            error_data = e.read()
            try:
                return json.loads(error_data)
            except:
                return {'success': False, 'error': {'message': str(e)}}
        except Exception as e:
            return {'success': False, 'error': {'message': str(e)}}

except ImportError:
    # Python 3 fallback
    import urllib.request
    import urllib.parse
    
    def make_request(url, method='GET', data=None, headers=None):
        """Make HTTP request using urllib (Python 3)"""
        if headers is None:
            headers = {'Content-Type': 'application/json'}
        
        if data and isinstance(data, dict):
            data = json.dumps(data).encode('utf-8')
        
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        
        try:
            response = urllib.request.urlopen(req)
            return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            error_data = e.read().decode('utf-8')
            try:
                return json.loads(error_data)
            except:
                return {'success': False, 'error': {'message': str(e)}}
        except Exception as e:
            return {'success': False, 'error': {'message': str(e)}}

class FluentNaoAPIClient(object):
    """Client for FluentNao HTTP API"""
    
    def __init__(self, base_url='http://localhost:3000'):
        self.base_url = base_url.rstrip('/')
        self.api_base = self.base_url + '/api/v1'
    
    def _request(self, endpoint, method='GET', data=None):
        """Make API request"""
        url = self.api_base + endpoint
        response = make_request(url, method, data)
        
        if response.get('success'):
            print("✅ {}: {}".format(endpoint, response.get('message', 'Success')))
        else:
            error = response.get('error', {})
            print("❌ {}: {}".format(endpoint, error.get('message', 'Unknown error')))
        
        return response
    
    def get_status(self):
        """Get robot status"""
        return self._request('/status')
    
    def enable_stiffness(self, duration=None):
        """Enable robot stiffness"""
        data = {}
        if duration:
            data['duration'] = duration
        return self._request('/robot/stiff', 'POST', data)
    
    def disable_stiffness(self):
        """Disable robot stiffness"""
        return self._request('/robot/relax', 'POST')
    
    def stand(self, speed=0.5, variant='Stand'):
        """Make robot stand"""
        data = {'speed': speed, 'variant': variant}
        return self._request('/posture/stand', 'POST', data)
    
    def sit(self, speed=0.5, variant='Sit'):
        """Make robot sit"""
        data = {'speed': speed, 'variant': variant}
        return self._request('/posture/sit', 'POST', data)
    
    def move_arms(self, position='up', duration=1.5, arms='both'):
        """Move robot arms"""
        data = {
            'position': position,
            'duration': duration,
            'arms': arms
        }
        return self._request('/arms/preset', 'POST', data)
    
    def control_hands(self, left_hand=None, right_hand=None, duration=0.5):
        """Control robot hands"""
        data = {'duration': duration}
        if left_hand:
            data['left_hand'] = left_hand
        if right_hand:
            data['right_hand'] = right_hand
        return self._request('/hands/position', 'POST', data)
    
    def move_head(self, yaw=0, pitch=0, duration=1.0):
        """Move robot head"""
        data = {
            'yaw': yaw,
            'pitch': pitch,
            'duration': duration
        }
        return self._request('/head/position', 'POST', data)
    
    def say(self, text, blocking=False, animated=False):
        """Make robot speak"""
        data = {
            'text': text,
            'blocking': blocking,
            'animated': animated
        }
        return self._request('/speech/say', 'POST', data)
    
    def set_leds(self, eyes=None, ears=None, chest=None, feet=None, duration=0.5):
        """Set LED colors"""
        leds = {}
        if eyes:
            leds['eyes'] = eyes
        if ears:
            leds['ears'] = ears
        if chest:
            leds['chest'] = chest
        if feet:
            leds['feet'] = feet
        
        data = {
            'duration': duration,
            'leds': leds
        }
        return self._request('/leds/set', 'POST', data)
    
    def turn_off_leds(self):
        """Turn off all LEDs"""
        return self._request('/leds/off', 'POST')
    
    def execute_animation(self, animation, parameters=None):
        """Execute predefined animation"""
        data = {'animation': animation}
        if parameters:
            data['parameters'] = parameters
        return self._request('/animations/execute', 'POST', data)
    
    def get_animations(self):
        """Get list of available animations"""
        return self._request('/animations/list')
    
    def execute_sequence(self, sequence, blocking=True):
        """Execute movement sequence"""
        data = {
            'sequence': sequence,
            'blocking': blocking
        }
        return self._request('/animations/sequence', 'POST', data)
    
    def get_sonar(self):
        """Get sonar sensor readings"""
        return self._request('/sensors/sonar')
    
    def set_duration(self, duration):
        """Set global movement duration"""
        data = {'duration': duration}
        return self._request('/config/duration', 'POST', data)

def demo_basic_movements(client):
    """Demonstrate basic robot movements"""
    print("\n=== Basic Movements Demo ===")
    
    # Enable stiffness
    client.enable_stiffness()
    time.sleep(1)
    
    # Stand up
    client.stand()
    time.sleep(2)
    
    # Move arms up
    client.move_arms('up', duration=2.0)
    time.sleep(2)
    
    # Open hands
    client.control_hands('open', 'open')
    time.sleep(1)
    
    # Say hello
    client.say("Hello! I am NAO robot!")
    time.sleep(2)
    
    # Move head
    client.move_head(yaw=30, pitch=0)
    time.sleep(1)
    client.move_head(yaw=-30, pitch=0)
    time.sleep(1)
    client.move_head(yaw=0, pitch=0)
    time.sleep(1)
    
    # Arms down
    client.move_arms('down')
    time.sleep(2)
    
    # Close hands
    client.control_hands('close', 'close')
    time.sleep(1)

def demo_led_control(client):
    """Demonstrate LED control"""
    print("\n=== LED Control Demo ===")
    
    # Red eyes
    client.set_leds(eyes='#FF0000')
    time.sleep(1)
    
    # Green ears
    client.set_leds(ears='#00FF00')
    time.sleep(1)
    
    # Blue chest
    client.set_leds(chest='#0000FF')
    time.sleep(1)
    
    # Yellow feet
    client.set_leds(feet='#FFFF00')
    time.sleep(1)
    
    # All colors at once
    client.set_leds(
        eyes='#FF0000',
        ears='#00FF00', 
        chest='#0000FF',
        feet='#FFFF00'
    )
    time.sleep(2)
    
    # Turn off
    client.turn_off_leds()
    time.sleep(1)

def demo_animations(client):
    """Demonstrate predefined animations"""
    print("\n=== Animations Demo ===")
    
    # Get available animations
    response = client.get_animations()
    if response.get('success'):
        animations = response['data']['animations']
        print("Available animations: {}".format(animations))
    
    # Execute salute
    client.execute_animation('salute')
    time.sleep(4)
    
    # Execute wave
    client.execute_animation('wave')
    time.sleep(4)
    
    # Execute tada with custom message
    client.execute_animation('tada', {'statement': 'Amazing!'})
    time.sleep(5)

def demo_sequence(client):
    """Demonstrate movement sequence"""
    print("\n=== Movement Sequence Demo ===")
    
    sequence = [
        {
            "type": "posture",
            "action": "stand",
            "duration": 2.0
        },
        {
            "type": "speech",
            "action": "say",
            "text": "Starting demonstration sequence",
            "blocking": True
        },
        {
            "type": "arms",
            "action": "preset",
            "position": "up",
            "duration": 1.5
        },
        {
            "type": "hands",
            "action": "position",
            "left_hand": "open",
            "right_hand": "open",
            "duration": 0.5
        },
        {
            "type": "leds",
            "action": "set",
            "leds": {
                "eyes": "#00FF00",
                "chest": "#0000FF"
            }
        },
        {
            "type": "wait",
            "duration": 2.0
        },
        {
            "type": "speech",
            "action": "say",
            "text": "Sequence complete!",
            "blocking": True
        },
        {
            "type": "leds",
            "action": "off"
        }
    ]
    
    client.execute_sequence(sequence)

def demo_sensors(client):
    """Demonstrate sensor reading"""
    print("\n=== Sensor Demo ===")
    
    response = client.get_sonar()
    if response.get('success'):
        data = response['data']
        print("Sonar readings - Left: {:.2f}m, Right: {:.2f}m".format(
            data['left'], data['right']
        ))

def main():
    """Main demo function"""
    print("FluentNao API Client Demo")
    print("========================")
    
    # Create client
    client = FluentNaoAPIClient()
    
    # Check status
    print("\n=== Robot Status ===")
    response = client.get_status()
    
    if not response.get('success'):
        print("❌ Cannot connect to FluentNao API server")
        print("Make sure the API server is running on http://localhost:3000")
        return False
    
    # Run demos
    try:
        demo_basic_movements(client)
        demo_led_control(client)
        demo_animations(client)
        demo_sequence(client)
        demo_sensors(client)
        
        print("\n=== Demo Complete ===")
        print("✅ All API demonstrations completed successfully!")
        
    except KeyboardInterrupt:
        print("\n⚠️  Demo interrupted by user")
    except Exception as e:
        print("\n❌ Demo failed: {}".format(e))
        return False
    
    return True

if __name__ == '__main__':
    success = main()
    exit(0 if success else 1)

