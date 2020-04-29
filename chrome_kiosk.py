#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import print_function

"""
Keep Google Chrome Running in Kiosk Mode
"""

__author__ = "Sam Forester"
__email__ = "sam.forester@utah.edu"
__copyright__ = "Copyright (c) 2020 University of Utah, Marriott Library"
__license__ = "MIT"
__version__ = '1.9.2'

import sys
import os
import subprocess
import time
import re
import plistlib
import signal
import shutil
import logging
import json
from datetime import datetime, timedelta

from management_tools import loggers


## CHANGELOG:
#   1.8.0: (2018.10.18)
#       - added remove_user_chrome_profile
#       - modified switch processing
#       - added more logging
#       - added logging stream handler and --verbose flag
#       - modified run loop
#       - replaced set_restart to restart_timer
#       - modified restart_timer to allow for null timers
#       - added pgrep
#       - modified screensaver_is_running to use pgrep()
#   1.8.1: (2018.10.18)
#       - fixed bug that would cause endless tabs to be re-opened
#         if Google Chrome was running at RunTime
#       - renamed launch_chrome_with_switches to launch_chrome
#       - fixed bug causing looping to move too fast during screensaver
#   1.8.2: (2018.10.18)
#       - fixed bug that was not allowing user profile to be removed
#       - added faster loop to restore Chrome after screensaver
#   1.9.0: (2018.10.19)
#       - added better mechanism for checking frontmost window
#       - added ability to detect display sleep
#   1.9.1: (2018.10.19)
#       - fixed error with Popen.call()
#       - fixed parameter error
#   1.9.2: (2019.11.12)
#       - fixed function rename set_restart -> restart_timer
#       - minor formatting changes


class SignalTrap(object):
    """
    Class for trapping interruptions in an attempt to shutdown
    more gracefully
    """
    def __init__(self, logger):
        self.stopped = False
        self.log = logger
        signal.signal(signal.SIGINT, self.trap)
        signal.signal(signal.SIGQUIT, self.trap)
        signal.signal(signal.SIGTERM, self.trap)
        signal.signal(signal.SIGTSTP, self.trap)
    
    def trap(self, signum, frame):
        self.log.debug("received signal: {0}".format(signum))
        self.stopped = True


def app_is_frontmost(name):
    """
    Uses applescript to see if specified app name running and
    frontmost window.
    
    Returns True or False
    """
    # doesn't require Accessibility access
    scpt = ['tell application "System Events"',
                'try',
                    'tell process "{0}"',
                        'if (frontmost is false) then return false',
                    'end tell',
                    'tell application "{0}"',
                        'return (count of windows) is greater than 0',
                    'end tell',
                'on error',
                    'return false',
                'end try',
            'end tell']
    # join all the strings and then format
    applscpt = "\n".join(scpt).format(name)
    cmd = ['osascript', '-e', applscpt]
    try:
        out = subprocess.check_output(cmd).rstrip()
        return True if out == 'true' else False
    except subprocess.CalledProcessError:
        return False


def screensaver_is_running():
    """
    Returns True if ScreenSaverEngine is running
    """
    return pgrep(None, 'ScreenSaverEngine')


def restart_timer(logger=None, **kwargs):
    """
    Returns a closure that uses the time of assignment 
    to return True if that amount of time has passed
    if given 
    
    ::params:: anything that can be used by datetime.timedelta()
    
    >>> restart = restart_timer(seconds=5)
    >>> time.sleep(1)
    >>> restart()
    False
    >>> time.sleep(5)
    >>> restart()
    True
    >>> restart()
    True
    
    >>> restart = restart_timer(hours=2)
    >>> time.sleep(1)
    >>> restart()
    False
    >>> time.sleep(7,200)
    >>> restart()
    True
    
    >>> restart = restart_timer(seconds=0)
    >>> time.sleep(1000)
    >>> restart()
    False
    >>> time.sleep(5)
    >>> restart()
    False
    
    """
    now = datetime.now().replace(microsecond=0)
    restart = now + timedelta(**kwargs)
    empty = (now == restart) or (restart < now)
    if logger and not empty:
        logger.debug("restart timer set: {0}".format(restart))
    def _restart():
        if empty:
            return False
        else:
            return datetime.now() > restart
    return _restart


def display_power():
    """
    Uses `ioreg` to return values of IODisplayWrangler's IOPowerManagent
    """
    cmd = ['/usr/sbin/ioreg', '-w', '0', '-n', 'IODisplayWrangler', 
                                         '-r', 'IODisplayWrangler']
    out = subprocess.check_output(cmd)
    m = re.search(r'"IOPowerManagement" = (\{.+\})', out, re.MULTILINE)
    # convert ioreg out put into something more JSON-y
    j = m.group(1).replace('=', ':')
    return json.loads(j)


def display_sleep():
    """
    Returns True if display is asleep
    """
    if display_power()["CurrentPowerState"] < 3:
        return True
    else:
        return False


def pgrep(logger, name):
    """
    Uses /usr/bin/pgrep to return list of running PIDs.
    returns empty list if no PIDs are found
    """
    if not logger:
        logger = logging.getLogger(__name__)
        logger.addHandler(logging.NullHandler())
    cmd = ['/usr/bin/pgrep', name]
    logger.debug("> {0}".format(" ".join(cmd)))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    if p.returncode == 0:
        pids = [x for x in out.splitlines() if x]
        logger.debug("{0}: pids: {1}".format(name, pids))
        return pids
    else:
        return []


