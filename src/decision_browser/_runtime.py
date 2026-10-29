import os
import shutil


def azure_cli_executable():
    return (shutil.which('az') or 'az') if os.name == 'nt' else 'az'
