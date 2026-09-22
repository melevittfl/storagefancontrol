import copy
import logging.config
import os

# storagefancontrol.py does 'from log_config import *', so keep the
# module's own imports out of its namespace.
__all__ = ['LOG_SETTINGS', 'LOG_FILE', 'configure_logging']

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


def configure_logging(console=False):
    """
    Set up logging. The daemon logs only to file, but an interactive run
    needs output on the terminal too: a --dry-run that printed nothing at
    all would look like it had silently failed.
    """
    settings = copy.deepcopy(LOG_SETTINGS)

    if console:
        settings['handlers']['console'] = {
            'class': 'logging.StreamHandler',
            'level': 'DEBUG',
            'formatter': 'normal',
            'stream': 'ext://sys.stderr',
        }
        settings['root']['handlers'].append('console')

    logging.config.dictConfig(settings)