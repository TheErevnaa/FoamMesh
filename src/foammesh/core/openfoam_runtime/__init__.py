"""OpenFOAM runtime profiles shared by desktop, CLI, API, and automation."""

from .launch_profiles import (
    LaunchCommand,
    LaunchProfileError,
    OpenFoamLaunchProfile,
    configured_profiles,
)

__all__ = [
    'LaunchCommand',
    'LaunchProfileError',
    'OpenFoamLaunchProfile',
    'configured_profiles',
]
