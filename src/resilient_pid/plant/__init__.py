"""
src/resilient_pid/plant/__init__.py
Plant abstraction package exposing simulated and physical runtime targets.
"""
from resilient_pid.plant.plant_interface import BasePlant, SimulatedPlant, HardwarePlant, main

__all__ = ["BasePlant", "SimulatedPlant", "HardwarePlant", "main"]