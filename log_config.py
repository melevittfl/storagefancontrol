import os

# storagefancontrol.py does 'from log_config import *', so keep the
# module's own imports out of its namespace.
__all__ = ['LOG_SETTINGS', 'LOG_FILE']

# Resolved against the script directory, not the CWD: TrueNAS SCALE
# Post Init scripts run from an unpredictable working directory.
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fan_control.log')

LOG_SETTINGS = {
    'version': 1,
    'root': {
        'level': 'DEBUG',
        'handlers': ['file'],
    },
    'handlers': {
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'level': 'DEBUG',
            'formatter': 'normal',
            'filename': LOG_FILE,
            'mode': 'a',
            'maxBytes': 10485760,
            'backupCount': 5,
        },
    },
    'formatters': {
        'normal': {
            'format': '%(asctime)s %(levelname)s: %(message)s',
            'datefmt': '%Y-%m-%d %H:%M:%S'
        }
    },
}