def remove_user_chrome_profile(logger):
    """
    Removes ~/Library/Application Support/Google
    """
    user_d = os.path.expanduser('~/Library/Application Support/Google')
    logger.debug("removing user chrome settings: {0}".format(user_d))
    try:
        shutil.rmtree(user_d)
    except OSError as e:
        # skip OSError: [Errno 2] No such file or directory
        logger.error("unable to remove: {0}: {1}".format(user_d, e))
        if e.errno != 2:
            #logger.error("unable to remove: {0}".format(user_d))
            raise
    if os.path.exists(user_d):
        logger.debug("still exists: {0}".format(user_d))


def launch_chrome(logger, switches, app=None, reset=True):
    """
    Launches Google Chrome in with specified flags
    """
    if not app:
        # default Google Chrome.app location
        app = '/Applications/Google Chrome.app'
    
    if reset:
        remove_user_chrome_profile(logger)
    
    chromebin = os.path.join(app, 'Contents/MacOS/Google Chrome')
    cmd = [chromebin] + switches
    logger.debug("> {0}".format(" ".join(cmd)))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    logger.debug("chrome PID: {0}".format(p.pid))
    return p


def main(args):
    """
    Launch Google Chrome and make sure it's in the foreground
    """
    script = os.path.basename(sys.argv[0])
    scriptname = os.path.splitext(script)[0]
    
    level = loggers.INFO
    if '--debug' in args:
        level = loggers.DEBUG
    
    logger = loggers.FileLogger(name=scriptname, level=level)
    if '--verbose' in args:
        sh = loggers.StreamLogger(level=level)
        logger.addHandler(sh)
    
    logger.debug("{0} started!".format(script))
    settings = '/Library/Management/edu.utah.mlib.kiosk.settings.plist'
        
    # get settings from file
    try:
        logger.debug("getting settings from: {0}".format(settings))
        config = plistlib.readPlist(settings)
    except Exception as e:
        logger.error(e)
        raise
    
    try:
        site = config['site']
    except KeyError:    
        logger.error("no site was specified")
        raise SystemExit("no site was specified")
    
    # add any additionally specified switches
    switches = config.get('switches', [])
    
    switches += ['--kiosk']
    if config.get('isDisplay'):
        switches.append("--app={0}".format(site))
    else:
        switches.append(site)
    
    # path to chrome app (default: /Applications/Google Chrome.app)
    app = config.get('location', '/Applications/Google Chrome.app')
    logger.debug("location: {0}".format(app))
    
    # seconds to wait between loops
    wait = config.get('wait', 5)    
    logger.debug("wait: {0}".format(wait))
    
    # timer to restart: (default: def _(): return False)
    restart = config.get('restart', -1)
    logger.debug("restart seconds: {0}".format(restart))
    time_to_restart = restart_timer(seconds=restart)
    
    # clear user profile between launches (default: True)
    reset = config.get('remove-profile', True)    
    logger.debug("remove profile: {0}".format(reset))
    
    # Kill any running instances of Google Chrome (or endless tabs)
    pids = pgrep(logger, "Google Chrome")
    if pids:
        logger.debug("Chrome was already running... killing...")
        cmd = ['/usr/bin/killall', "Google Chrome"]
        logger.debug("> {0}".format(" ".join(cmd)))
        subprocess.Popen(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).wait()
    # start Google Chrome
    logger.info("starting Google Chrome")
    chrome = launch_chrome(logger, switches, app, reset)
    
    sig = SignalTrap(logger)
    while not sig.stopped:
        # if chrome.poll() returns anything but None, it exited
        running = chrome.poll() is None
        
        if screensaver_is_running() or display_sleep():
            if running:
                logger.debug("closing chrome while display inactive")
                chrome.terminate()
                chrome.wait()
            # TO-DO: would like to keep this from looping every second
            # during screensaver, but that's for another day
            time.sleep(1)
            continue
        
        # Automatically restart Chrome after a certain amount of time
        if time_to_restart():
            msg = "restarting after {0} seconds".format(restart)
            logger.debug(msg)
            logger.debug("resetting restart timer")
            time_to_restart = restart_timer(seconds=restart)
            if running:
                chrome.terminate()
                chrome.wait()
                continue
            else:
                logger.debug("chrome wasn't running... odd")
            logger.debug("resetting restart timer")
            time_to_restart = restart_timer(seconds=restart)
        
        # check to see that Chrome is the frontmost process
        if running and not sig.stopped:
            if not app_is_frontmost("Google Chrome"):
                logger.debug("Google Chrome isn't active")
                chrome.terminate()
                chrome.wait()
                running = False
        
        # Finally, restart chrome if it isn't running
        if not running:
            logger.error("chrome isn't running")
            pid = chrome.pid
            poll = chrome.poll()
            logger.debug("dead chrome: {0} poll: {1}".format(pid,poll))
            # relaunch chrome
            chrome = launch_chrome(logger, switches, app, reset)
        time.sleep(wait)
    
    chrome.terminate()
    logger.debug("{0} finished!".format(script))
    return 0


if __name__ == '__main__':
    try:
        args = sys.argv[1:]
    except IndexError:
        args = []
    retcode = main(args)
    sys.exit(retcode)
