"""Safe argument builders and capability checks for external tools."""
import platform
import shutil


def terminal_capability() -> tuple[bool, str]:
    system = platform.system()
    if system == 'Windows':
        candidates = ('wt.exe', 'powershell.exe')
    elif system == 'Darwin':
        candidates = ('osascript',)
    elif system == 'Linux':
        candidates = ('gnome-terminal', 'konsole', 'xfce4-terminal', 'xterm')
    else:
        return False, f'unsupported operating system: {system}'
    available = next((name for name in candidates if shutil.which(name)), None)
    return (True, available) if available else (False, 'no supported terminal emulator was found')
