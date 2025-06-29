#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FluentNao API Animations Module

Implements complex predefined animations for the FluentNao HTTP API.
These animations correspond to the examples provided in the user's request.

Author: Dave Snowdon
Date: June 18, 2025
"""

from __future__ import print_function

def salute(nao):
    """Execute salute animation"""
    nao.set_duration(1.0)

    # up to forehead
    nao.arms.right_forward(0, 21.0, 6.0)
    nao.elbows.right_bent(0, -5.0).right_turn_up(0, -42.0)
    nao.wrists.right_center(0, 15.0)
    nao.hands.right_close(0).go()

    # out
    nao.arms.right_forward(0, 20.0, 8.0)
    nao.elbows.right_straight(0, -43.0).right_turn_up(0, -38.0)
    nao.hands.right_open().go()

    # down to neutral
    nao.arms.right_down(0, 41.0, 3.0).elbows.turn_in(0, -27.0).right_bent(0, -41.0)
    nao.hands.right_close(0).go()

def wave(nao):
    """Execute wave animation"""
    nao.set_duration(1.0)

    # up
    nao.arms.right_forward(0, 5.0, 4.0).elbows.right_bent(0, -18.0).right_turn_in(0, -17.0)
    nao.wrists.right_turn_out(0, -35.0).hands.right_close(0).go()

    # outward
    nao.arms.right_forward(0, -3.0, 35.0).left_down(0, 36.0, 15.0).elbows.right_turn_up(0, 12.0).left_turn_in(0, -30.0)
    nao.wrists.left_center(0, 3.0).right_center(0, 8.0).hands.right_open().go()

    # down
    nao.arms.right_down(0, 41.0, 23.0).elbows.right_bent(0, -15.0).right_turn_in(0, -14.0)
    nao.wrists.center(0, 3.0).go()

def tada(nao, statement="Ta da!"):
    """Execute tada animation with optional statement"""
    nao.set_duration(1.0)

    nao.hands.close()
    nao.leds.off()

    # ta da
    nao.hands.open(0)
    nao.head.forward(0, 17.0).down(0, -19.0)
    nao.arms.right_up(0, -35.0, 26.0).left_forward(0, -39.0, 14.0)
    nao.elbows.right_straight(0, -22.0).right_turn_up(0, -12.0).left_bent(0, -45.0).left_turn_up(0, 28.0)
    nao.wrists.left_turn_in(0, -23.0)
    nao.leds.eyes(0x7ac5cd).ears(0x7ac5cd).chest(0x7ac5cd).feet(0x7ac5cd)
    nao.say(statement)
    nao.go()

    # sit
    nao.head.forward(0, -1.0).center(0, -1.0)
    nao.arms.right_down(0, 34.0, 16.0).left_down(0, 38.0, 12.0).elbows.turn_in(0, -29.0).right_bent(0, -18.0).left_bent(0, -20.0)
    nao.wrists.center(0, 3.0).hands.right_close(0).left_open(0).go()

def hello_sequence(nao):
    """Execute a friendly hello sequence"""
    nao.set_duration(1.5)
    
    # Stand up straight
    nao.stand()
    
    # Wave hello
    wave(nao)
    
    # Say hello
    nao.say("Hello! Nice to meet you!")
    
    # Return to neutral position
    nao.arms.down()
    nao.hands.close()
    nao.go()

def introduction_sequence(nao):
    """Execute a robot introduction sequence"""
    nao.set_duration(1.0)
    
    # Stand and prepare
    nao.stand()
    nao.say("Hello, I am NAO robot!")
    
    # Show capabilities
    nao.arms.up()
    nao.hands.open()
    nao.go()
    
    nao.say("I can move my arms and hands")
    
    # Head movement
    nao.head.left(0, 30)
    nao.go()
    nao.head.right(0, 60)
    nao.go()
    nao.head.center()
    nao.go()
    
    nao.say("I can turn my head")
    
    # LED demonstration
    nao.leds.eyes(0xFF0000)  # Red
    nao.go()
    nao.wait(1)
    nao.leds.eyes(0x00FF00)  # Green
    nao.go()
    nao.wait(1)
    nao.leds.eyes(0x0000FF)  # Blue
    nao.go()
    nao.wait(1)
    nao.leds.off()
    nao.go()
    
    nao.say("And I can control my LED lights")
    
    # Return to neutral
    nao.arms.down()
    nao.hands.close()
    nao.go()
    
    nao.say("Thank you for watching!")

# Animation registry
ANIMATIONS = {
    'salute': salute,
    'wave': wave,
    'tada': tada,
    'hello': hello_sequence,
    'introduction': introduction_sequence
}

def execute_animation(nao, animation_name, parameters=None):
    """Execute a named animation with optional parameters"""
    if animation_name not in ANIMATIONS:
        raise ValueError("Unknown animation: {}".format(animation_name))
    
    animation_func = ANIMATIONS[animation_name]
    
    # Handle animations with parameters
    if animation_name == 'tada' and parameters and 'statement' in parameters:
        animation_func(nao, parameters['statement'])
    else:
        animation_func(nao)
    
    return True

def get_available_animations():
    """Get list of available animations"""
    return list(ANIMATIONS.keys())

